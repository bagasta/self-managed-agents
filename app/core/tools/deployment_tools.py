"""
deployment_tools.py — LangChain tools for deploying apps from sandbox workspace.

Tools:
  deploy_app         — start persistent container + Cloudflare tunnel, return public URL
  stop_deployment    — kill containers and free resources
  get_deployment_status — check if deployment is running and retrieve URL
  get_deployment_logs   — tail app container logs for debugging
"""
from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool

from app.core.infra import deployment_service as _svc


def _deployment_ttl_label() -> str:
    seconds = _svc.deployment_ttl_seconds()
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} hari"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} jam"
    minutes = max(1, seconds // 60)
    return f"{minutes} menit"


def build_deployment_tools(session_id: str, workspace_dir: Path, sandbox_image: str) -> list:
    sid = str(session_id)
    wdir = workspace_dir
    ttl_label = _deployment_ttl_label()

    @tool
    def deploy_app(command: str, port: int = 8080) -> str:
        """
        Deploy aplikasi dari workspace ke public URL via Cloudflare tunnel.
        App berjalan di persistent Docker container yang mount /workspace.

        PENTING — DEPLOYMENT OTOMATIS MATI SETELAH 24 JAM.
        Setelah 24 jam, container di-stop otomatis untuk menghemat resource.
        Beritahu user bahwa URL hanya aktif selama ~24 jam sejak deploy.

        ALUR WAJIB untuk membuat dan deploy app baru:
        1. Tulis semua file dulu dengan write_file (workspace otomatis terbuat, JANGAN ls dulu)
        2. Panggil get_deployment_status() untuk cek apakah sudah ada deployment
        3. Jika status "running" → JANGAN deploy ulang, kembalikan URL yang ada
        4. Jika "not_deployed" → panggil deploy_app dengan command yang tepat
        5. Kembalikan URL hasil deploy_app kepada user — INI WAJIB, termasuk info 24 jam TTL

        PENTING untuk static file server (python3 -m http.server, npx serve):
        - Edit file di /workspace/ langsung terlihat tanpa restart server.
        - Jika kamu edit_file HTML/CSS/JS, TIDAK PERLU deploy_app lagi.
        - deploy_app ulang hanya jika: ganti command server, port berubah, atau dependency baru.

        CATATAN: /workspace tidak ada sebelum write_file pertama dipanggil — jangan ls sebelum menulis file.

        Args:
            command: Perintah untuk menjalankan app, contoh:
                     "pip install flask && python app.py"
                     "pip install -r requirements.txt && python server.py"
                     "python3 -m http.server 8080"
            port: Port yang digunakan app (default 8080). Harus sama dengan port di kode app.

        Returns:
            URL publik yang bisa diakses, contoh: https://xxx.trycloudflare.com
            (URL aktif selama 24 jam, setelah itu deployment otomatis berhenti)
        """
        result = _svc.deploy_app(
            session_id=sid,
            workspace_dir=wdir,
            command=command,
            port=port,
            sandbox_image=sandbox_image,
        )
        if "error" in result:
            return f"[deploy_error] {result['error']}"
        return (
            f"Deployment berhasil!\n"
            f"URL: {result['url']}\n"
            f"Status: {result['status']}\n"
            f"Command: {result['command']}\n\n"
            f"Deployment otomatis berhenti setelah {ttl_label} untuk menghemat resource.\n"
            f"Catatan: URL akan berubah jika deployment di-restart."
        )

    @tool
    def stop_deployment() -> str:
        """
        Hentikan deployment yang sedang berjalan untuk sesi ini.
        Membersihkan Docker containers dan network.
        Gunakan ini jika app perlu di-rebuild dari awal atau tidak lagi dibutuhkan.
        """
        result = _svc.stop_deployment(sid)
        return f"Deployment dihentikan. Status: {result['status']}"

    @tool
    def get_deployment_status() -> str:
        """
        Cek status deployment saat ini: apakah aktif, URL-nya apa, dan container-nya sehat.
        Gunakan ini jika user bertanya 'apakah appnya masih jalan?' atau 'link-nya apa?'
        """
        result = _svc.get_deployment_status(sid)
        status = result["status"]
        if status == "not_deployed":
            return "Belum ada deployment aktif untuk sesi ini."
        url = result.get("url") or "(URL belum tersedia)"
        return (
            f"Status: {status}\n"
            f"URL: {url}\n"
            f"Command: {result.get('command', '-')}\n"
            f"App container: {result.get('app_container', '-')}\n"
            f"CF tunnel: {result.get('cf_container', '-')}"
        )

    @tool
    def get_deployment_logs(tail: int = 50) -> str:
        """
        Ambil log terbaru dari app container untuk debugging.
        Berguna jika app crash atau tidak merespons setelah deploy.

        Args:
            tail: Jumlah baris log terakhir yang diambil (default 50)
        """
        return _svc.get_app_logs(sid, tail=tail)

    return [deploy_app, stop_deployment, get_deployment_status, get_deployment_logs]
