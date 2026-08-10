# Arthur SOP & Production Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Membuat SOP yang dibuat Arthur benar-benar otoritatif di runtime (di-enforce, bukan himbauan), persisten penuh, dan kuota biaya terkendali — tanpa mengubah bagian yang sudah benar.

**Architecture:** Perbaikan bedah di titik spesifik: (1) gate kuota token pre-run, (2) hard tool-gating berdasarkan maturity SOP di tool-setup runtime, (3) validator delivery selaras kontrak parent-delivery, (4) persistensi SOP penuh via kolom JSONB `artifact`, (5) ubah kegagalan baca SOP dari swallow senyap → sinyal eksplisit, (6) fallback SOP memaksa `needs_review`. Setiap perubahan additive/guarded; tidak menyentuh jalur yang sudah teruji.

**Tech Stack:** Python 3 / FastAPI / LangGraph / SQLAlchemy async / Alembic / PostgreSQL (JSONB) / pytest.

---

## INVARIANTS — JANGAN DIUBAH (sudah benar, lindungi)

Setiap task WAJIB mempertahankan perilaku berikut. Kalau sebuah perubahan memaksa salah satu ini berubah, STOP dan minta review.

1. **WA identity / LID guard** — `app/core/utils/wa_identity.py` (`is_probable_whatsapp_lid`, `resolve_auto_provision_external_id`). Logika `>15 digit` / `@lid` = LID dan WA wajib `phone_number` nyata. Jangan diubah.
2. **Tool Capability Registry + reply_guard** — `app/core/engine/tool_capability_registry.py`, `app/core/engine/reply_guard.py`. Pencegahan klaim halu & runtime tool contract sudah benar. Boleh DIPAKAI/diperluas, jangan dilonggarkan.
3. **Memory scoping** — `app/core/domain/memory_service.py`. `soul` global (`scope=None`), sisanya scoped `external_user_id`. Jangan ubah aturan scoping.
4. **Escalation routing & draft-confirm-send** — `app/api/channels.py` (operator session, owner/operator routing, `escalate_to_human` gate sebelum `reply_to_user`/`send_to_number`). Jangan ubah alur eskalasi.
5. **`max_agents` enforcement** — `app/core/domain/subscription_service.py:343`. Sudah benar. Task B1 hanya MENAMBAH gate kuota token, tidak menyentuh gate jumlah agent.
6. **Preset catalog & tool bundles** — `AGENT_PRESETS` di `builder_tools.py`. Bundle tool per preset sudah benar; jangan ubah komposisinya.
7. **Pipeline compose Arthur** (`plan_agent`/`compose_*`/`create_agent`/`update_agent`/`verify_agent`) tetap ada dengan signature sama. Plan ini memperbaiki output & enforcement-nya, BUKAN merombak tool surface (refactor pipeline ramping = plan terpisah, di luar scope ini).

Regression guard: sebelum mulai, jalankan baseline dan catat hasil:
```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_builder_tools.py tests/test_reply_guard.py tests/test_whatsapp_progress.py tests/test_whatsapp_direct_send.py -q
```
Expected: semua PASS (baseline ~105 + dst). Angka ini jadi acuan "tidak merusak yang benar".

---

## File Structure

| File | Tanggung jawab | Task |
|---|---|---|
| `app/core/domain/subscription_service.py` | Tambah `assert_token_quota_available()` (read-only check) | B1 |
| `app/core/engine/agent_runner.py` | Panggil gate kuota sebelum run; panggil hard-gating; ubah swallow→log | B1, A1, A3 |
| `app/core/engine/agent_tool_setup.py` | Cabut tool aksi-final saat SOP draft/needs_review | A1 |
| `app/core/engine/sop_runtime_gate.py` (BARU) | Logika murni: tool mana yang boleh saat maturity tertentu | A1 |
| `app/core/tools/builder_tools.py` | Ganti validator `send_whatsapp_document` → kontrak parent-delivery; fallback SOP set needs_review | A4, A5 |
| `app/core/domain/agent_sop_service.py` | Persist/baca kolom `artifact` penuh; logging kegagalan | A2, A3 |
| `app/models/agent_operating_manual.py` | Kolom `artifact JSONB` | A2 |
| `alembic/versions/019_*.py` (BARU) | Migrasi + backfill `artifact` | A2 |
| `tests/test_*` | Test per task | semua |

