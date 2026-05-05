"""
subagent_builder.py — Membangun daftar sub-agent untuk Deep Agents SDK.

Dipecah dari agent_runner.py (item 2.1 production plan).

Fungsi yang diekspor:
  build_subagents(agent_ids, parent_session_id, db, log)

Konstanta:
  _SYSTEM_SUBAGENTS — list preset sub-agents bawaan sistem

Runtime wiring contract
-----------------------
Subagents yang butuh sandbox (sandbox: true di tools_config) di-compile sebagai
CompiledSubAgent via create_deep_agent() dengan backend=DockerBackend(sub_sandbox).

Ini KRITIS untuk kebenaran deploy path:
  - write_file / execute / ls / glob / grep  → dari FilesystemMiddleware pakai DockerBackend(sub_sandbox)
  - deploy_app / get_deployment_status       → dari build_deployment_tools(sub_sandbox)
  - Keduanya pakai sub_sandbox.workspace_dir yang SAMA → tidak ada workspace mismatch

Subagent tanpa sandbox: plain SubAgent dict, dapat FilesystemMiddleware dari parent backend
(StateBackend atau parent DockerBackend).
"""
from __future__ import annotations

import uuid
from typing import Any

from langchain_openai import ChatOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.database import AsyncSessionLocal

from app.config import get_settings
from app.core.infra.sandbox import DockerSandbox
from app.core.engine.tool_builder import (
    _is_enabled,
    build_deployment_tools,
    build_http_tools,
    build_memory_tools,
    build_sandbox_binary_tool,
    build_skill_tools,
)

settings = get_settings()


# ---------------------------------------------------------------------------
# Built-in system sub-agents
# ---------------------------------------------------------------------------

