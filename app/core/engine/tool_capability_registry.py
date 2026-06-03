from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class ToolCapability:
    key: str
    label: str
    enabled_description: str
    disabled_reason: str
    fallback_sentence: str
    high_risk: bool = False
    claim_patterns: tuple[str, ...] = ()


CAPABILITIES: tuple[ToolCapability, ...] = (
    ToolCapability(
        key="memory",
        label="Memory",
        enabled_description="bisa menyimpan dan membaca konteks penting user/customer.",
        disabled_reason="tidak ada memory tool pada run ini.",
        fallback_sentence="Saya belum bisa menyimpan atau membaca memory di run ini.",
    ),
    ToolCapability(
        key="escalation",
        label="Escalation",
        enabled_description="bisa meneruskan kasus ke Owner/operator manusia.",
        disabled_reason="handoff ke Owner/operator belum aktif pada run ini.",
        fallback_sentence="Saya belum bisa meneruskan ini otomatis ke Owner/operator. Owner perlu mengaktifkan eskalasi dulu.",
        high_risk=True,
        claim_patterns=(
            r"\b(sudah|telah)\s+(saya\s+)?(teruskan|eskalasi|laporkan)\b",
            r"\b(admin|operator|owner)\s+(akan|sudah)\s+(menghubungi|mengecek|membantu)\b",
        ),
    ),
    ToolCapability(
        key="sandbox",
        label="Sandbox",
        enabled_description="bisa menjalankan analisis/kode di workspace sandbox.",
        disabled_reason="sandbox tidak tersedia pada run ini.",
        fallback_sentence="Saya belum bisa menjalankan kode atau membaca file secara langsung di run ini. Owner perlu mengaktifkan kemampuan file/sandbox dulu.",
        high_risk=True,
        claim_patterns=(
            r"\b(saya\s+)?(sudah|telah)\s+(menjalankan|eksekusi|mengeksekusi)\s+(kode|script|program)\b",
            r"\bhasil\s+(eksekusi|run|menjalankan kode)\b",
            r"\b(saya\s+)?(sudah|telah)\s+(membaca|proses|memproses)\s+file\s+(excel|xlsx|csv|pdf)\b",
        ),
    ),
    ToolCapability(
        key="deploy",
        label="Deploy",
        enabled_description="bisa menyiapkan URL publik untuk prototype/app jika sandbox mendukung.",
        disabled_reason="deploy tidak tersedia pada run ini.",
        fallback_sentence="Saya belum bisa deploy atau membuat URL publik dari run ini. Owner perlu mengaktifkan deploy/sandbox dulu.",
        high_risk=True,
        claim_patterns=(
            r"\b(sudah|telah)\s+(saya\s+)?deploy\b",
            r"\blink\s+(deploy|preview|public)\b",
            r"\b(trycloudflare|vercel\.app|netlify\.app)\b",
        ),
    ),
    ToolCapability(
        key="rag",
        label="Dokumen/RAG",
        enabled_description="bisa mencari jawaban dari dokumen yang sudah diupload.",
        disabled_reason="pencarian dokumen/RAG tidak aktif atau belum ada konteks dokumen pada run ini.",
        fallback_sentence="Saya belum bisa menjawab dari dokumen internal karena fitur dokumen belum aktif atau belum ada dokumen yang tersedia.",
        high_risk=True,
        claim_patterns=(
            r"\bberdasarkan\s+(dokumen|knowledge base|file yang diupload)\b",
            r"\b(saya\s+)?(sudah|telah)\s+(cek|membaca|menemukan)\s+(dokumen|knowledge base)\b",
        ),
    ),
    ToolCapability(
        key="scheduler",
        label="Scheduler",
        enabled_description="bisa membuat reminder atau jadwal otomatis.",
        disabled_reason="scheduler tidak tersedia pada run ini.",
        fallback_sentence="Saya belum bisa membuat reminder otomatis di run ini. Owner perlu mengaktifkan scheduler dulu.",
        high_risk=True,
        claim_patterns=(
            r"\b(pengingat|reminder|jadwal)\s+(sudah|telah)\s+(dibuat|disetel|diatur)\b",
            r"\b(sudah|telah)\s+(saya\s+)?(set|buat|atur)\s+(reminder|pengingat|jadwal)\b",
        ),
    ),
    ToolCapability(
        key="whatsapp_media",
        label="WhatsApp Media",
        enabled_description="bisa mengirim gambar/dokumen lewat WhatsApp.",
        disabled_reason="pengiriman media WhatsApp tidak tersedia pada run ini.",
        fallback_sentence="Saya belum bisa mengirim file/gambar lewat WhatsApp di run ini. Owner perlu mengaktifkan WhatsApp Media dulu.",
        high_risk=True,
        claim_patterns=(
            r"\b(sudah|telah)\s+(saya\s+)?(kirim|mengirim)\s+(file|dokumen|pdf|gambar|foto)\b",
            r"\b(file|dokumen|pdf|gambar|foto)\s+(sudah|telah)\s+(saya\s+)?kirim\b",
            r"\[(document_sent|image_sent)\]",
        ),
    ),
    ToolCapability(
        key="tavily",
        label="Web Search",
        enabled_description="bisa mencari dan membaca informasi web terbaru.",
        disabled_reason="web search tidak aktif pada run ini.",
        fallback_sentence="Saya belum bisa browsing web dari run ini.",
    ),
    ToolCapability(
        key="google_workspace",
        label="Google Workspace",
        enabled_description="bisa memakai integrasi Google Workspace jika auth Owner sudah valid.",
        disabled_reason="Google Workspace tidak aktif atau belum tersambung pada run ini.",
        fallback_sentence="Saya belum bisa mengakses Google Workspace. Owner perlu mengaktifkan dan menghubungkan Google dulu.",
        high_risk=True,
        claim_patterns=(
            r"\b(sudah|telah)\s+(saya\s+)?(buat|membuat|cek|membaca|update|mengubah)\s+(google|gmail|drive|docs|sheets|slides|calendar|forms)\b",
            r"\b(google\s+(docs|sheets|slides|forms|drive|calendar)|gmail)\s+(sudah|telah)\b",
        ),
    ),
    ToolCapability(
        key="mcp",
        label="Integrasi Eksternal",
        enabled_description="bisa memakai integrasi resmi yang tersambung, seperti Google Workspace.",
        disabled_reason="integrasi eksternal tidak tersedia pada run ini.",
        fallback_sentence="Integrasi eksternal belum tersedia pada run ini.",
    ),
    ToolCapability(
        key="subagents",
        label="Sub-agent",
        enabled_description="bisa mendelegasikan pekerjaan ke agent spesialis jika tersedia.",
        disabled_reason="sub-agent tidak tersedia pada run ini.",
        fallback_sentence="Saya belum bisa mendelegasikan pekerjaan ke agent spesialis di run ini.",
    ),
    ToolCapability(
        key="builder",
        label="Arthur Builder",
        enabled_description="bisa membuat, membaca, mengubah, dan menghapus agent platform milik Owner.",
        disabled_reason="builder tools tidak tersedia pada run ini.",
        fallback_sentence="Saya belum bisa mengelola agent platform dari run ini.",
    ),
)