---

## Task B1: Gate kuota token pre-run (read-only, additive)

**Files:**
- Modify: `app/core/domain/subscription_service.py`
- Modify: `app/core/engine/agent_runner.py` (di awal entrypoint run, sebelum LLM dipanggil — verifikasi titiknya di sekitar pemuatan agent/subscription)
- Test: `tests/test_subscription_service.py`

- [ ] **Step 1: Tulis test gagal**

```python
# tests/test_subscription_service.py
import pytest
from app.core.domain.subscription_service import assert_token_quota_available, QuotaExceeded

class _Sub:
    def __init__(self, used, quota, grace_until=None, status="active"):
        self.tokens_used = used
        self.token_quota = quota
        self.grace_until = grace_until
        self.status = status

def test_quota_allows_when_under_limit():
    assert_token_quota_available(_Sub(used=10, quota=100))  # no raise

def test_quota_blocks_when_at_or_over_limit():
    with pytest.raises(QuotaExceeded):
        assert_token_quota_available(_Sub(used=100, quota=100))

def test_quota_none_quota_is_unlimited():
    assert_token_quota_available(_Sub(used=10**12, quota=None))  # tier_3 unlimited
```

- [ ] **Step 2: Jalankan, pastikan gagal**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_subscription_service.py -k quota -q`
Expected: FAIL (`ImportError: cannot import name 'assert_token_quota_available'`)

- [ ] **Step 3: Implementasi minimal di subscription_service.py**

```python
class QuotaExceeded(Exception):
    """Token quota habis untuk subscription ini."""

def assert_token_quota_available(subscription) -> None:
    """Read-only. Raise QuotaExceeded kalau tokens_used >= token_quota.
    token_quota None = unlimited (tier_3)."""
    quota = getattr(subscription, "token_quota", None)
    if quota is None:
        return
    used = int(getattr(subscription, "tokens_used", 0) or 0)
    if used >= int(quota):
        raise QuotaExceeded(
            f"Token quota habis ({used}/{quota}). Owner perlu upgrade plan atau tunggu reset."
        )
```

- [ ] **Step 4: Jalankan, pastikan lulus**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_subscription_service.py -k quota -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Pasang gate di agent_runner**

Cari titik tepat sebelum LLM dijalankan (di mana subscription/agent dimuat — verifikasi lokasi, lihat sekitar `agent_runner.py` tempat run_record dibuat). Tambahkan:

```python
from app.core.domain.subscription_service import assert_token_quota_available, QuotaExceeded
# ... setelah subscription dimuat, sebelum membangun/menjalankan agent:
try:
    if subscription is not None:
        assert_token_quota_available(subscription)
except QuotaExceeded as exc:
    # Jangan crash; balas sopan & jangan bakar token LLM.
    return _quota_blocked_result(str(exc))  # reuse pola balasan non-crash yang sudah ada
```

CATATAN: jangan ubah pencatatan `tokens_used` pasca-run yang sudah ada. Gate ini hanya MENCEGAH run baru saat sudah lewat batas. Hormati `grace_until` bila ada (kalau `grace_until` di masa depan, izinkan).

- [ ] **Step 6: Test integrasi gate (mock subscription over-quota → tidak ada panggilan LLM)**

Tambahkan test yang memverifikasi entrypoint mengembalikan pesan blokir tanpa memanggil LLM saat over-quota. Gunakan pola mock yang sudah dipakai test runner lain di `tests/`.

- [ ] **Step 7: Commit**

```bash
git add app/core/domain/subscription_service.py app/core/engine/agent_runner.py tests/test_subscription_service.py
git commit -m "feat: enforce token quota pre-run to cap LLM cost"
```

---

## Task A1: Hard tool-gating berdasarkan maturity SOP

Tujuan: saat SOP `maturity ∈ {draft, needs_review}` atau `owner_review_required=True`, tool aksi-final (`send_whatsapp_document`, `send_whatsapp_image`, dan tool kirim/komit final lain) **dicabut fisik** dari tool list runtime. Sisakan intake/recall/escalate. Ini menjadikan himbauan teks di `prompt_builder.py:199-206` sebagai enforcement nyata.

**Files:**
- Create: `app/core/engine/sop_runtime_gate.py`
- Modify: `app/core/engine/agent_tool_setup.py`
- Test: `tests/test_sop_runtime_gate.py`

- [ ] **Step 1: Tulis test gagal untuk logika murni**

```python
# tests/test_sop_runtime_gate.py
from app.core.engine.sop_runtime_gate import gated_tool_names, is_sop_locked

