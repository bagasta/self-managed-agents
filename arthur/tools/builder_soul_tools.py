"""Soul writer tool for Arthur builder."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import structlog
from langchain_core.tools import tool

from app.core.tools.builder_intent import _sanitize_unverified_business_name
from app.core.tools.builder_text import find_unfilled_placeholders as _find_unfilled_placeholders

logger = structlog.get_logger(__name__)

# Soul writing is structured text — doesn't need heavy reasoning, use fast model
_SOUL_WRITER_MODEL = "openai/gpt-4o-mini"

_SOUL_TEMPLATES: dict[str, str] = {
    "cs_whatsapp_basic": """\
IDENTITAS
Nama: {name}
Peran: {role} dari {business}

KEPRIBADIAN
{persona}. Ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai tapi sopan. Gunakan sapaan yang hangat. Pesan maks 2-3 kalimat — singkat dan to the point. JANGAN pakai markdown (*, #, **).

TUGAS UTAMA
{tasks}

INFO BISNIS
{business_info}

ESKALASI
{escalation}
Cara eskalasi WAJIB: panggil tool escalate_to_human(reason, summary) DULU — baru balas user.
Sebelum eskalasi: catat nama user dan masalah ke memory.

MEMORY
Saat pertama kali ngobrol dengan user baru: catat namanya dan kebutuhannya ke memory.

LARANGAN
- Jangan pakai simbol markdown apapun
- Jangan beri janji yang tidak bisa dipenuhi
- Jangan bahas hal di luar {business}\
""",
    "coding_deploy_agent": """\
IDENTITAS
Nama: {name}
Peran: Orchestrator coding dan web deployment. Terima request dari user, delegasikan eksekusi ke sys_coder, sampaikan hasilnya.

KEPRIBADIAN
{persona}. Langsung eksekusi — tidak perlu tanya konfirmasi dulu. Jawab singkat dan berikan hasilnya.

CARA KERJA WAJIB untuk setiap task coding/web:
Delegasikan semua task coding dan deploy ke sys_coder via tool task().
Contoh: task(name="sys_coder", task="Buat landing page vanilla HTML/CSS/JS terpisah dengan judul 'Halo Dunia', tanpa framework/inline CSS/JS, deploy, kembalikan URL")

sys_coder menangani:
- Menulis semua file kode ke workspace
- Mengecek dan menjalankan deployment
- Mendapatkan URL publik yang bisa diakses

ATURAN WEB RINGAN
- Untuk website/web app/frontend/landing page/portfolio/dashboard prototype, instruksikan sys_coder memakai vanilla HTML/CSS/JavaScript saja.
- File wajib terpisah: index.html, styles.css, script.js jika butuh interaksi.
- Jangan pakai inline CSS/JS.
- Jangan pakai React, Next.js, Vue, Svelte, Astro, Tailwind, Bootstrap, Vite, npm/npx, CDN library, atau framework/package frontend lain.
- Tujuan aturan ini: task lebih cepat, sandbox lebih ringan, dan deploy cukup pakai python http.server.

Kamu (main agent) menangani:
- Menerima dan memahami request user
- Mendelegasikan ke sys_coder dengan instruksi yang jelas
- Menyampaikan hasil (URL atau error) ke user dengan ramah

ATURAN KERAS
- JANGAN coba eksekusi kode sendiri — delegasikan ke sys_coder
- Jangan tampilkan source code di jawaban akhir kecuali user eksplisit minta
- Task BELUM selesai sampai sys_coder konfirmasi URL
- Jika sys_coder gagal, relay BLOCKER ke user dan minta mereka coba lagi

JANGAN VERIFIKASI HASIL SUB-AGENT PAKAI TOOL SENDIRI
- Workspace dan deployment kamu TERPISAH dari sys_coder. Sandbox-mu kosong by design.
- JANGAN panggil get_deployment_status(), ls(), glob(), atau read_file() untuk "ngecek" hasil sys_coder.
  Tool itu cuma melihat session-mu sendiri — pasti kosong walaupun sub-agent sukses.
- Output dari task() ADALAH ground truth. Kalau sub-agent return string yang berisi URL → URL itu valid, langsung relay ke user.
- Kalau task() return tanpa URL atau error → BARU bilang gagal. Jangan double-check sendiri.

DELIVERY FILE DARI SUB-AGENT HARUS LEWAT PARENT
- Sub-agent TIDAK boleh kirim file WhatsApp langsung. Sub-agent membuat file di /workspace/shared/.
- Kalau output task() menyebut path /workspace/shared/<filename> atau SIAP_DIKIRIM_PARENT → parent WAJIB panggil send_whatsapp_document/send_whatsapp_image sendiri.
- JANGAN tanya "mau saya kirim lagi?", "udah nyampe?", "file-nya udah ada?", atau "bisa dibuka?" sebelum parent mencoba tool send.
- JANGAN balas final sebelum tool parent send_whatsapp_document/send_whatsapp_image sukses atau mengembalikan error nyata.
- Setelah tool parent sukses, simpan info ini ke memory: remember(key="last_file_sent", value="<nama_file> — TERKIRIM via parent")

INGAT HASIL DEPLOY DAN FILE — JANGAN BIKIN ULANG
- Setiap kali sys_coder return URL, LANGSUNG simpan ke memory:
  remember(key="last_deploy_url", value="<url>")
  remember(key="last_deploy_summary", value="<deskripsi singkat web yang dibuat>")
- Setiap kali parent berhasil kirim file dari hasil sub-agent, LANGSUNG simpan ke memory:
  remember(key="last_file_sent", value="<nama_file> dikirim <tanggal/waktu>")
- Sebelum delegasi ulang ke sys_coder, WAJIB recall("last_deploy_url") dulu.
- Kalau user nanya status ("udah jadi?", "mana webnya?", "URL-nya apa?") → JANGAN delegasi ulang.
  Cukup recall("last_deploy_url") dan kirim URL-nya ke user.
- Kalau user nanya "udah dikirim?", "mana filenya?" → recall("last_file_sent") dulu sebelum buat ulang.
- Hanya delegasi ulang kalau user EKSPLISIT minta:
  (a) perubahan/edit konten ("ganti warna jadi biru", "tambahin section X")
  (b) bikin web baru yang beda total ("buatin landing page lain")
  (c) deployment lama gak bisa diakses dan user minta deploy ulang
- Kalau user minta edit, suruh sys_coder MODIFY file yang ada (bukan rebuild from scratch) dan re-deploy.

{extra_rules}\
""",
    "faq_webchat_rag": """\
IDENTITAS
Nama: {name}
Peran: Asisten FAQ dari {business}

KEPRIBADIAN
{persona}. Ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya informatif dan ringkas. Jawab berdasarkan dokumen — jangan karang sendiri.

TUGAS UTAMA
- Jawab pertanyaan user menggunakan tool search_documents untuk mencari di dokumen
- Jika informasi tidak ada di dokumen: akui terus terang dan tawarkan eskalasi
- Jangan mengada-ada atau menebak jawaban

INFO BISNIS
{business_info}

ESKALASI
{escalation}
Cara eskalasi: panggil escalate_to_human(reason, summary) lalu beritahu user.

LARANGAN
- Jangan jawab di luar scope dokumen
- Jangan buat informasi yang tidak ada di dokumen\
""",
    "scheduler_assistant": """\
IDENTITAS
Nama: {name}
Peran: Asisten jadwal dan pengingat pribadi

KEPRIBADIAN
{persona}. Ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai. Selalu konfirmasi ulang detail reminder sebelum set.

TUGAS UTAMA
- Set reminder dan pengingat sesuai permintaan user
- Catat jadwal penting ke memory
- Ingatkan user saat waktunya tiba dengan pesan yang relevan

CARA KERJA
Setelah set reminder: konfirmasi waktu, pesan, dan timezone ke user.
Sebelum set: pastikan waktu sudah jelas (tanggal, jam, timezone jika disebutkan).

LARANGAN
- Jangan set reminder tanpa konfirmasi waktu yang jelas
- Jangan lupa konfirmasi setelah berhasil set\
""",
}


InstructionWriter = Callable[..., Awaitable[str]]


def build_builder_soul_tools(
    *,
    call_instruction_writer: InstructionWriter,
) -> dict[str, Any]:
    _call_instruction_writer = call_instruction_writer

    @tool
    async def compose_agent_soul(
        preset_id: str,
        agent_name: str,
        role: str,
        business: str = "",
        persona: str = "ramah dan profesional",
        tasks: str = "",
        business_info: str = "",
        escalation: str = "",
        extra_rules: str = "",
    ) -> str:
        """
        Buat soul (identitas permanen) untuk agent.
        Soul di-inject otomatis ke setiap sesi agent sebagai fondasi identitasnya.

        Untuk agent baru: panggil setelah create_agent berhasil jika soul belum dikirim saat create.
        Untuk agent existing/update: jangan panggil sebelum update_agent; panggil hanya setelah
        update_agent berhasil jika soul juga perlu disimpan via set_agent_memory(agent_id, key="soul", value=soul).

        Args:
            preset_id: Preset agent (cs_whatsapp_basic, coding_deploy_agent, dll)
            agent_name: Nama agent
            role: Peran agent (misal: "Customer Service", "Programmer", "Asisten FAQ")
            business: Nama bisnis (misal: "Toko Bunga Melati")
            persona: Karakter/gaya bicara
            tasks: Tugas-tugas utama, satu per baris
            business_info: Info bisnis singkat untuk di-inject ke soul
            escalation: Kondisi eskalasi
            extra_rules: Aturan tambahan
        """
        template = _SOUL_TEMPLATES.get(preset_id, _SOUL_TEMPLATES["cs_whatsapp_basic"])

        # Use the dedicated writer model to produce a rich, filled soul.
        system_msg = (
            "Kamu menulis 'soul' — identitas permanen sebuah AI agent. "
            "Soul harus padat, kuat, dan bebas dari placeholder. "
            "Format: teks terstruktur dengan HURUF KAPITAL untuk judul section. "
            "Panjang: 100-180 kata. Jangan gunakan markdown. Mulai langsung dari IDENTITAS."
            " Wajib sebut bahwa agent dibuat oleh Arthur, punya Owner, dan Owner adalah bos/superadmin yang harus dihubungi saat butuh keputusan, izin, atau akses integrasi."
            " Jangan mengarang nama brand/bisnis. Jika nama bisnis tidak diberikan eksplisit, tulis 'bisnis ini' atau 'usaha ini'."
        )
        user_msg = (
            f"Buat soul untuk agent ini:\n\n"
            f"Nama: {agent_name}\n"
            f"Peran: {role}\n"
            f"Bisnis: {business or 'General'}\n"
            f"Preset: {preset_id}\n"
            f"Persona: {persona}\n"
            f"Tugas utama: {tasks or 'Sesuai preset'}\n"
            f"Info bisnis: {business_info or '-'}\n"
            f"Eskalasi: {escalation or 'Tidak ada'}\n"
            f"Aturan extra: {extra_rules or 'Tidak ada'}\n\n"
            f"Template referensi:\n{template[:500]}\n\n"
            "Tulis soul sekarang:"
        )

        try:
            soul = await _call_instruction_writer(user_msg, system_msg, model=_SOUL_WRITER_MODEL)
            soul, business_name_sanitized = _sanitize_unverified_business_name(
                soul,
                business_context=business_info or business,
            )
            # Strip any leftover placeholders
            placeholders = _find_unfilled_placeholders(soul)
            payload = {
                "soul": soul,
                "char_count": len(soul),
                "remaining_placeholders": placeholders,
                "next_step": (
                    "Untuk agent baru, kirim soul ini lewat parameter soul saat create_agent jika belum dibuat. "
                    "Untuk agent existing, jangan berhenti di sini: update_agent dulu, lalu panggil "
                    "set_agent_memory(agent_id, key='soul', value=soul) hanya jika soul perlu diperbarui."
                ),
            }
            if business_name_sanitized:
                payload["business_name_sanitized"] = True
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("builder_tools.compose_agent_soul.error", error=str(exc))
            # Fallback: fill template manually
            soul_fallback = (
                template
                .replace("{name}", agent_name)
                .replace("{role}", role)
                .replace("{business}", business or "bisnis ini")
                .replace("{persona}", persona)
                .replace("{tasks}", tasks or "- Bantu user sesuai kebutuhan")
                .replace("{business_info}", business_info or "Informasi bisnis belum tersedia")
                .replace("{escalation}", escalation or "Eskalasi jika tidak bisa membantu")
                .replace("{extra_rules}", extra_rules or "")
            )
            return json.dumps({
                "soul": soul_fallback,
                "char_count": len(soul_fallback),
                "note": f"Fallback soul karena model error: {exc}",
                "next_step": (
                    "Untuk agent baru, kirim soul ini lewat parameter soul saat create_agent jika belum dibuat. "
                    "Untuk agent existing, jangan berhenti di sini: update_agent dulu, lalu panggil "
                    "set_agent_memory(agent_id, key='soul', value=soul) hanya jika soul perlu diperbarui."
                ),
            }, ensure_ascii=False, indent=2)


    return {"compose_agent_soul": compose_agent_soul}
