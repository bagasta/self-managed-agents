# Rencana Refactor agent_runner.py

## Status Progress Sesi Ini (27 Apr 2026)

### Sudah selesai di sesi ini:
- [x] 5.2 Sandbox hardening — `mem_limit="256m"`, `nano_cpus=0.25e9`, `network_mode="none"`, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`
- [x] 5.3 Input sanitizer — `app/core/input_sanitizer.py` (sanitize_user_input, flag_potential_injection)
- [x] 5.3 PII log redaction — `app/core/log_sanitizer.py` (redact_pii), diintegrasikan ke agent_runner.py _AgentLogger.on_tool_start
- [x] 5.3 sanitize_user_input dipanggil di channels.py wa_incoming() sebelum user_message dikirim ke agent
- [x] 4.2 Prometheus — `app/core/metrics.py` (6 custom metrics), requirements.txt ditambah prometheus-fastapi-instrumentator + prometheus-client, Instrumentator di main.py
- [x] 4.5 Sentry — sudah ada di main.py dari sesi sebelumnya (tinggal tandai checklist)
- [x] 4.6 /health/detailed — endpoint baru di main.py (cek db + scheduler + wa_service)
- [x] is_scheduler_running() — ditambah ke scheduler_service.py
- [x] 2.7 Type hints — AgentRunResult TypedDict, run_agent signature diubah agent_model: AgentModel -> AgentRunResult

### Belum selesai (lanjut sesi berikut):
- [ ] 2.1 Pecah agent_runner.py
- [ ] 2.3 Pecah wa_incoming() handler
- [ ] 5.1 Multi-key API key system
- [ ] 1.2 Dokumentasikan single-worker constraint

---

## 2.1 — Rencana Pecah agent_runner.py

### Target struktur file:

```
app/core/
  agent_runner.py          ← hanya run_agent() + helpers kecil (~200 baris)
  tool_builder.py          ← semua build_*_tools() + helpers AST
  subagent_builder.py      ← _SYSTEM_SUBAGENTS + build_subagents() + _build_system_subagent()
  prompt_builder.py        ← _build_agent_context_block() + system prompt assembly
  context_service.py       ← _build_rag_context() + _maybe_summarize_context()
```

---

### app/core/tool_builder.py

Pindahkan fungsi-fungsi ini dari agent_runner.py:

1. **helpers**: `_extract_ast_params()`, `_pip_prefix()` — dari baris ~165-211
2. **`_STDLIB_MODULES`** — `set(sys.stdlib_module_names)`
3. **`build_sandbox_binary_tool(sandbox)`** — baris 83-91
4. **`build_memory_tools(agent_id, db, scope)`** — baris 97-122
5. **`build_skill_tools(agent_id, db)`** — baris 129-155
6. **`build_tool_creator_tools(agent_id, db, sandbox)`** — baris 213-281
7. **`build_loaded_custom_tools(custom_tools_db, sandbox)`** — baris 288-296
8. **`_make_custom_tool_runner(...)`** — baris 299-343
9. **`build_whatsapp_media_tools(session, sandbox)`** — baris 350-463
10. **`build_wa_agent_manager_tools(session)`** — baris 470-563
11. **`build_http_tools(tools_config)`** — baris 805-808

**Imports yang dibutuhkan tool_builder.py:**
```python
from __future__ import annotations
import ast, json, sys, uuid
from typing import Any, Optional
import structlog
from langchain_core.tools import tool, StructuredTool
from pydantic import BaseModel, Field, create_model
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.custom_tool_service import create_or_update_custom_tool, list_custom_tools
from app.core.memory_service import (build_memory_context, delete_memory, extract_long_term_memory,
    get_memory, list_memories, upsert_memory)