def test_locked_when_draft():
    assert is_sop_locked({"maturity": "draft"}) is True
    assert is_sop_locked({"maturity": "needs_review"}) is True
    assert is_sop_locked({"maturity": "usable"}) is False
    assert is_sop_locked({"owner_review_required": True, "maturity": "usable"}) is True
    assert is_sop_locked(None) is True  # tidak ada SOP = terkunci

def test_gated_tools_removed_when_locked():
    names = {"recall", "remember", "escalate_to_human", "reply_to_user",
             "send_whatsapp_document", "send_whatsapp_image"}
    kept = gated_tool_names(names, sop={"maturity": "draft"})
    assert "send_whatsapp_document" not in kept
    assert "send_whatsapp_image" not in kept
    assert "escalate_to_human" in kept  # eskalasi tetap boleh
    assert "recall" in kept

def test_no_gating_when_usable():
    names = {"send_whatsapp_document", "recall"}
    kept = gated_tool_names(names, sop={"maturity": "usable", "owner_review_required": False})
    assert kept == names
```

- [ ] **Step 2: Jalankan, pastikan gagal**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_sop_runtime_gate.py -q`
Expected: FAIL (`ModuleNotFoundError: app.core.engine.sop_runtime_gate`)

- [ ] **Step 3: Implementasi sop_runtime_gate.py**

```python
from __future__ import annotations
from typing import Any, Iterable

# Tool aksi-final yang tidak boleh dipanggil sebelum SOP matang.
FINAL_ACTION_TOOLS: frozenset[str] = frozenset({
    "send_whatsapp_document",
    "send_whatsapp_image",
})

def is_sop_locked(sop: dict[str, Any] | None) -> bool:
    """SOP belum boleh aksi final: tidak ada SOP, draft/needs_review, atau owner_review_required."""
    if not isinstance(sop, dict):
        return True
    if bool(sop.get("owner_review_required")):
        return True
    return str(sop.get("maturity") or "").lower() in {"draft", "needs_review", "missing"}

def gated_tool_names(tool_names: Iterable[str], *, sop: dict[str, Any] | None) -> set[str]:
    names = {str(n) for n in tool_names}
    if not is_sop_locked(sop):
        return names
    return names - FINAL_ACTION_TOOLS
```

- [ ] **Step 4: Jalankan, pastikan lulus**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_sop_runtime_gate.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Terapkan di agent_tool_setup.py**

Setelah tool list final dirakit (titik yang sama tempat `deploy` saat ini dicabut, sekitar line 259-263), tambahkan penyaringan berbasis SOP. Ambil SOP dari `getattr(agent_model, "_runtime_operating_manual", None)` (di-set oleh `agent_runner.py:1425`). PENTING: jangan gate Arthur sendiri (builder) — hanya agent bisnis. Skip kalau `"builder" in capabilities`.

```python
from app.core.engine.sop_runtime_gate import gated_tool_names, is_sop_locked
# ... setelah tools dirakit, sebelum dikembalikan:
caps = getattr(agent_model, "capabilities", None) or []
if "builder" not in caps and "system" not in caps:
    sop = getattr(agent_model, "_runtime_operating_manual", None)
    if is_sop_locked(sop):
        before = len(tools)
        keep = gated_tool_names({t.name for t in tools}, sop=sop)
        tools = [t for t in tools if t.name in keep]
        logger.info("agent_tool_setup.sop_locked_tools_removed",
                    removed=before - len(tools), maturity=(sop or {}).get("maturity"))
```

