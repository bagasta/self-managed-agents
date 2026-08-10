# QA Production: Docker Sandbox, Deploy, dan Security Scan

**Tanggal**: 2026-05-22  
**Scope**: QA production readiness untuk penggunaan Docker sandbox, deploy container, subagent sandbox, dan security review dengan workflow Codex Security.  
**Verdict**: **NO-GO untuk production multi-tenant jika fitur sandbox/deploy dibuka untuk user umum.**

## Ringkasan Penilaian

| Area | Nilai | Catatan |
|------|-------|---------|
| Functional readiness | 8/10 | Sandbox, subagent, deploy path, dan MCP guard sudah bekerja secara fungsional. |
| Sandbox isolation readiness | 3/10 | Container masih punya network bridge, capability default, dan belum memakai runtime isolasi wajib. |
| Production SaaS security readiness | 4/10 | Aman untuk dev/internal trusted workload, belum aman untuk hostile multi-tenant workload. |
| Overall go-live | No-Go | Go-live hanya boleh jika `sandbox` dan `deploy` dimatikan untuk user umum atau dibatasi allowlist internal. |

## Temuan Utama

### 1. Critical - Sandbox Bisa Akses Network Internal Host

**Evidence runtime**:

Sandbox container berhasil connect ke:

```text
OPEN 172.17.0.1:5432
OPEN 172.17.0.1:80
```

**Lokasi kode**:

- `app/core/infra/sandbox.py`
  - `network_mode="bridge"`
  - komentar kode menyatakan full internet/no firewall.

**Risiko**:

Agent dengan `execute` bisa melakukan internal port scanning, mencoba koneksi ke DB/internal API, atau exfiltrate data melalui internet.

**Status production**: blocker.

### 2. Critical - API Container Mount Docker Socket

**Evidence**:

- `docker-compose.yml` mount `/var/run/docker.sock:/var/run/docker.sock`.

**Risiko**:

Jika API process compromise, attacker bisa mengontrol Docker daemon host. Ini setara host-level compromise dalam banyak deployment.

**Status production**: blocker.

### 3. High - Sandbox Container Belum Di-Hardening

**Evidence**:

Sandbox run belum enforce:

- `cap_drop=["ALL"]`
- `security_opt=["no-new-privileges:true"]`
- non-root user
- `read_only=True`
- `pids_limit`
- seccomp/AppArmor profile
- mandatory gVisor/Kata/Firecracker/Daytona runtime

Saat ini gVisor hanya optional lewat `DOCKER_RUNTIME`, bukan default/wajib.

**Risiko**:

Container escape risk dan resource abuse risk masih tinggi untuk workload tidak tepercaya.

**Status production**: blocker untuk public sandbox.

### 4. High - Deploy Membuka Workload Agent ke Public Internet

**Evidence**:

`deploy_app` menjalankan command agent dalam persistent container lalu membuka Cloudflare Quick Tunnel.

**Risiko**:

User-generated app menjadi public endpoint. Tanpa abuse control yang kuat, ini bisa dipakai untuk phishing, malware hosting, data exfiltration callback, atau proxy service.

**Status production**: blocker untuk deploy publik user umum.

### 5. Medium - Resource DoS Risk

**Evidence**:

Ada `max_concurrent_sandboxes`, `mem_limit`, dan `nano_cpus`, tetapi:

- concurrency check bersifat check-then-run, belum atomic.
- tidak ada `pids_limit`.
- timeout container tidak selalu enforced dari luar jika caller tidak pass timeout.
- deployment container memakai restart policy dan TTL cleanup masih tergantung lifecycle service.

**Risiko**:

Multi-request atau parallel session bisa membebani CPU, RAM, process table, network, dan disk.

## Temuan Positif

- Path traversal workspace sudah blocked.
  - Test `/workspace/../../.env` untuk `DockerSandbox` dan `DockerBackend` menghasilkan `Path traversal blocked`.
