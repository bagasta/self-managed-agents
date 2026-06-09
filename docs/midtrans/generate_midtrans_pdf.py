from __future__ import annotations

from pathlib import Path
import textwrap


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "End-to-End-Process-Managed-Agent-SaaS.pdf"

PAGE_W = 595
PAGE_H = 842
MARGIN_X = 46
TOP_Y = 792
BOTTOM_Y = 46


def _pdf_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .encode("latin-1", "replace")
        .decode("latin-1")
    )


def _rgb(r: int, g: int, b: int) -> str:
    return f"{r / 255:.3f} {g / 255:.3f} {b / 255:.3f}"


class Canvas:
    def __init__(self) -> None:
        self.pages: list[list[str]] = []
        self.ops: list[str] = []
        self.y = TOP_Y
        self.page_no = 0
        self.new_page(cover=True)

    def new_page(self, *, cover: bool = False) -> None:
        if self.ops:
            self._footer()
            self.pages.append(self.ops)
        self.page_no += 1
        self.ops = []
        self.y = TOP_Y
        if cover:
            self.rect(0, 724, PAGE_W, 118, fill=(37, 99, 235), stroke=None)
            self.text(MARGIN_X, 792, "End-to-End Customer Process", size=24, bold=True, color=(255, 255, 255))
            self.text(MARGIN_X, 762, "Managed Agent Platform - Dokumen Pendukung Onboarding Midtrans", size=12, color=(255, 255, 255))
            self.y = 704
        else:
            self.text(MARGIN_X, 805, "Managed Agent Platform - End-to-End Process", size=10, bold=True, color=(37, 99, 235))
            self.line(MARGIN_X, 796, PAGE_W - MARGIN_X, 796, color=(203, 213, 225))
            self.y = 776

    def _footer(self) -> None:
        self.line(MARGIN_X, 34, PAGE_W - MARGIN_X, 34, color=(203, 213, 225))
        self.text(MARGIN_X, 20, "Dokumen pendukung onboarding Midtrans - produk jasa digital/SaaS.", size=8, color=(100, 116, 139))
        self.text(PAGE_W - 92, 20, f"Halaman {self.page_no}", size=8, color=(100, 116, 139))

    def finish(self) -> None:
        if self.ops:
            self._footer()
            self.pages.append(self.ops)
            self.ops = []

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        fill: tuple[int, int, int] | None = None,
        stroke: tuple[int, int, int] | None = (203, 213, 225),
    ) -> None:
        if fill:
            self.ops.append(f"{_rgb(*fill)} rg")
        if stroke:
            self.ops.append(f"{_rgb(*stroke)} RG 0.8 w")
        op = "B" if fill and stroke else "f" if fill else "S"
        self.ops.append(f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} re {op}")

    def line(self, x1: float, y1: float, x2: float, y2: float, *, color: tuple[int, int, int] = (15, 23, 42)) -> None:
        self.ops.append(f"{_rgb(*color)} RG 0.7 w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def text(
        self,
        x: float,
        y: float,
        value: str,
        *,
        size: int = 10,
        bold: bool = False,
        color: tuple[int, int, int] = (23, 32, 51),
    ) -> None:
        font = "F2" if bold else "F1"
        self.ops.append(f"BT /{font} {size} Tf {_rgb(*color)} rg {x:.1f} {y:.1f} Td ({_pdf_escape(value)}) Tj ET")

    def ensure(self, height: int) -> None:
        if self.y - height < BOTTOM_Y:
            self.new_page()

    def h2(self, value: str) -> None:
        self.ensure(46)
        self.y -= 8
        self.text(MARGIN_X, self.y, value, size=15, bold=True, color=(22, 50, 92))
        self.y -= 8
        self.line(MARGIN_X, self.y, PAGE_W - MARGIN_X, self.y, color=(37, 99, 235))
        self.y -= 18

    def h3(self, value: str) -> None:
        self.ensure(28)
        self.text(MARGIN_X, self.y, value, size=11, bold=True, color=(31, 63, 115))
        self.y -= 16

    def paragraph(self, value: str, *, width: int = 96, size: int = 10) -> None:
        lines = textwrap.wrap(value, width=width)
        self.ensure(14 * len(lines) + 8)
        for line in lines:
            self.text(MARGIN_X, self.y, line, size=size)
            self.y -= 14
        self.y -= 5

    def bullet(self, items: list[str], *, width: int = 92) -> None:
        for item in items:
            lines = textwrap.wrap(item, width=width)
            self.ensure(14 * len(lines) + 4)
            self.text(MARGIN_X + 8, self.y, "-", size=10, bold=True, color=(37, 99, 235))
            self.text(MARGIN_X + 22, self.y, lines[0], size=10)
            self.y -= 14
            for line in lines[1:]:
                self.text(MARGIN_X + 22, self.y, line, size=10)
                self.y -= 14
            self.y -= 3
        self.y -= 3

    def info_box(self, title: str, body: str, *, fill: tuple[int, int, int] = (239, 246, 255)) -> None:
        lines = textwrap.wrap(body, width=92)
        height = 30 + 13 * len(lines)
        self.ensure(height + 8)
        self.rect(MARGIN_X, self.y - height + 10, PAGE_W - 2 * MARGIN_X, height, fill=fill, stroke=(191, 219, 254))
        self.text(MARGIN_X + 12, self.y - 8, title, size=10, bold=True, color=(30, 64, 175))
        ty = self.y - 24
        for line in lines:
            self.text(MARGIN_X + 12, ty, line, size=9)
            ty -= 13
        self.y -= height + 8

    def card(self, title: str, lines: list[str]) -> None:
        wrapped: list[str] = []
        for line in lines:
            wrapped.extend(textwrap.wrap(line, width=84) or [""])
        height = 34 + 13 * len(wrapped)
        self.ensure(height + 8)
        self.rect(MARGIN_X, self.y - height + 10, PAGE_W - 2 * MARGIN_X, height, fill=(248, 250, 252), stroke=(203, 213, 225))
        self.text(MARGIN_X + 12, self.y - 9, title, size=11, bold=True, color=(23, 52, 95))
        ty = self.y - 27
        for line in wrapped:
            self.text(MARGIN_X + 12, ty, line, size=9)
            ty -= 13
        self.y -= height + 8

    def table(self, headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
        x0 = MARGIN_X
        row_h = 26
        self.ensure(row_h * (len(rows) + 1) + 8)
        self.rect(x0, self.y - row_h + 8, sum(widths), row_h, fill=(239, 246, 255), stroke=(203, 213, 225))
        x = x0
        for i, header in enumerate(headers):
            self.text(x + 6, self.y - 9, header, size=9, bold=True, color=(23, 52, 95))
            self.line(x, self.y + 8, x, self.y - row_h + 8, color=(203, 213, 225))
            x += widths[i]
        self.line(x, self.y + 8, x, self.y - row_h + 8, color=(203, 213, 225))
        self.y -= row_h
        for row in rows:
            wrapped_cols = [textwrap.wrap(cell, width=max(12, int(widths[i] / 5.6))) or [""] for i, cell in enumerate(row)]
            height = max(row_h, 13 * max(len(col) for col in wrapped_cols) + 12)
            self.ensure(height + 4)
            self.rect(x0, self.y - height + 8, sum(widths), height, fill=(255, 255, 255), stroke=(203, 213, 225))
            x = x0
            for i, lines in enumerate(wrapped_cols):
                self.line(x, self.y + 8, x, self.y - height + 8, color=(203, 213, 225))
                ty = self.y - 9
                for line in lines:
                    self.text(x + 6, ty, line, size=8)
                    ty -= 12
                x += widths[i]
            self.line(x, self.y + 8, x, self.y - height + 8, color=(203, 213, 225))
            self.y -= height
        self.y -= 10


def write_pdf(path: Path, pages: list[list[str]]) -> None:
    objects: list[bytes] = []
    catalog_id = 1
    pages_id = 2
    font_regular_id = 3
    font_bold_id = 4
    page_ids: list[int] = []
    content_ids: list[int] = []

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    next_id = 5
    for page_ops in pages:
        page_id = next_id
        content_id = next_id + 1
        next_id += 2
        page_ids.append(page_id)
        content_ids.append(content_id)
        stream = "\n".join(page_ops).encode("latin-1", "replace")
        page_obj = (
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects.append(page_obj)
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{idx} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(output)


def build() -> None:
    c = Canvas()

    c.info_box(
        "Identitas Dokumen",
        "Nama produk/brand: Managed Agent Platform. Jenis usaha: SaaS / jasa AI agent. Website/aplikasi: https://managed-agent.chiefaiofficer.id. Tujuan pembayaran: payment link Midtrans dari Arthur untuk subscription, renewal, token top-up, dan layanan setup.",
    )

    c.h2("1. Ringkasan Produk Barang/Jasa")
    c.paragraph(
        "Managed Agent Platform adalah layanan SaaS untuk membuat, menjalankan, dan mengelola AI agent yang dapat melayani customer melalui WhatsApp, webchat, API, atau channel lain. Produk yang dijual berupa akses layanan digital, subscription bulanan, aktivasi agent, integrasi WhatsApp, token pemakaian, dan layanan setup/konfigurasi."
    )
    c.table(
        ["Produk", "Deskripsi", "Fitur", "Harga"],
        [
            ["Trial", "Uji coba membuat 1 AI agent.", "1 agent, GPT-4.1 Mini, sekitar 2 juta token, WhatsApp demo/testing.", "Rp0 / gratis."],
            ["Starter", "Subscription bulanan untuk 1 AI agent aktif.", "1 agent, 1 koneksi WhatsApp, sekitar 10 juta token, konfigurasi dasar.", "Ditampilkan Arthur sebelum payment link dikirim."],
            ["Pro", "Subscription bulanan untuk kebutuhan bisnis lebih besar.", "Maksimal 2 agent, 2 koneksi WhatsApp, sekitar 20 juta token, pilihan model tambahan.", "Ditampilkan Arthur sebelum payment link dikirim."],
            ["Enterprise", "Paket custom volume tinggi atau integrasi khusus.", "Jumlah agent, kuota, integrasi, dukungan operasional, dan SLA sesuai kesepakatan.", "Berdasarkan quotation/invoice."],
            ["Token Top-up", "Tambahan kuota pemakaian subscription aktif.", "Kuota token tambahan untuk memperpanjang kapasitas pemakaian agent.", "Ditampilkan Arthur sebelum payment link dikirim."],
        ],
        [70, 130, 190, 110],
    )
    c.info_box(
        "Catatan Harga",
        "Nominal final dikonfirmasi dalam percakapan dengan Arthur sebelum payment link dibuat. Arthur menyampaikan rincian paket, periode, nominal, dan total pembayaran, lalu mengirim link pembayaran Midtrans.",
        fill=(255, 247, 237),
    )

    c.new_page()
    c.h2("2. End-to-End Process Customer Memesan Layanan")
    steps = [
        ("1. Customer chat Arthur", "Customer menghubungi Arthur melalui WhatsApp atau channel chat resmi untuk membuat/mengelola AI agent."),
        ("2. Konsultasi kebutuhan", "Arthur menanyakan kebutuhan bisnis, jumlah agent, channel WhatsApp, dan kebutuhan integrasi."),
        ("3. Rekomendasi paket", "Arthur merekomendasikan Trial, Starter, Pro, Enterprise, renewal, atau token top-up sesuai kebutuhan."),
        ("4. Konfirmasi order", "Arthur menyampaikan nama paket, periode layanan, fitur, harga, dan meminta konfirmasi customer."),
        ("5. Arthur kirim link", "Setelah customer setuju, sistem membuat payment link Midtrans dan Arthur mengirim link tersebut ke customer."),
        ("6. Bayar", "Customer membuka payment link Midtrans, memilih metode pembayaran, lalu menyelesaikan pembayaran."),
        ("7. Notifikasi", "Midtrans mengirim status transaksi ke backend melalui callback/webhook pembayaran."),
        ("8. Aktivasi", "Sistem mengaktifkan subscription, token quota, agent, atau top-up sesuai transaksi berhasil."),
    ]
    for title, body in steps:
        c.card(title, [body])

    c.new_page()
    c.h2("3. Detail Proses Pembayaran")
    c.bullet(
        [
            "Customer memulai percakapan dengan Arthur melalui WhatsApp atau channel chat resmi.",
            "Arthur mengumpulkan kebutuhan customer dan merekomendasikan paket subscription, renewal, atau token top-up.",
            "Customer menyetujui paket, periode, dan nominal pembayaran yang disampaikan Arthur.",
            "Sistem membuat order dengan order ID unik, item name, quantity, periode layanan, nominal, dan data customer.",
            "Sistem membuat payment link Midtrans untuk order tersebut.",
            "Arthur mengirim payment link Midtrans ke customer di percakapan yang sama.",
            "Customer membuka link dan menyelesaikan pembayaran di halaman Midtrans menggunakan metode yang dipilih.",
            "Midtrans mengirim status transaksi ke backend: pending, settlement/success, expire, cancel, deny, atau failure.",
            "Jika pembayaran success/settlement, backend menandai invoice paid, mengaktifkan layanan, dan Arthur mengirim konfirmasi ke customer.",
            "Jika pembayaran pending, Arthur dapat mengingatkan customer untuk menyelesaikan pembayaran melalui link yang sama.",
            "Jika pembayaran gagal/expired/cancel, order ditandai gagal dan customer dapat membuat order baru.",
        ]
    )
    c.h3("Data Transaksi yang Dikaitkan")
    c.table(
        ["Data", "Keterangan"],
        [
            ["Order ID", "ID unik per transaksi untuk rekonsiliasi dengan Midtrans dan invoice internal."],
            ["Customer Data", "Nama, email/nomor WhatsApp, dan user ID internal jika customer sudah login."],
            ["Item Details", "Nama paket, periode, kuota, jumlah agent, token top-up, atau layanan custom."],
            ["Gross Amount", "Total pembayaran dalam IDR sesuai paket dan nominal yang dikonfirmasi customer ke Arthur."],
            ["Transaction Status", "Status dari Midtrans dipakai untuk aktivasi, pembatalan, atau retry pembayaran."],
        ],
        [130, 370],
    )

    c.new_page()
    c.h2("4. Proses Setelah Pembayaran Sukses")
    c.info_box(
        "Deliverable Digital",
        "Setelah transaksi berhasil, sistem tidak mengirim barang fisik. Deliverable customer berupa akses digital, aktivasi AI agent, kuota pemakaian, konfigurasi WhatsApp, dan dukungan setup sesuai paket.",
        fill=(240, 253, 244),
    )
    c.table(
        ["Tahap", "Output untuk Customer", "Estimasi"],
        [
            ["Aktivasi subscription", "Status subscription aktif, masa berlaku diperbarui, dan token quota ditambahkan.", "Otomatis setelah pembayaran berhasil."],
            ["Pembuatan/update agent", "Arthur melanjutkan pembuatan agent baru atau mengaktifkan agent existing sesuai paket.", "Otomatis melalui percakapan Arthur atau dibantu tim support."],
            ["Koneksi WhatsApp", "Arthur memberi instruksi pairing/QR atau mengarahkan customer ke dashboard koneksi WhatsApp.", "Dipandu oleh Arthur, dashboard, atau support."],
            ["Konfirmasi", "Customer menerima konfirmasi dari Arthur melalui chat, serta dapat melihat status di dashboard jika tersedia.", "Setelah aktivasi selesai."],
        ],
        [120, 250, 130],
    )

    c.h2("5. Batasan, Refund, dan Support")
    c.table(
        ["Area", "Kebijakan Operasional"],
        [
            ["Sifat Produk", "Layanan digital berbasis subscription. Tidak ada pengiriman barang fisik."],
            ["Aktivasi", "Layanan aktif setelah pembayaran berhasil dan data order valid."],
            ["Kegagalan Pembayaran", "Jika transaksi pending/gagal/expired, layanan belum diaktifkan sampai pembayaran berhasil."],
            ["Refund", "Permintaan refund ditangani manual oleh tim support sesuai status pemakaian dan kebijakan merchant."],
            ["Support", "Customer dapat menghubungi Arthur atau tim support melalui WhatsApp, dashboard, email, atau channel merchant."],
            ["Keamanan", "Data pembayaran diproses oleh Midtrans. Sistem merchant hanya menerima status transaksi dan data order yang dibutuhkan."],
        ],
        [130, 370],
    )

    c.h2("6. Ringkasan Flow Teknis")
    c.bullet(
        [
            "Customer chat dengan Arthur dan menyetujui paket subscription/top-up.",
            "Backend membuat order dan payment link Midtrans dengan item details dan gross amount.",
            "Arthur mengirim payment link Midtrans ke customer.",
            "Customer membuka link dan melakukan pembayaran di halaman Midtrans.",
            "Midtrans mengirim notification callback ke backend merchant.",
            "Backend melakukan validasi status transaksi dan mencegah duplicate processing.",
            "Jika success, backend mengaktifkan subscription/top-up dan mencatat invoice paid.",
            "Arthur mengirim konfirmasi pembayaran dan customer dapat memakai layanan Managed Agent Platform.",
        ]
    )

    c.finish()
    write_pdf(OUTPUT, c.pages)
    print(OUTPUT)


if __name__ == "__main__":
    build()