VERIFIKASI: nama atribut tool (`t.name`) dan variabel `tools`/`logger` sesuai konteks file aktual. Sesuaikan bila berbeda.

- [ ] **Step 6: Test integrasi tool-setup (agent draft → tool media hilang; Arthur → tidak terpengaruh)**

Tambah test di `tests/test_agent_tool_setup*` (buat bila belum ada) memakai agent_model palsu dengan `_runtime_operating_manual={"maturity":"draft"}` dan caps biasa → assert `send_whatsapp_document` tidak ada; lalu caps `["builder"]` → assert tetap ada.

- [ ] **Step 7: Commit**

```bash
git add app/core/engine/sop_runtime_gate.py app/core/engine/agent_tool_setup.py tests/test_sop_runtime_gate.py
git commit -m "feat: hard-gate final-action tools when agent SOP not mature"
```

---

## Task A4: Validator delivery selaras kontrak parent-delivery

Ganti syarat "instructions wajib menyebut `send_whatsapp_document`" (di `builder_tools.py:1845-1846`, dan pola serupa ~3732, ~4761, ~5061) dengan validator kontrak parent-delivery. Hapus dorongan menulis instruksi membingungkan hanya demi lolos validasi.

**Files:**
- Modify: `app/core/tools/builder_tools.py`
- Test: `tests/test_builder_tools.py`

- [ ] **Step 1: Tulis test gagal untuk helper validator baru**

```python
# tests/test_builder_tools.py  (tambahkan)
from app.core.tools.builder_tools import file_delivery_contract_issues

def test_parent_delivery_contract_ok():
    instr = ("Subagent simpan ke /workspace/shared/hasil.pdf, return SIAP_DIKIRIM_PARENT. "
             "Subagent tidak boleh kirim WhatsApp. Parent kirim via send_whatsapp_document.")
    assert file_delivery_contract_issues(instr, file_delivery=True) == []

def test_parent_delivery_contract_missing_markers():
    issues = file_delivery_contract_issues("Kirim file ke customer.", file_delivery=True)
    assert issues  # ada keluhan marker hilang

def test_no_file_delivery_means_no_issue():
    assert file_delivery_contract_issues("CS biasa tanpa file.", file_delivery=False) == []
```

- [ ] **Step 2: Jalankan, pastikan gagal**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_builder_tools.py -k delivery -q`
Expected: FAIL (`ImportError: file_delivery_contract_issues`)

- [ ] **Step 3: Implementasi helper**

```python
def file_delivery_contract_issues(instructions: str, *, file_delivery: bool) -> list[str]:
    """Validasi kontrak parent-delivery untuk agent yang menghasilkan file.
    Kontrak benar: subagent tulis ke /workspace/shared, return SIAP_DIKIRIM_PARENT,
    subagent tidak kirim WA, parent yang memanggil media-send."""
    if not file_delivery:
        return []
    text = (instructions or "").lower()
    issues: list[str] = []
    if "/workspace/shared" not in text:
        issues.append("Instruksi file harus menyuruh subagent menyimpan ke /workspace/shared/<file>.")
    if "siap_dikirim_parent" not in text:
        issues.append("Instruksi file harus mewajibkan subagent return penanda SIAP_DIKIRIM_PARENT.")
    parent_sends = ("send_whatsapp_document" in text) or ("send_whatsapp_image" in text)
    if not parent_sends:
        issues.append("Instruksi harus menyebut parent memanggil send_whatsapp_document/send_whatsapp_image setelah artifact kembali.")
    return issues
