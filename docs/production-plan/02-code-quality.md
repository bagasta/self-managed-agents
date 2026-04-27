# Fase 2 — Code Quality & Maintainability

Masalah di sini tidak akan crash production hari ini, tapi akan memperlambat development,
menyebabkan bug tersembunyi yang sulit di-trace, dan mempersulit onboarding engineer baru.

---

## 2.1 Pecah `agent_runner.py` (1477 baris)

### Masalah
File ini adalah God Object. Satu file menampung semua logika inti:
tool builders, prompt assembly, sub-agent builder, RAG, context summarizer, main runner.

Efek nyata:
- Sulit menemukan bug karena harus scroll 1400+ baris
- Circular import workaround (import di dalam fungsi) karena dependencies tidak diorganisir
- Tidak bisa di-test unit karena semua tersambung ke satu fungsi besar

### Solusi: Pecah jadi modul-modul kecil

```
app/core/
  agent_runner.py          ← hanya run_agent() + _load_history() + helper kecil (~200 baris)
  prompt_builder.py        ← _build_agent_context_block(), system prompt assembly
  tool_builder.py          ← semua build_*_tools() functions
  subagent_builder.py      ← build_subagents(), _build_system_subagent(), _SYSTEM_SUBAGENTS
  context_service.py       ← _build_rag_context(), _maybe_summarize_context()
```

Target: tidak ada file di `app/core/` yang melebihi 400 baris.

### Langkah Refactor (aman, tidak breaking)

1. Buat file baru dengan fungsi yang dipindah
2. Di `agent_runner.py`, tambahkan `from app.core.tool_builder import build_memory_tools` etc.
3. Jalankan manual test (kirim pesan ke agent) untuk verifikasi
4. Hapus definisi lama dari `agent_runner.py`

---

## 2.2 Dedup `_normalize_phone()`

### Masalah
Fungsi identik didefinisikan 4 kali:
- `agent_runner.py:76` — `_normalize_phone`
- `channels.py:54` — `_norm`
- `channels.py:258` — `_normalize_phone` (lagi)
- `wa_dev_operator_route` — inline lambda `_norm`

### Solusi

```python
# app/core/phone_utils.py
def normalize_phone(phone: str) -> str:
    """Strip leading '+' and '@domain' suffix from WhatsApp JID or phone number."""
    return phone.lstrip("+").split("@")[0]
```

Ganti semua 4 definisi dengan import dari sini.

---

## 2.3 Pecah `wa_incoming()` Handler (325 baris)

### Masalah
Satu endpoint handler mengurus terlalu banyak hal sekaligus.

### Solusi: Extrak helper functions

```python
# Sebelum: semua inline di wa_incoming()

# Sesudah: pecah jadi fungsi-fungsi kecil yang bisa di-test

async def _find_agent_by_device(device_id: str, db) -> Agent | None: ...

async def _find_or_create_session(agent, from_phone, chat_id, device_id, db) -> Session: ...

async def _find_escalation_context(agent, db) -> tuple[str | None, str | None]: ...

async def _process_media(body: WAIncomingMessage, session_id) -> tuple[str, str | None, str | None]: ...

async def _send_reply(device_id, reply_target, reply, is_operator, ...) -> None: ...
```

Target: `wa_incoming()` hanya berisi orchestration flow (~80 baris), bukan implementasi detail.

---

## 2.4 Pindahkan Magic Numbers ke Config

### Masalah

```python
# agent_runner.py:922
_SUMMARY_TRIGGER = 10  # tidak bisa dikonfigurasi tanpa ubah kode

# agent_runner.py:608, 622, 657
"model": "openai/gpt-4o-mini"  # hardcoded di 4 tempat

# channels.py:423
MAX_CHARS = 12000  # tidak bisa di-tune

# agent_runner.py:679, 784
max_tokens=4096  # hardcoded
```

### Solusi