- Subagent sandbox workspace sudah terpisah dari parent.
- `shared/` per parent session membantu handoff subagent tanpa cross-session sharing.
- Google Workspace external-service fallback ke sandbox/subagent sudah di-guard.
- Deploy tools sekarang hanya muncul jika `deploy` aktif, bukan implisit dari `sandbox`.

## Verifikasi Yang Dilakukan

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_deploy_path.py \
  tests/test_subscription_service.py \
  tests/test_mcp_fallbacks.py
```

Hasil:

```text
24 passed, 2 warnings
```

Runtime network probe dari sandbox:

```text
OPEN 172.17.0.1:5432
OPEN 172.17.0.1:80
```

Path traversal probe:

```text
sandbox read: [error] Path traversal blocked: '/workspace/../../.env'
sandbox write: [error] Failed to write file: Path traversal blocked: '/workspace/../../tmp/pwned.txt'
backend read: Path traversal blocked: '/workspace/../../.env'
backend write: Path traversal blocked: '/workspace/../../tmp/pwned.txt'
```

## Production Gate

### Boleh Go-Live Jika

- `sandbox=false` dan `deploy=false` untuk semua user umum.
- Hanya MCP/non-sandbox agents yang dibuka.
- Sandbox/deploy hanya untuk allowlist internal/admin.
- Monitoring dan kill switch tersedia.

### Tidak Boleh Go-Live Jika

- User umum bisa membuat agent dengan `sandbox: true`.
- User umum bisa memakai `deploy_app`.
- API service production masih mount Docker socket langsung.
- Sandbox container masih memakai bridge network unrestricted.

## Remediation Wajib Sebelum Public Sandbox

1. **Pisahkan sandbox worker dari API**
   - API tidak boleh mount Docker socket.
   - Gunakan worker terisolasi khusus sandbox.
   - Pertimbangkan rootless Docker/Podman, gVisor, Kata, Firecracker, Daytona, atau sandbox provider dedicated.

2. **Default network deny**
   - `network_mode="none"` untuk sandbox default.
   - Jika perlu internet, lewat egress proxy dengan allowlist dan audit log.
   - Blok akses ke RFC1918/link-local/host gateway/metadata service.

3. **Container hardening**
   - `cap_drop=["ALL"]`
   - `security_opt=["no-new-privileges:true"]`
   - non-root user
   - `read_only=True`
   - tmpfs `/tmp`
   - `pids_limit`
   - seccomp/AppArmor profile
   - enforce runtime isolation, bukan optional.

4. **Resource enforcement**
   - Atomic global concurrency limiter via Redis/DB lock.
   - Per-user/per-agent sandbox quota.
   - Enforce command timeout from host side.
   - Disk quota per workspace.

5. **Deploy hardening**
   - Deploy public tunnel harus feature-flagged.
   - Per-user quota dan abuse monitoring.
   - TTL sweeper background wajib.
   - Audit command, port, URL, container ID, and owner.
   - Kill switch per deployment and global.

6. **Audit logging**
   - Log setiap `execute`, `deploy_app`, package install, outbound-denied event, and long-running command.
   - Correlate dengan `agent_id`, `session_id`, `external_user_id`, run_id.

## Rekomendasi Prioritas

| Priority | Task | Status |
|----------|------|--------|
| P0 | Disable sandbox/deploy for public users by default | Required |
| P0 | Remove Docker socket mount from production API | Required |
| P0 | Enforce `network_mode=none` or egress proxy | Required |
| P0 | Add cap drop/no-new-privileges/non-root/pids limit | Required |
| P1 | Create dedicated sandbox worker architecture | Required |
| P1 | Add deployment abuse controls and TTL sweeper | Required |
| P1 | Add sandbox security regression tests | Required |
| P2 | Persist explicit pending Google request state in session metadata | Recommended |

## Final Decision

Sandbox/deploy saat ini **siap untuk dev/internal demo**, tetapi **belum siap untuk production SaaS multi-tenant**.

Rekomendasi launch:

```text
Launch MCP + normal agents first.
Keep sandbox/deploy behind internal allowlist.
Re-open public sandbox only after P0 sandbox security gates pass.
```