from app.core.sandbox import DockerSandbox
from app.core.skill_service import create_or_update_skill, get_skill, list_skills as _list_skills
from app.config import get_settings
```

---

### app/core/subagent_builder.py

Pindahkan:
1. **`_SYSTEM_SUBAGENTS`** — list[dict], baris 574-661
2. **`_build_system_subagent(spec, parent_session_id)`** — baris 664-693
3. **`build_subagents(agent_ids, parent_session_id, db, log)`** — baris 696-798

**Imports yang dibutuhkan:**
```python
from __future__ import annotations
import uuid
from typing import Any
import structlog
from langchain_openai import ChatOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.core.sandbox import DockerSandbox
from app.core.tool_builder import build_sandbox_binary_tool, build_memory_tools, build_skill_tools, build_http_tools
```

---

### app/core/context_service.py

Pindahkan:
1. **`_build_rag_context(agent_id, user_message, db, tools_config, log)`** — baris 869-915
2. **`_maybe_summarize_context(session, db, llm, log)`** — baris 922-982
3. **`_SUMMARY_TRIGGER = 10`** — baris 922 (sekarang sudah dipindah ke settings.context_summary_trigger, tapi masih ada di file sebagai konstanta; pakai settings)

**Imports yang dibutuhkan:**
```python
from __future__ import annotations
import uuid
from typing import Any
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.models.message import Message
```

---

### app/core/prompt_builder.py

Pindahkan:
1. **`_build_agent_context_block(agent_model, session, active_groups, custom_tools_db, subagent_list, sender_name)`** — baris 989-1041

Sistem prompt assembly (baris 1187-1300 dalam run_agent) TIDAK dipindah — itu terlalu tightly coupled dengan run_agent state. Biarkan di run_agent.

**Imports yang dibutuhkan:**
```python
from __future__ import annotations
from typing import Any
from app.core.phone_utils import normalize_phone
from app.models.session import Session
```

---

### app/core/agent_runner.py (setelah refactor)

Yang tersisa di agent_runner.py setelah semua dipindah:
- Imports (lebih sedikit, hanya import dari modul baru)
- `AgentRunResult` TypedDict
- `_is_enabled()` helper
- `_normalize_phone()` — **HAPUS** (pakai normalize_phone dari phone_utils)
- `_load_history()`
- `_count_user_messages()`
- `_db_messages_to_lc()`
- `run_agent()` — fungsi utama, tetap di sini

**Imports baru di agent_runner.py:**
```python
from app.core.tool_builder import (
    build_sandbox_binary_tool, build_memory_tools, build_skill_tools,
    build_tool_creator_tools, build_loaded_custom_tools,
    build_whatsapp_media_tools, build_wa_agent_manager_tools, build_http_tools,
)
from app.core.subagent_builder import build_subagents
from app.core.prompt_builder import _build_agent_context_block
from app.core.context_service import _build_rag_context, _maybe_summarize_context
from app.core.phone_utils import normalize_phone
```

---

## 2.3 — Rencana Pecah wa_incoming()

### Target: wa_incoming() hanya ~80 baris orchestration

Extract helper functions ke bawah file channels.py (atau file channels_helpers.py jika perlu):

### Helper 1: `_find_agent_by_device_id(device_id, db) -> Agent | None`
Baris 228-250 — logic lookup agent dari device_id (termasuk wadev_ prefix handling)

### Helper 2: `_find_escalation_context(agent, db) -> tuple[str | None, str | None]`
Baris 276-318 — cari session dengan escalation_active, ambil pesan user terkini
Returns: `(escalation_user_jid, escalation_context)`

### Helper 3: `_find_or_create_session(agent, lookup_user_id, body, effective_reply_target, db) -> Session`
Baris 336-375 — cari session existing atau buat baru, update channel_config jika perlu

### Helper 4: `_process_media_attachment(body, session, log) -> tuple[str, str | None, str | None]`
Baris 382-436 — decode media bytes, simpan ke workspace, extract text dari dokumen
Returns: `(media_context, media_image_b64, media_image_mime)`

### wa_incoming() setelah refactor (~80 baris):
```python
@router.post("/wa/incoming")
async def wa_incoming(body: WAIncomingMessage, db: AsyncSession = Depends(get_db)):
    log = logger.bind(device_id=body.device_id, from_phone=body.from_)
    
    agent = await _find_agent_by_device_id(body.device_id, db)
    if not agent: raise HTTPException(404, ...)
    
    # operator detection logic (~15 baris)
    ...
    
    escalation_user_jid, escalation_context = None, None
    if is_operator:
        escalation_user_jid, escalation_context = await _find_escalation_context(agent, db)
    
    session = await _find_or_create_session(agent, lookup_user_id, body, effective_reply_target, db)
    
    media_context, media_image_b64, media_image_mime = await _process_media_attachment(body, session, log)
    
    user_message = sanitize_user_input(raw_message) + media_context
    
    # run agent + kirim reply (~30 baris)
    ...
```

---

## 5.1 — Rencana Multi-key API Key System

### Langkah:

1. **Buat `app/models/api_key.py`**:
```python
class APIKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
```

2. **Buat alembic migration**: `make migrate MSG="add api_keys table"`

3. **Update `app/deps.py`**:
```python
async def get_api_key(x_api_key: str = Header(...), db: AsyncSession = Depends(get_db)) -> APIKey:
    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
    result = await db.execute(select(APIKey).where(APIKey.key_hash == key_hash, APIKey.is_active == True))
    api_key = result.scalar_one_or_none()
    if not api_key:
        # fallback ke single key lama (backward compat selama migrasi)
        if x_api_key == settings.api_key:
            return None  # atau return dummy APIKey
        raise HTTPException(401, "Invalid API key")
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    return api_key
```

4. **Tambah endpoint CRUD** di `app/api/api_keys.py` (list, create, revoke)

5. **Update `app/models/__init__.py`** untuk include APIKey

---

## 1.2 — Dokumentasikan Single-Worker Constraint

Tambahkan docstring/komentar di `app/core/event_bus.py`:
- Jelaskan bahwa event_bus adalah in-memory asyncio pub/sub
- HARUS single-worker (--workers 1) karena tidak ada shared state antar process
- Roadmap: ganti dengan Redis pub/sub untuk multi-instance support
- Link ke production-plan/01-critical-blockers.md#12

---

## Urutan Eksekusi Sesi Berikut

1. **2.1** — Buat tool_builder.py, subagent_builder.py, context_service.py, prompt_builder.py → strip agent_runner.py
2. **2.3** — Extract helpers dari wa_incoming()
3. **5.1** — APIKey model + migration + deps.py update
4. **1.2** — Tambah komentar event_bus.py
5. Update semua checklist + recap.md