```python
# app/config.py — tambahkan:
context_summary_trigger: int = 10        # summarize setelah N user messages
default_subagent_model: str = "openai/gpt-4o-mini"
default_subagent_max_tokens: int = 4096
media_doc_max_chars: int = 12000
llm_max_tokens: int = 4096
```

---

## 2.5 Perbaiki Error Handling yang Tidak Konsisten

### Masalah

Ada dua pola `except Exception: pass` yang masih tersisa:

```python
# channels.py:485
try:
    await send_wa_message(body.device_id, effective_reply_target, _GENERIC_ERROR_MSG)
except Exception:
    pass  # ← ini, saat kirim error message ke user gagal — setidaknya log warning

# agent_runner.py:1361
try:
    text = response.generations[0][0].text[:200]
except Exception:
    pass  # ← ini acceptable karena ini hanya untuk logging preview
```

### Solusi

Buat aturan: `except Exception: pass` **hanya** boleh ada kalau:
1. Ada komentar yang menjelaskan kenapa silent OK
2. Atau ganti dengan `except Exception as exc: log.debug(...)`

Tambahkan grep ke CI/pre-commit:
```bash
# Cegah `except Exception: pass` baru tanpa komentar
grep -rn "except Exception:\s*$" app/ | grep -v "# noqa"
```

---

## 2.6 Hilangkan Import di Dalam Fungsi

### Masalah
Ada 15+ import di dalam body fungsi/endpoint di `channels.py` dan `agent_runner.py`.
Ini biasanya tanda circular import yang di-workaround.

```python
# channels.py — contoh
async def wa_incoming(...):
    ...
    from app.core.agent_runner import run_agent      # line 451
    from app.core.wa_client import send_wa_message   # line 472
    from app.core.wa_client import send_wa_message   # line 483 (duplikat!)
    from app.core.text_utils import markdown_to_wa   # line 512
    from app.core.wa_client import send_wa_message   # line 513 (duplikat lagi!)
```

### Solusi

1. Audit dependency graph: mana yang circular, mana yang hanya malas di-pindah
2. Pindahkan import ke top-level jika tidak circular
3. Untuk yang genuinely circular: reorganisasi module sehingga tidak perlu circular

Quick win: `send_wa_message` di-import 3x di fungsi yang sama — pindahkan ke top-level.

---

## 2.7 Tambah Type Hints yang Hilang

### Masalah
Beberapa fungsi kunci pakai `Any` atau tidak ada return type:

```python
async def run_agent(*, agent_model: Any, session: Session, ...) -> dict[str, Any]:
```

`agent_model: Any` — harusnya `agent_model: Agent` (SQLAlchemy model).
Ini menyembunyikan error kalau tipe yang salah dipass.

### Solusi

```python
from app.models.agent import Agent as AgentModel

async def run_agent(
    *,
    agent_model: AgentModel,
    session: Session,
    user_message: str,
    db: AsyncSession,
    ...
) -> AgentRunResult:  # TypedDict atau dataclass
```

Buat `AgentRunResult` sebagai TypedDict:
```python
from typing import TypedDict

class AgentRunResult(TypedDict):
    reply: str
    steps: list[dict]
    run_id: uuid.UUID
    tokens_used: int
```

---

## Checklist Fase 2

- [ ] 2.1 Pecah `agent_runner.py` → `tool_builder.py`, `prompt_builder.py`, `subagent_builder.py`, `context_service.py`
- [ ] 2.2 Buat `phone_utils.py`, dedup `_normalize_phone` dari 4 tempat jadi 1
- [ ] 2.3 Pecah `wa_incoming()` handler jadi helper functions
- [ ] 2.4 Pindahkan semua magic numbers ke `config.py`
- [ ] 2.5 Ganti semua `except Exception: pass` dengan logging atau komentar jelas
- [ ] 2.6 Hilangkan import di dalam fungsi (terutama duplikat)
- [ ] 2.7 Tambah type hints untuk `run_agent()` dan fungsi publik lainnya