_SYSTEM_SUBAGENTS: list[dict] = [
    {
        "name": "sys_critic",
        "description": "Quality reviewer: evaluasi output agent lain, approve jika OK atau reject dengan feedback spesifik untuk diperbaiki.",
        "system_prompt": (
            "Kamu adalah agen critic dan quality reviewer. Tugasmu adalah mengevaluasi output yang diberikan kepadamu.\n\n"
            "Cara kerja:\n"
            "1. Baca output yang perlu direview dengan teliti\n"
            "2. Evaluasi berdasarkan: akurasi, kelengkapan, relevansi dengan task, dan kualitas\n"
            "3. Berikan verdict dengan format:\n\n"
            "   **VERDICT: APPROVED** — jika output sudah baik dan bisa digunakan\n"
            "   atau\n"
            "   **VERDICT: REJECTED** — jika output perlu diperbaiki\n\n"
            "4. Jika REJECTED, berikan feedback spesifik: apa yang salah, apa yang kurang, dan apa yang harus diperbaiki\n"
            "5. Jika APPROVED, berikan catatan singkat mengapa output sudah memenuhi standar\n\n"
            "Jadilah kritis tapi konstruktif. Jangan approve output yang mengandung informasi salah, "
            "kode yang error, atau tidak menjawab task dengan benar."
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": False, "http": False},
    },
    {
        "name": "sys_researcher",
        "description": "Riset spesialis: cari dan rangkum informasi dari internet via HTTP tools.",
        "system_prompt": (
            "Kamu adalah agen riset spesialis. Tugasmu adalah mencari, mengumpulkan, dan merangkum informasi "
            "dari internet secara akurat dan terstruktur.\n\n"
            "Cara kerja:\n"
            "1. Gunakan http_get untuk mengakses URL dan mencari informasi\n"
            "2. Ringkas temuan dengan jelas dan terstruktur\n"
            "3. Sertakan sumber informasi\n"
            "4. Jika informasi tidak ditemukan, jelaskan apa yang kamu coba dan apa hasilnya\n\n"
            "Selalu kembalikan hasil riset yang lengkap, akurat, dan bisa langsung digunakan."
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"http": {"enabled": True}, "sandbox": False},
    },
    {
        "name": "sys_coder",
        "description": "Programmer spesialis: tulis dan jalankan kode (Python, JS, HTML, dll) di sandbox. Bisa deploy website/app ke public URL via Cloudflare tunnel.",
        "system_prompt": (
            "Kamu adalah agen programmer full-stack spesialis. Tugasmu adalah menulis, menjalankan, men-debug kode, "
            "dan men-deploy aplikasi/website ke public URL.\n\n"
            "Kamu bisa menggunakan bahasa pemrograman apapun: Python, JavaScript/Node.js, HTML/CSS, Go, Bash, dll.\n\n"
            "ATURAN WAJIB untuk website/web app/aplikasi yang perlu diakses:\n"
            "1. Tulis semua file yang dibutuhkan ke workspace menggunakan write_file\n"
            "2. Panggil get_deployment_status() — jika 'running' kembalikan URL yang ada, jangan deploy ulang\n"
            "3. Jika belum ada deployment → panggil deploy_app(command, port)\n"
            "   - Static website (HTML/CSS/JS): deploy_app('python3 -m http.server 8080', 8080)\n"
            "   - Flask/FastAPI: deploy_app('pip install flask && python app.py', 8080)\n"
            "   - Node.js: deploy_app('npm install && node server.js', 3000)\n"
            "4. Setelah deploy_app() selesai, verifikasi: panggil get_deployment_status() — pastikan status 'running' dan URL ada\n"
            "5. Jika URL kosong atau status bukan 'running', panggil get_deployment_logs() untuk debug, lalu perbaiki\n\n"
            "ATURAN OUTPUT — WAJIB diikuti tanpa pengecualian:\n"
            "- JANGAN tulis teks apapun sebelum semua tool selesai dipanggil\n"
            "- Teks output pertama yang kamu tulis = FINAL REPLY = task selesai\n"
            "- Jadi: tulis semua file → deploy → verifikasi URL → BARU tulis output\n"
            "- Output akhir: maks 5 baris, sertakan URL jika ada\n"
            "- JANGAN dump source code HTML/CSS/JS di output akhir kecuali user EKSPLISIT minta kodenya\n"
            "- JANGAN jelaskan cara kerja kode — langsung eksekusi\n"
            "- JANGAN cari inspirasi desain dari internet kecuali user minta\n"
            "- Task BELUM selesai sampai deploy_app() sukses dan URL dikonfirmasi via get_deployment_status()\n\n"
            "Alur kerja untuk script/komputasi (bukan web):\n"
            "1. Tulis kode menggunakan write_file, jalankan dengan execute()\n"
            "2. Debug jika error, kembalikan hasil eksekusi\n"
            "3. Output akhir: hasil/error saja, bukan listing kode lengkap\n\n"
            "Install dependency: execute('pip install <package>') atau execute('npm install <package>')"
        ),
        "model": "deepseek/deepseek-v4-flash",
        "tools_config": {"sandbox": True, "deploy": True, "http": False},
    },
    {
        "name": "sys_writer",
        "description": "Penulis dan editor spesialis: buat, edit, dan format konten tulisan.",
        "system_prompt": (
            "Kamu adalah agen penulis dan editor spesialis. Tugasmu adalah membuat, mengedit, dan memformat "
            "konten tulisan berkualitas tinggi.\n\n"
            "Kemampuan:\n"
            "- Menulis artikel, laporan, email, proposal, dan konten lainnya\n"
            "- Mengedit dan memperbaiki tulisan yang ada\n"
            "- Mengubah format dan tone tulisan sesuai kebutuhan\n"
            "- Menerjemahkan antara Bahasa Indonesia dan Inggris\n\n"
            "Selalu hasilkan tulisan yang jelas, terstruktur, dan sesuai tone yang diminta."
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": False, "http": False},
    },
    {
        "name": "sys_system_message_builder",
        "description": (
            "Spesialis menulis system prompt / instructions agent WhatsApp. "
            "Berikan konteks bisnis dan kebutuhan user, agent ini akan menghasilkan "
            "instructions yang siap pakai sesuai best practices platform."
        ),
        "system_prompt": (
            "Kamu adalah spesialis dalam menulis system prompt (instructions) untuk AI Agent WhatsApp.\n\n"
            "INPUT yang akan kamu terima:\n"
            "- Nama agent\n"
            "- Konteks bisnis / use case\n"
            "- Persona dan gaya bicara (santai/formal)\n"
            "- Fitur yang diaktifkan (escalation, scheduler, http, rag, whatsapp_media, dll)\n"
            "- Kondisi eskalasi ke operator (jika ada)\n"
            "- Informasi bisnis spesifik (produk, harga, jam buka, kebijakan, dll)\n\n"
            "OUTPUT yang kamu hasilkan:\n"
            "Satu blok teks instructions yang lengkap, siap di-paste sebagai system prompt agent.\n\n"
            "ATURAN WAJIB saat menulis instructions:\n"
            "1. JANGAN pakai markdown — TIDAK ADA **, ##, *, atau backtick. WhatsApp tidak merender markdown.\n"
            "2. Instruksikan agent untuk singkat: 1-3 kalimat per balasan, hindari wall of text.\n"
            "3. Tentukan bahasa respons secara eksplisit (Indonesia default).\n"
            "4. Jika escalation aktif: instruksikan tool escalate_to_human(reason, summary) WAJIB dipanggil sebelum balas user. "
            "Sertakan kondisi eskalasi yang spesifik. Jangan hanya bilang 'diteruskan ke tim'.\n"
            "5. Jika scheduler aktif: sebutkan kapan agent boleh set reminder.\n"
            "6. Jika http aktif: instruksikan cara mengambil data eksternal jika dibutuhkan.\n"
            "7. Sertakan minimal 1-2 contoh percakapan (few-shot) yang sesuai tone dan bisnis.\n"
            "8. Sertakan section YANG TIDAK BOLEH DILAKUKAN yang spesifik untuk bisnis tersebut.\n\n"
            "TEMPLATE STRUKTUR (ikuti urutan ini):\n\n"
            "Kamu adalah [Nama], [peran] dari [bisnis/konteks].\n\n"
            "TUGASMU:\n"
            "[Tugas utama 1]\n"
            "[Tugas utama 2]\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia, [santai/formal]\n"
            "Sapaan: [misal: Halo Kak!]\n"
            "Panjang pesan: singkat, 1-2 kalimat per poin\n"
            "JANGAN pakai simbol *, #, atau format markdown apapun\n\n"
            "YANG TIDAK BOLEH DILAKUKAN:\n"
            "[Larangan spesifik bisnis]\n\n"
            "ESKALASI KE OPERATOR: (hanya jika escalation aktif)\n"
            "Eskalasikan jika: [kondisi spesifik]\n"
            "Cara eskalasi WAJIB: panggil tool escalate_to_human(reason, summary) terlebih dahulu, baru balas user\n"
            "JANGAN hanya bilang diteruskan ke tim tanpa memanggil tool escalate_to_human\n"
            "Sebelum eskalasi, catat ke memory: nama user, masalah, waktu\n\n"
            "INFORMASI PENTING:\n"
            "[Konten bisnis: produk, harga, jam buka, kebijakan, dll]\n\n"
            "CONTOH PERCAKAPAN:\n"
            "User: [pertanyaan umum]\n"
            "[Nama]: [jawaban ideal, singkat, tanpa markdown]\n\n"
            "User: [pertanyaan lain]\n"
            "[Nama]: [jawaban ideal]\n\n"
            "Hasilkan HANYA teks instructions-nya saja, tanpa penjelasan tambahan atau komentar."
        ),
        "model": "anthropic/claude-sonnet-4-6",
        "tools_config": {"sandbox": False, "http": False},
    },
    {
        "name": "sys_analyst",
        "description": "Analis data spesialis: olah data, kalkulasi, dan buat laporan analisis.",
        "system_prompt": (
            "Kamu adalah agen analis data spesialis. Tugasmu adalah mengolah data, melakukan kalkulasi, "
            "dan membuat laporan analisis.\n\n"
            "Cara kerja:\n"
            "1. Terima data dalam bentuk teks, CSV, JSON, atau format lain\n"
            "2. Tulis kode Python dengan pandas/numpy menggunakan write_file\n"
            "3. Jalankan analisis di sandbox menggunakan execute\n"
            "4. Buat ringkasan temuan dan insight yang actionable\n"
            "5. Format hasil sebagai tabel atau laporan terstruktur\n\n"
            "Install library: execute('pip install pandas numpy')"
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": True, "http": False},
    },

]


def _make_sub_llm(spec_or_model: str | Any) -> ChatOpenAI:
    """Build a ChatOpenAI LLM for a subagent, handling mistral prefix."""
    _sm = spec_or_model if isinstance(spec_or_model, str) else (spec_or_model or "")
    _sm_is_mistral = _sm.startswith("mistral/") or _sm.startswith("mistral-")
    return ChatOpenAI(
        model=_sm.removeprefix("mistral/") if _sm_is_mistral else _sm,
        api_key=settings.mistral_api_key if _sm_is_mistral else settings.openrouter_api_key,
        base_url="https://api.mistral.ai/v1" if _sm_is_mistral else "https://openrouter.ai/api/v1",
        temperature=0.5,
        max_tokens=settings.default_subagent_max_tokens,
    )


def _build_system_subagent(spec: dict, parent_session_id: uuid.UUID) -> tuple[dict, DockerSandbox | None]:
    """
    Build a SubAgent (or CompiledSubAgent) dict and optional DockerSandbox from a system sub-agent spec.

    For sandbox-enabled subagents (e.g. sys_coder, sys_analyst):
      Returns a CompiledSubAgent with its own create_deep_agent(backend=DockerBackend(sub_sandbox)).
      This ensures write_file/execute use sub_sandbox.workspace_dir, matching deploy_app's workspace.

    For non-sandbox subagents:
      Returns a plain SubAgent dict; FilesystemMiddleware from parent backend is sufficient.
    """
    sub_cfg = spec.get("tools_config", {})
    extra_tools: list = []
    sub_sandbox: DockerSandbox | None = None

    needs_sandbox = _is_enabled(sub_cfg, "sandbox", default=False)

    if needs_sandbox:
        sub_session_id = f"{parent_session_id}_sys_{spec['name']}"
        sub_sandbox = DockerSandbox(sub_session_id)
        # sandbox_write_binary_file: custom tool not covered by Deep Agents FilesystemMiddleware
        extra_tools.extend(build_sandbox_binary_tool(sub_sandbox))

        if _is_enabled(sub_cfg, "deploy", default=False):
            # deploy_app / get_deployment_status / get_deployment_logs / stop_deployment
            # These MUST use sub_sandbox so workspace matches write_file's workspace.
            extra_tools.extend(build_deployment_tools(sub_sandbox))

    if _is_enabled(sub_cfg, "http", default=False):
        extra_tools.extend(build_http_tools(sub_cfg))

    sub_llm = _make_sub_llm(spec["model"])

    # For sandbox-capable subagents: compile as CompiledSubAgent with its own DockerBackend.
    # This is the key fix: the SDK's FilesystemMiddleware inside this compiled agent will
    # use DockerBackend(sub_sandbox), so write_file writes to sub_sandbox.workspace_dir —
    # the same directory that deploy_app mounts. No workspace mismatch.
    if needs_sandbox and sub_sandbox is not None:
        try:
            from deepagents import create_deep_agent
            from app.core.engine.deep_agent_backend import DockerBackend

            sub_backend = DockerBackend(sub_sandbox)
            runnable = create_deep_agent(
                model=sub_llm,
                tools=extra_tools,
                system_prompt=spec["system_prompt"],
                backend=sub_backend,
            )
            return {
                "name": spec["name"],
                "description": spec["description"],
                "runnable": runnable,
            }, sub_sandbox
        except (ImportError, TypeError, AttributeError) as _dag_err:
            import structlog
            structlog.get_logger().warning(
                "subagent.deepagent_fallback",
                error=str(_dag_err)[:300],
                name=spec["name"],
            )
            pass  # Fall through to plain SubAgent dict (degraded mode)

    # Non-sandbox subagent or fallback: plain SubAgent dict.
    # create_deep_agent will inject FilesystemMiddleware from parent backend.
    return {
        "name": spec["name"],
        "description": spec["description"],
        "system_prompt": spec["system_prompt"],
        "tools": extra_tools,
        "model": sub_llm,
    }, sub_sandbox


async def build_subagents(
    agent_ids: list[str],
    parent_session_id: uuid.UUID,
    db: AsyncSession,
    log: Any,
) -> tuple[list, list[DockerSandbox]]:
    """
    Build SubAgent / CompiledSubAgent list untuk Deep Agents SDK.

    - agent_ids kosong → pakai semua system sub-agents (tidak perlu DB)
    - agent_ids berisi UUID → load agent custom dari DB

    Returns (subagent_list, sandbox_list) — caller wajib close sandboxes di finally block.

    Wiring contract:
      Sandbox-capable subagents dikembalikan sebagai CompiledSubAgent (ada key 'runnable').
      Non-sandbox subagents dikembalikan sebagai SubAgent dict (ada key 'system_prompt').
    """
    subagents: list = []
    sub_sandboxes: list[DockerSandbox] = []

    if not agent_ids:
        # Try loading system agents from DB first (seeded via scripts/seed_system_agents.py)
        from app.models.agent import Agent as AgentModel
        try:
            db_sys_result = await db.execute(
                select(AgentModel).where(
                    AgentModel.capabilities.contains(["subagent"]),
                    AgentModel.is_deleted.is_(False),
                )
            )
            db_sys_agents = db_sys_result.scalars().all()
        except Exception:
            db_sys_agents = []

        if db_sys_agents:
            # Build specs from DB records (same shape as _SYSTEM_SUBAGENTS)
            specs = [
                {
                    "name": a.name,
                    "description": a.description or "",
                    "system_prompt": a.instructions or "",
                    "model": a.model or "openai/gpt-4o-mini",
                    "tools_config": a.tools_config if isinstance(a.tools_config, dict) else {},
                }
                for a in db_sys_agents
            ]
            log.info("build_subagents.from_db", count=len(specs))
        else:
            # Fallback to hardcoded defaults
            specs = _SYSTEM_SUBAGENTS
            log.info("build_subagents.from_hardcoded", count=len(specs))

        for spec in specs:
            sa, ssb = _build_system_subagent(spec, parent_session_id)
            subagents.append(sa)
            if ssb:
                sub_sandboxes.append(ssb)
        return subagents, sub_sandboxes

    from app.models.agent import Agent as AgentModel

    for raw_id in agent_ids:
        try:
            agent_uuid = uuid.UUID(raw_id)
        except ValueError:
            log.warning("build_subagents.invalid_uuid", agent_id=raw_id)
            continue

        try:
            result = await db.execute(
                select(AgentModel).where(
                    AgentModel.id == agent_uuid,
                    AgentModel.is_deleted.is_(False),
                )
            )
            agent_row = result.scalar_one_or_none()
        except Exception as exc:
            log.error("build_subagents.db_error", agent_id=raw_id, error=str(exc))
            continue

        if agent_row is None:
            log.warning("build_subagents.not_found", agent_id=raw_id)
            continue

        sub_cfg: dict[str, Any] = agent_row.tools_config if isinstance(agent_row.tools_config, dict) else {}
        extra_tools: list = []
        sub_sandbox: DockerSandbox | None = None
        needs_sandbox = _is_enabled(sub_cfg, "sandbox", default=False)

        if needs_sandbox:
            sub_session_id = f"{parent_session_id}_sub_{agent_uuid}"
            sub_sandbox = DockerSandbox(sub_session_id)
            sub_sandboxes.append(sub_sandbox)
            extra_tools.extend(build_sandbox_binary_tool(sub_sandbox))

            if _is_enabled(sub_cfg, "deploy", default=False):
                extra_tools.extend(build_deployment_tools(sub_sandbox))

        if _is_enabled(sub_cfg, "memory", default=True):
            extra_tools.extend(build_memory_tools(agent_row.id, AsyncSessionLocal, scope=None))

        if _is_enabled(sub_cfg, "skills", default=True):
            extra_tools.extend(build_skill_tools(agent_row.id, AsyncSessionLocal))

        if _is_enabled(sub_cfg, "http", default=False):
            extra_tools.extend(build_http_tools(sub_cfg))

        # Intentionally excluded: escalation, scheduler, wa_agent_manager, tool_creator
        # Subagents do not have channels and should not trigger external side effects.

        _rm = agent_row.model or "openai/gpt-4o-mini"
        sub_llm = _make_sub_llm(_rm)
        sub_llm.temperature = getattr(agent_row, "temperature", 0.7)

        # For sandbox-capable custom subagents: compile with own DockerBackend so
        # write_file and deploy_app both target the same workspace directory.
        if needs_sandbox and sub_sandbox is not None:
            try:
                from deepagents import create_deep_agent
                from app.core.engine.deep_agent_backend import DockerBackend

                sub_backend = DockerBackend(sub_sandbox)
                runnable = create_deep_agent(
                    model=sub_llm,
                    tools=extra_tools,
                    system_prompt=agent_row.instructions or "You are a helpful assistant.",
                    backend=sub_backend,
                )
                sa: dict = {
                    "name": agent_row.name,
                    "description": (agent_row.instructions or "")[:300].replace("\n", " "),
                    "runnable": runnable,
                }
                subagents.append(sa)
                log.info(
                    "build_subagents.loaded_compiled",
                    name=agent_row.name,
                    tools=len(extra_tools),
                    sandbox=True,
                )
                continue
            except (ImportError, TypeError) as exc:
                log.warning("build_subagents.compiled_fallback", name=agent_row.name, error=str(exc))

        # Non-sandbox custom subagent or fallback: plain SubAgent dict.
        sa = {
            "name": agent_row.name,
            "description": (agent_row.instructions or "")[:300].replace("\n", " "),
            "system_prompt": agent_row.instructions or "You are a helpful assistant.",
            "tools": extra_tools,
            "model": sub_llm,
        }
        subagents.append(sa)
        log.info("build_subagents.loaded", name=agent_row.name, tools=len(extra_tools))

    return subagents, sub_sandboxes
