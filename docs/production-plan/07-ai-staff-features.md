# AI Staff — Feature Plan

Tiga fitur baru untuk meningkatkan kontrol dan kapabilitas agent WhatsApp.

## Daftar Fitur

| # | Fitur | Kompleksitas | Estimasi |
|---|-------|-------------|----------|
| 1 | [Allowlist Pengirim](#1-allowlist-pengirim) | Rendah | ~1 jam |
| 2 | [On/Off Chat per User](#2-onoff-chat-per-user-operator-only) | Sedang | ~2 jam |
| 3 | [Voice Note Transcription](#3-voice-note-transcription) | Tinggi | ~4 jam |

Urutan pengerjaan: 1 → 2 → 3. Fitur 1 dan 2 tidak butuh perubahan Go service.

---

## 1. Allowlist Pengirim

Agent hanya membalas pesan dari nomor-nomor yang ada di daftar izin. Nomor di luar daftar di-*silent drop* (tidak dibalas, tidak error).

### File yang diubah

**`app/models/agent.py`** — tambah kolom:
```python
allowed_senders: Mapped[list | None] = mapped_column(JSONB, nullable=True)
# null = semua nomor diizinkan (default behavior)
# ["628111", "628222"] = hanya nomor ini yang dibalas
```

**`alembic/`** — generate migration:
```bash
make migrate MSG="add allowed_senders to agents"
```

**`app/api/channels.py`** — tambah cek setelah `find_agent_by_device()`, sebelum session dibuat:
```python
if agent.allowed_senders:
    normalized_sender = normalize_phone(from_phone)
    allowed = {normalize_phone(p) for p in agent.allowed_senders}
    if normalized_sender not in allowed:
        log.info("wa_incoming.blocked_sender", from_phone=from_phone)
        return {"status": "ignored"}
```

**`app/schemas/agent.py`** — tambah ke `AgentUpdate` dan `AgentResponse`:
```python
allowed_senders: list[str] | None = None
```

### Catatan
- Operator (`operator_ids`) selalu diizinkan, cek allowlist hanya untuk non-operator.
- Format nomor: simpan tanpa `+`, normalisasi pakai `normalize_phone()` yang sudah ada.

---

## 2. On/Off Chat per User (Operator Only)

Operator bisa mematikan balasan AI untuk satu pengguna tertentu via perintah natural language. AI tetap diam selama dinonaktifkan; operator yang mengaktifkan kembali.

### File yang diubah

**`app/models/session.py`** — tambah kolom:
```python
ai_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

**`alembic/`** — generate migration:
```bash
make migrate MSG="add ai_disabled to sessions"
```

**`app/api/channels.py`** — tambah cek setelah session ditemukan/dibuat, sebelum `run_agent`:
```python
if session.ai_disabled and not _is_operator:
    log.info("wa_incoming.ai_disabled", session_id=str(session.id))
    return {"status": "ai_disabled"}
```

**`app/core/tools/operator_tools.py`** — buat file baru dengan dua tools:

```python
async def disable_ai_for_user(phone: str, agent_id, db) -> str:
    """
    Matikan balasan AI untuk nomor pengguna tertentu.
    Args:
        phone: Nomor WhatsApp pengguna (contoh: 628xxx)
    """
    # normalize phone → cari session by agent_id + external_user_id
    # set session.ai_disabled = True → flush → commit
    # return konfirmasi

async def enable_ai_for_user(phone: str, agent_id, db) -> str:
    """
    Aktifkan kembali balasan AI untuk nomor pengguna tertentu.
    Args:
        phone: Nomor WhatsApp pengguna (contoh: 628xxx)
    """
    # normalize phone → cari session → set ai_disabled = False
    # return konfirmasi
```

**`app/core/tool_builder.py`** — tambah `operator_tools` ke stack ketika session adalah operator:
```python
if is_operator_session:
    tools += build_operator_tools(agent_id=agent_id, db=db)
```

**`app/core/agent_runner.py`** — pass `is_operator` flag ke `tool_builder` (sudah ada di context, tinggal diteruskan).

### Catatan
- Tools ini **hanya aktif di session operator** — non-operator tidak pernah dapat akses.
- Jika pengguna yang di-disable belum punya session (belum pernah chat), kembalikan pesan informatif ke operator.
- `ai_disabled` disimpan di session, bukan di agent — sehingga bisa per-user, bukan global.

---

## 3. Voice Note Transcription

Pesan suara (PTT/voice note) dari WhatsApp dikonversi ke teks menggunakan Whisper sebelum dikirim ke agent, sehingga agent memahami konteks percakapan suara.

### File yang diubah

**`wa-service/device_manager.go`** — tambah handling `AudioMessage` dan PTT di `handleIncoming()`:
```go
// Setelah case DocumentMessage (sekitar line 620)
case *waProto.Message_AudioMessage:
    audioMsg := msg.GetAudioMessage()
    audioBytes, err := client.Download(audioMsg)
    if err == nil {
        mediaData = base64.StdEncoding.EncodeToString(audioBytes)
        if audioMsg.GetPtt() {
            mediaType = "ptt"       // push-to-talk / voice note
            mediaFilename = "voice.ogg"
        } else {
            mediaType = "audio"     // file audio biasa
            mediaFilename = "audio.ogg"
        }
    }
```
Rebuild binary setelah perubahan: `make wa-build`.

**`app/core/transcription_service.py`** — buat service baru:
```python
async def transcribe_audio(audio_b64: str, mime: str = "audio/ogg") -> str:
    """
    Transkripsi audio base64 ke teks via OpenAI Whisper API.
    Return teks transkripsi, atau fallback string jika gagal.
    """
    # decode base64 → bytes
    # POST ke https://api.openai.com/v1/audio/transcriptions
    #   model=whisper-1, file=audio bytes, response_format=text
    # return teks atau "[Pesan suara tidak dapat ditranskripsi]"
```

**`app/config.py`** — tambah:
```python
OPENAI_API_KEY: str = ""   # untuk Whisper; bisa sama dengan key OpenRouter
```

**`.env.example`** — tambah:
```
OPENAI_API_KEY=sk-...
```

**`app/api/channels.py`** — di `process_wa_media()`, tambah case audio:
```python
elif media_type in ("audio", "ptt"):
    transcript = await transcription_service.transcribe_audio(media_data)
    label = "Voice note" if media_type == "ptt" else "Audio"
    media_context = f"\n[{label}: {transcript}]"
    # Tidak ada media_image_b64 — audio tidak dikirim ke LLM sebagai gambar
    return media_context, None, None
```

### Catatan
- Whisper mendukung format ogg/opus yang dipakai WhatsApp secara native.
- Jika `OPENAI_API_KEY` kosong, langsung fallback ke `[Pesan suara diterima - transkripsi tidak aktif]`.
- wa-service binary perlu di-rebuild dan di-redeploy setelah perubahan Go.
- Estimasi biaya Whisper: ~$0.006/menit audio — sangat murah untuk use case percakapan.

---

## Urutan Deployment

```
Fitur 1 (Allowlist)
  → make migrate
  → restart API

Fitur 2 (On/Off Chat)
  → make migrate
  → restart API

Fitur 3 (Voice Note)
  → make wa-build (rebuild wa-service binary)
  → restart API + wa-service
```

Fitur 1 dan 2 bisa di-deploy bersamaan (satu migration run).
