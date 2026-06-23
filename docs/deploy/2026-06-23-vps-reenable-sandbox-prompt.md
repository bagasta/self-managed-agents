# Prompt untuk Claude di VPS — Deploy re-enable sandbox/subagent/deploy

Copy-paste blok di bawah ke Claude Code yang jalan di VPS server.

---

Kamu bekerja di server produksi managed-agents-project. Branch `feat/reenable-sandbox-vps-stable`
me-re-enable fitur sandbox/subagent/deploy/tool_creator dan memperbaiki akar instabilitas DinD.
Tugasmu: deploy branch itu dengan AMAN dan verifikasi sandbox benar-benar jalan.

## Konteks akar masalah (WAJIB paham sebelum mengubah)

App jalan di dalam container dan men-spawn container sandbox sebagai SIBLING lewat
`/var/run/docker.sock`. Bind-mount source di `docker run` di-resolve oleh daemon HOST,
bukan filesystem container app. Jika workspace di-mount sebagai NAMED VOLUME (mis.
`sandbox_data:/tmp/agent-sandboxes`), maka:
- file ops app (write/read/edit) menulis ke named volume
- `execute`/`deploy_app` (sibling container) me-mount jalur host literal yang BERBEDA & kosong
→ "file not found", run gagal acak.

Fix: workspace harus **host-bind dengan jalur IDENTIK** kiri=kanan, dan env
`SANDBOX_HOST_BASE_DIR` di-set ke jalur host itu. Kode sudah pakai `to_host_path()` untuk
menerjemahkan, tapi default-nya no-op kalau `SANDBOX_HOST_BASE_DIR` kosong.

## Langkah

1. **Ambil branch**
   - `git fetch && git checkout feat/reenable-sandbox-vps-stable && git pull`
   - JANGAN commit/ubah file lain. Kalau ada konflik working-tree, lapor dulu, jangan paksa.

2. **Inspeksi konfigurasi container app saat ini** (jangan asumsi)
   - Temukan compose file yang dipakai prod (cari service app/api yang punya
     `/var/run/docker.sock`). Cek blok `volumes:`-nya.
   - Tentukan bagaimana `SANDBOX_BASE_DIR` (`/tmp/agent-sandboxes`) di-mount:
     named volume? anonymous? host-bind? jalur host-nya apa?
   - Cek `docker inspect <app_container>` bagian `Mounts` untuk konfirmasi sumber asli.
   - LAPORKAN temuan ini sebelum mengubah apa pun.

3. **Perbaiki volume jadi host-bind jalur identik**
   - Di service app/api compose, ganti mount sandbox jadi:
     ```yaml
     volumes:
       - /tmp/agent-sandboxes:/tmp/agent-sandboxes   # host bind, jalur SAMA
       - /var/run/docker.sock:/var/run/docker.sock
     ```
     (Kalau mau jalur lain mis. `/opt/agent-sandboxes`, pakai jalur sama di kedua sisi
     dan samakan `SANDBOX_BASE_DIR` + `SANDBOX_HOST_BASE_DIR` ke jalur itu.)
   - Hapus named volume `sandbox_data` dari daftar `volumes:` top-level jika tak terpakai lagi.
   - Di host: `mkdir -p /tmp/agent-sandboxes` (atau jalur yang dipilih).

4. **Set env** (di `.env` prod atau `environment:` service)
   ```
   SANDBOX_SUBAGENTS_ENABLED=true
   SANDBOX_HOST_BASE_DIR=/tmp/agent-sandboxes
   SANDBOX_MEM_LIMIT=1g
   SANDBOX_NANO_CPUS=1000000000
   MAX_CONCURRENT_SANDBOXES=6
   ```
   `SANDBOX_HOST_BASE_DIR` HARUS == jalur host yang di-bind di langkah 3.

5. **Pastikan image sandbox ada di host daemon**
   - `docker images | grep managed-agents-sandbox` — kalau tidak ada, build:
     `docker build -f sandbox.Dockerfile -t managed-agents-sandbox:latest .`
   - Ini sibling di daemon host, jadi image harus ada di host, bukan cuma di dalam app container.

6. **Restart & cek startup**
   - Rebuild/restart service app. Tail log; pastikan tidak ada error import/lifespan.
   - Cek reaper loop terpasang (log `sandbox_reaper` muncul saat ada yang dibersihkan; tidak error).

7. **Smoke test sandbox (BUKTI, bukan asumsi)** — ini gerbang sukses:
   - Buat/gunakan agent dengan `tools_config.sandbox=true`, kirim 1 pesan yang memaksa:
     tulis file via tool lalu baca/`execute` file yang sama (mis. "buat file /workspace/tmp/ping.txt
     berisi 'ok', lalu cat file itu").
   - HARUS: file yang ditulis terbaca oleh `execute` (output 'ok'). Kalau "file not found",
     berarti host-bind/SANDBOX_HOST_BASE_DIR belum benar — perbaiki, jangan lanjut.
   - Verifikasi di host: `ls /tmp/agent-sandboxes/<session_id>/tmp/ping.txt` ADA.

8. **Smoke test deploy (kalau dipakai)**
   - Agent dengan `deploy=true`: minta deploy static page sederhana, pastikan dapat URL
     `*.trycloudflare.com` yang hidup.

## Aturan
- Jangan klaim sukses tanpa output smoke test langkah 7 yang lulus.
- Kalau named-volume tidak bisa diubah (constraint infra), STOP dan lapor — jangan biarkan
  fix jadi no-op diam-diam.
- Rollback cepat bila perlu: set `SANDBOX_SUBAGENTS_ENABLED=false` lalu restart (kill switch).
