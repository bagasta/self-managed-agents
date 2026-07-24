"""Shared runtime instruction text helpers for Arthur-created agents."""
from __future__ import annotations

from app.core.utils.phone_utils import normalize_phone


def _append_google_workspace_instruction(instructions: str | None) -> tuple[str, bool]:
    base = (instructions or "").rstrip()
    has_workspace_instruction = (
        "Google Workspace tools aktif" in base
        or "Google Docs" in base and "Google Drive" in base
    )
    has_sheets_safety_rule = "ATURAN GOOGLE SHEETS" in base
    if has_workspace_instruction and has_sheets_safety_rule:
        return base, False
    blocks: list[str] = []
    if not has_workspace_instruction:
        blocks.append(
            "KEMAMPUAN GOOGLE WORKSPACE\n"
            "Jika user meminta membuat atau mengedit Google Docs, Google Sheets, Google Drive, Gmail, Calendar, Slides, atau Forms, "
            "gunakan integrasi Google Workspace yang tersedia. Jangan mengatakan tidak punya akses jika integrasi Google aktif. "
            "Untuk laporan riset di Google Docs, lakukan riset terlebih dahulu, susun konten lengkap, lalu buat dokumen Google Docs dan kirim link dokumennya. "
            "Jika akun Google Owner belum terhubung atau perlu izin ulang, jelaskan secara natural bahwa Owner perlu menghubungkan Google lagi dan berikan link otentikasi jika tersedia. "
            "Jangan menyebut istilah teknis internal/protokol tool kepada user."
        )
    if not has_sheets_safety_rule:
        blocks.append(
            "ATURAN GOOGLE SHEETS\n"
            "Untuk permintaan menambah, mencatat, atau mengimpor record ke sheet yang sudah ada: wajib baca struktur/header sheet terlebih dahulu dengan read_sheet_values, "
            "lalu gunakan append_table_rows dengan object yang key-nya persis nama header. Jangan gunakan modify_sheet_values atau memilih range A1/A2 untuk menambah record. "
            "modify_sheet_values hanya untuk perubahan pada range yang user sebutkan secara eksplisit. Jika header tidak ada, ambigu, atau data tidak cocok dengan kolom, jangan menulis dan minta klarifikasi."
        )
    addition = "\n\n".join(blocks)
    return f"{base}\n\n{addition}" if base else addition, True


def _platform_staff_identity_block(
    *,
    owner_phone: str | None,
    operator_phone: str = "",
    operator_name: str = "",
) -> str:
    owner_id = normalize_phone(owner_phone or "") or str(owner_phone or "").strip() or "Owner platform"
    operator_bits: list[str] = []
    if operator_name.strip():
        operator_bits.append(operator_name.strip())
    if operator_phone.strip():
        operator_bits.append(operator_phone.strip())
    operator_label = " / ".join(operator_bits)

    owner_line = f"Owner agent ini adalah {owner_id}."
    if operator_label and operator_label != owner_id:
        owner_line += f" Operator/admin yang bisa dihubungi: {operator_label}."

    return (
        "IDENTITAS PLATFORM DAN OWNER\n"
        "Kamu adalah staff AI yang dibuat dan dikonfigurasi oleh Arthur, Agent Builder di platform ini.\n"
        f"{owner_line}\n"
        "Owner adalah bos dan superadmin untuk agent ini. Saat Owner memberi arahan, perlakukan itu sebagai instruksi kerja utama selama tidak melanggar keamanan atau kebijakan platform.\n"
        "Jika kamu tidak tahu jawaban, kekurangan data, butuh keputusan manusia, atau ada masalah yang tidak bisa kamu selesaikan sendiri, minta bantuan Owner/operator dengan jujur.\n"
        "Jika kamu butuh akses akun atau integrasi milik Owner seperti Google tetapi akses belum terhubung, expired, atau ditolak, jangan mengarang hasil. Minta Owner menghubungkan atau memberi izin ulang lewat link yang disediakan platform.\n"
        "Saat bicara ke pelanggan akhir, tetap gunakan bahasa sederhana dan jangan menyebut istilah teknis internal."
    )


def _append_platform_staff_identity_instruction(
    instructions: str | None,
    *,
    owner_phone: str | None,
    operator_phone: str = "",
    operator_name: str = "",
) -> tuple[str, bool]:
    base = (instructions or "").rstrip()
    if "IDENTITAS PLATFORM DAN OWNER" in base and "dibuat dan dikonfigurasi oleh Arthur" in base:
        return base, False
    block = _platform_staff_identity_block(
        owner_phone=owner_phone,
        operator_phone=operator_phone,
        operator_name=operator_name,
    )
    return f"{base}\n\n{block}" if base else block, True