```

- [ ] **Step 4: Jalankan, pastikan lulus**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_builder_tools.py -k delivery -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Ganti pemanggil lama**

Di `builder_tools.py:1845-1846` dan titik validasi serupa (~3732, ~4761 dalam `validate_agent_config`, ~5061 dalam `create_agent`): ganti cek `if "send_whatsapp_document" not in instructions: errors.append(...)` menjadi:

```python
errors.extend(file_delivery_contract_issues(instructions, file_delivery=<file_delivery_flag>))
```

`<file_delivery_flag>` = sinyal "agent menghasilkan file" yang sudah dipakai di sekitar kode itu (variabel `file_delivery` sudah ada di beberapa cabang, lihat ~2679). Kalau tidak ada di scope, turunkan dari preset/tools_config (`whatsapp_media` aktif + workflow file). Jangan paksa cek pada agent non-file.

- [ ] **Step 6: Jalankan suite builder penuh, pastikan tidak ada regresi**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_builder_tools.py -q`
Expected: PASS (>= baseline; sesuaikan test lama yang mengasumsikan pesan error string lama bila ada)

- [ ] **Step 7: Commit**

```bash
git add app/core/tools/builder_tools.py tests/test_builder_tools.py
git commit -m "feat: validate parent-delivery contract instead of bare send_whatsapp_document mention"
```

---

## Task A2: Persistensi SOP penuh via kolom `artifact` JSONB + backfill

Hentikan kehilangan `human_approval_points`, `validation_checklist`, `state_plan`, `knowledge_plan`, dan `escalation_rules` level-atas saat runtime membaca row DB.

**Files:**
- Modify: `app/models/agent_operating_manual.py`
- Create: `alembic/versions/019_agent_operating_manual_artifact.py`
- Modify: `app/core/domain/agent_sop_service.py` (`upsert_agent_operating_manual`, `operating_manual_row_to_artifact`)
- Test: `tests/test_memory_service.py` atau buat `tests/test_agent_sop_service.py`

- [ ] **Step 1: Tulis test gagal (round-trip preservasi field)**

```python
# tests/test_agent_sop_service.py
from app.core.domain.agent_sop_service import (
    normalize_agent_operating_manual, operating_manual_row_to_artifact,
)
from app.models.agent_operating_manual import AgentOperatingManual

def test_artifact_roundtrip_preserves_full_fields():
    manual = normalize_agent_operating_manual({
        "maturity": "usable",
        "workflows": [{"workflow_id": "wf1", "name": "Order"}],
        "validation_checklist": ["cek pembayaran"],
        "human_approval_points": [{"step": 3, "who": "operator"}],
        "state_plan": {"keys": ["order_status"]},
    })
    row = AgentOperatingManual(agent_id=__import__("uuid").uuid4())
    # simulasikan apa yang ditulis upsert (panggil fungsi yang sama bila refactor jadi pure)
    row.artifact = manual
    out = operating_manual_row_to_artifact(row)
    assert out["validation_checklist"] == ["cek pembayaran"]
    assert out["human_approval_points"][0]["who"] == "operator"
    assert out["state_plan"]["keys"] == ["order_status"]
```