CAPABILITY_BY_KEY = {cap.key: cap for cap in CAPABILITIES}
HIGH_RISK_CAPABILITIES = tuple(cap.key for cap in CAPABILITIES if cap.high_risk)


def _tools_config_enabled(tools_config: dict[str, Any], capability: str) -> bool:
    if capability == "google_workspace":
        mcp_cfg = tools_config.get("mcp")
        if not isinstance(mcp_cfg, dict):
            return False
        if "servers" in mcp_cfg or "enabled" in mcp_cfg:
            return bool(mcp_cfg.get("enabled")) and "google_workspace" in (mcp_cfg.get("servers") or {})
        return isinstance(mcp_cfg.get("google_workspace"), dict)
    if capability == "subagents":
        subagents_cfg = tools_config.get("subagents")
        return bool(subagents_cfg.get("enabled") if isinstance(subagents_cfg, dict) else subagents_cfg)
    return bool(tools_config.get(capability))


def is_capability_enabled(
    capability: str,
    *,
    tools_config: dict[str, Any] | None = None,
    active_groups: list[str] | tuple[str, ...] | set[str] | None = None,
) -> bool:
    cfg = tools_config if isinstance(tools_config, dict) else {}
    groups = {str(group) for group in (active_groups or []) if group}
    if capability == "subagents" and any(group.startswith("subagents(") for group in groups):
        return True
    if capability == "google_workspace" and (
        "mcp" in groups or any(group.startswith("mcp(") for group in groups) or "google_reauth" in groups
    ):
        return _tools_config_enabled(cfg, "google_workspace")
    return capability in groups or _tools_config_enabled(cfg, capability)


def build_runtime_tool_contract_text(
    *,
    tools_config: dict[str, Any] | None,
    active_groups: list[str] | tuple[str, ...] | set[str] | None,
) -> str:
    enabled: list[str] = []
    disabled: list[str] = []
    for cap in CAPABILITIES:
        active = is_capability_enabled(cap.key, tools_config=tools_config, active_groups=active_groups)
        if active:
            enabled.append(f"  - {cap.label}: aktif; {cap.enabled_description}")
        elif cap.high_risk:
            disabled.append(
                f"  - {cap.label}: tidak aktif/tersedia pada run ini; {cap.disabled_reason} jangan klaim bisa memakainya."
            )

    lines = [
        "## Runtime Tool Contract",
        "- Sumber kebenaran tools adalah runtime platform, bukan instructions/soul yang dibuat LLM.",
        "- Kalau capability tidak aktif di daftar ini, jangan mengaku bisa menjalankannya dan minta Owner mengaktifkan/setup dulu jika diperlukan.",
        "- Tools aktif:",
    ]
    lines.extend(enabled or ["  - Tidak ada capability khusus yang aktif."])
    lines.append("- Capability rawan halu yang sedang tidak tersedia:")
    lines.extend(disabled or ["  - Tidak ada capability rawan yang terdeteksi nonaktif."])
    return "\n".join(lines)


def disabled_capability_claims(
    reply: str,
    *,
    tools_config: dict[str, Any] | None,
    active_groups: list[str] | tuple[str, ...] | set[str] | None,
) -> list[ToolCapability]:
    text = (reply or "").strip().lower()
    if not text:
        return []
    blocked: list[ToolCapability] = []
    for cap in CAPABILITIES:
        if not cap.high_risk or is_capability_enabled(cap.key, tools_config=tools_config, active_groups=active_groups):
            continue
        for pattern in cap.claim_patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                blocked.append(cap)
                break
    return blocked