- [ ] **Step 2: Jalankan, pastikan gagal**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_agent_sop_service.py -q`
Expected: FAIL (`AttributeError: 'AgentOperatingManual' object has no attribute 'artifact'`)

- [ ] **Step 3: Tambah kolom di model**

```python
# app/models/agent_operating_manual.py
artifact: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
```

- [ ] **Step 4: Migrasi + backfill**

```python
# alembic/versions/019_agent_operating_manual_artifact.py
"""add artifact jsonb to agent_operating_manuals + backfill"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "019_agent_operating_manual_artifact"
down_revision = "018_agent_operating_manuals"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("agent_operating_manuals",
        sa.Column("artifact", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="{}"))
    # Backfill: artifact = artifact penuh dari agents.tools_config->operating_manual bila ada,
    # else komposisi dari kolom sempit yang ada.
    op.execute("""
        UPDATE agent_operating_manuals m
        SET artifact = COALESCE(a.tools_config->'operating_manual',
                                jsonb_build_object(
                                  'source', m.source, 'domain', m.domain,
                                  'domain_confidence', m.domain_confidence,
                                  'maturity', m.maturity,
                                  'owner_review_required', m.owner_review_required,
                                  'missing_context', to_jsonb(m.missing_context),
                                  'assumptions', to_jsonb(m.assumptions),
                                  'workflows', to_jsonb(m.workflows)))
        FROM agents a
        WHERE m.agent_id = a.id
    """)

def downgrade() -> None:
    op.drop_column("agent_operating_manuals", "artifact")
```

VERIFIKASI: nama tabel (`agent_operating_manuals`) dan `down_revision` sesuai file `018_*.py` aktual.

- [ ] **Step 5: Tulis artifact penuh saat upsert; baca dari artifact**

Di `upsert_agent_operating_manual` (sekitar :900-933) set `row.artifact = normalized` (selain kolom sempit yang tetap diisi untuk query/index). Di `operating_manual_row_to_artifact` (:861-876) kembalikan `row.artifact` penuh bila ada, fallback ke proyeksi kolom sempit hanya bila `artifact` kosong (row lama pra-migrasi).

```python
# operating_manual_row_to_artifact
if isinstance(row.artifact, dict) and row.artifact:
    return dict(row.artifact)
# ... (proyeksi lama tetap sebagai fallback)
```

- [ ] **Step 6: Jalankan test + migrasi dev**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_agent_sop_service.py -q` → PASS
Run (dev DB): `make upgrade` → migrasi 019 sukses.

- [ ] **Step 7: Commit**

```bash
git add app/models/agent_operating_manual.py alembic/versions/019_agent_operating_manual_artifact.py app/core/domain/agent_sop_service.py tests/test_agent_sop_service.py
git commit -m "feat: persist full SOP artifact (JSONB) to stop lossy runtime read"
```

---

## Task A3: Kegagalan baca SOP → log eksplisit, bukan swallow senyap

**Files:**
- Modify: `app/core/domain/agent_sop_service.py` (`get_latest_agent_operating_manual`, :879-897)
- Test: `tests/test_agent_sop_service.py`

- [ ] **Step 1: Tulis test gagal (exception di-log + tetap fallback)**

```python
import logging
import pytest
from app.core.domain import agent_sop_service

@pytest.mark.asyncio
async def test_sop_read_failure_is_logged(caplog):
    class _DB:
        async def execute(self, *a, **k):
            raise RuntimeError("db boom")
    with caplog.at_level(logging.ERROR):
        out = await agent_sop_service.get_latest_agent_operating_manual(
            __import__("uuid").uuid4(), _DB(), fallback_tools_config={"operating_manual": {"maturity": "usable"}})
    assert any("sop" in r.message.lower() or "operating_manual" in r.message.lower() for r in caplog.records)
    assert out is not None  # tetap fallback agar runtime jalan
```

- [ ] **Step 2: Jalankan, pastikan gagal**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_agent_sop_service.py -k logged -q`
Expected: FAIL (tidak ada log error)

- [ ] **Step 3: Ganti `except Exception: pass` jadi log + fallback**

```python
import structlog
logger = structlog.get_logger(__name__)
# ...
    except Exception as exc:
        logger.error("agent_sop_service.operating_manual_read_failed",
                     agent_id=str(agent_id), error=str(exc))
    return get_agent_operating_manual(fallback_tools_config)
```

Jangan ubah kontrak return (tetap fallback ke tools_config). Hanya tambahkan visibilitas.

- [ ] **Step 4: Jalankan, pastikan lulus**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_agent_sop_service.py -k logged -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/domain/agent_sop_service.py tests/test_agent_sop_service.py
git commit -m "fix: log SOP DB read failures instead of swallowing silently"
```

---

## Task A5: Fallback SOP generik memaksa `needs_review`

Saat `create_agent` jatuh ke `_fallback_agent_blueprint`/`_fallback_agent_instructions` (output writer tak bisa dipulihkan), tandai SOP `needs_review` + `owner_review_required=True` agar tidak go-live diam-diam sebagai "usable".

**Files:**
- Modify: `app/core/tools/builder_tools.py` (jalur fallback di `create_agent`, sekitar :5060-5080 setelah `_fallback_agent_blueprint` dipakai)
- Test: `tests/test_builder_tools.py`

- [ ] **Step 1: Tulis test gagal**

```python
from app.core.tools.builder_tools import mark_manual_needs_review_if_fallback

def test_fallback_manual_forced_needs_review():
    m = mark_manual_needs_review_if_fallback({"maturity": "usable"}, used_fallback=True)
    assert m["maturity"] == "needs_review"
    assert m["owner_review_required"] is True

def test_non_fallback_unchanged():
    m = mark_manual_needs_review_if_fallback({"maturity": "usable", "owner_review_required": False}, used_fallback=False)
    assert m["maturity"] == "usable"
```

- [ ] **Step 2: Jalankan, pastikan gagal**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_builder_tools.py -k fallback_manual -q`
Expected: FAIL (ImportError)

- [ ] **Step 3: Implementasi helper + panggil di jalur fallback**

```python
def mark_manual_needs_review_if_fallback(manual: dict, *, used_fallback: bool) -> dict:
    if used_fallback and isinstance(manual, dict):
        manual = dict(manual)
        manual["maturity"] = "needs_review"
        manual["owner_review_required"] = True
    return manual
```

Di `create_agent`, set flag `used_fallback=True` di cabang yang memanggil `_fallback_agent_blueprint`, lalu sebelum persist SOP: `operating_manual_input = mark_manual_needs_review_if_fallback(operating_manual_input, used_fallback=used_fallback)`.

- [ ] **Step 4: Jalankan, pastikan lulus**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/test_builder_tools.py -k fallback_manual -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/tools/builder_tools.py tests/test_builder_tools.py
git commit -m "feat: force needs_review when SOP is built from silent fallback"
```

---

## Task FINAL: Regression gate & smoke

- [ ] **Step 1: Jalankan suite terfokus (harus >= baseline INVARIANTS)**

Run:
```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_builder_tools.py tests/test_reply_guard.py \
  tests/test_subscription_service.py tests/test_agent_sop_service.py \
  tests/test_sop_runtime_gate.py tests/test_whatsapp_progress.py \
  tests/test_whatsapp_direct_send.py -q
```
Expected: semua PASS. Bandingkan jumlah dengan baseline awal — tidak boleh ada yang hilang/berubah-merah selain test yang sengaja diupdate.

- [ ] **Step 2: Smoke manual (staging) — preservasi yang sudah benar**

- `make upgrade` (migrasi 019 sukses).
- `python scripts/seed_arthur.py` (Arthur ter-seed, tidak ter-gate sendiri).
- WA smoke: Arthur buat 1 agent bisnis berbasis preset `cs_whatsapp_basic` → cek balasan onboarding WA (INVARIANT #2 reply_guard masih jalan).
- Buat agent generated-file (`data_analyst_agent`) dengan SOP draft → verifikasi `send_whatsapp_document` TIDAK callable sampai SOP `usable` (A1), lalu set usable → tool muncul.
- Over-quota subscription → run diblok tanpa bakar token (B1).

- [ ] **Step 3: Commit catatan smoke (opsional)**

```bash
git add docs/superpowers/plans/2026-06-03-arthur-sop-remediation.md
git commit -m "docs: Arthur SOP remediation plan + smoke checklist"
```

---

## Self-Review (sudah dijalankan penulis plan)

- **Spec coverage:** B1↔kuota, A1↔hard-gating, A4↔validator delivery, A2↔persistensi penuh, A3↔log kegagalan, A5↔fallback needs_review. Semua temuan QA P0/P1 terpetakan. (Refactor pipeline ramping = plan terpisah, sengaja di luar scope agar tidak mengubah yang sudah benar.)
- **Placeholder scan:** tidak ada "TODO/TBD"; setiap step kode menyertakan kode. Titik yang butuh verifikasi konteks (signature/atribut) ditandai eksplisit "VERIFIKASI".
- **Type consistency:** `is_sop_locked`/`gated_tool_names`/`FINAL_ACTION_TOOLS` konsisten antar task; `assert_token_quota_available`/`QuotaExceeded` konsisten; `file_delivery_contract_issues`, `mark_manual_needs_review_if_fallback`, `artifact` dipakai dengan nama sama di test dan implementasi.
