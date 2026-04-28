# Recap: Bug Allowlist & WhatsApp LID Accounts

**Tanggal**: 2026-04-28  
**Status**: ✅ Terselesaikan — Verified working in production

---

# Recap: Bug Voice Note Transcription — OGG Format Not Supported

**Tanggal**: 2026-04-28  
**Status**: ✅ Terselesaikan — Verified working in production

## Gejala

Kirim voice note (PTT) di WhatsApp → transcription gagal dengan error 400 dari OpenRouter:
```
"Invalid value: 'ogg'. Supported values are: 'wav' and 'mp3'."
```
Agent menerima fallback `[Voice note: tidak dapat ditranskripsi]` dan tidak bisa baca isi VN.

## Root Cause

Model `openai/gpt-audio-mini` via OpenRouter hanya support format `wav` dan `mp3`. WhatsApp PTT dikirim sebagai `.ogg`. Kode sebelumnya langsung kirim format `ogg` ke API tanpa konversi.

## Fix

### 1. `app/core/transcription_service.py`
- Tambah `_OPENAI_SUPPORTED_FORMATS = {"mp3", "wav"}`
- Tambah `_convert_to_mp3(audio_b64)` — konversi via `ffmpeg` menggunakan `asyncio.create_subprocess_exec`
- Pakai `shutil.which("ffmpeg")` untuk resolve path binary
- Di `transcribe_audio()`: auto-konversi jika format bukan mp3/wav sebelum kirim ke API

### 2. `Dockerfile`
- Tambah `ffmpeg` ke `apt-get install` block

### 3. `tests/test_transcription_service.py`
- 2 test baru: `test_ogg_converted_to_mp3`, `test_ogg_conversion_failure_returns_fallback`
- Existing tests ganti ke format `"mp3"`
- `TestProcessWaMediaAudio` tests: monkeypatch `_convert_to_mp3`
- **13/13 tests passed**

## Deploy
```bash
cd deploy && docker compose -f docker-compose.prod.yml up --build -d api
```

---

---

## Deskripsi Bug

Fitur `allowed_senders` pada agent tidak berfungsi dengan benar untuk akun WhatsApp modern yang menggunakan sistem **LID (Linked ID)**.

### Gejala

- User isi `allowed_senders` dengan format nomor biasa: `+6282299312107`
- Agent **tetap memblokir** nomor tersebut (seharusnya diizinkan)
- Agent juga **memblokir nomor lain** yang tidak ada di allowlist (seharusnya benar)
- Kedua nomor diblokir dengan log yang sama

### Log Error di Server

```json
{"device_id": "wadev_b76b7e02-...", "from_phone": "+236116347228384", "chat_id": "236116347228384@lid", "event": "wa_incoming.blocked_sender"}
{"device_id": "wadev_b76b7e02-...", "from_phone": "+151414827434073", "chat_id": "151414827434073@lid", "event": "wa_incoming.blocked_sender"}
```

---

## Root Cause

WhatsApp modern menggunakan dua sistem identifikasi berbeda:

| Format | Contoh | Keterangan |
|--------|--------|-----------|
| **Phone JID** | `6282299312107@s.whatsapp.net` | Akun WA lama |
| **LID** | `236116347228384@lid` | Akun WA baru (Linked ID) |

Untuk akun LID:
- `evt.Info.Sender.User` di Go berisi **LID number** (`236116347228384`), **bukan phone number**
- `evt.Info.Chat.String()` juga berisi LID format (`236116347228384@lid`)
- **Tidak ada field yang berisi phone number asli** dalam message event

Sehingga:
- `allowed_senders = ["+6282299312107"]` → normalized: `6282299312107`
- Incoming `from_phone = "+236116347228384"` → normalized: `236116347228384`
- **Tidak pernah match** karena keduanya adalah identifier berbeda

---

## Yang Sudah Dicoba (Gagal)

### Attempt 1: Dual-check from_phone + chat_id
Cek allowlist terhadap `from_phone` DAN `chat_id`. Gagal karena keduanya sama-sama LID format.

### Attempt 2: `GetPNForLID` dari local store
Gunakan `client.Store.LIDs.GetPNForLID()` di Go untuk resolve LID → phone dari local SQLite cache.  
Gagal karena: mapping LID↔phone hanya ada di cache jika kontak sudah pernah di-sync WA (akun yang baru pertama kali pesan tidak ada di cache).

### Attempt 3: `IsOnWhatsApp` endpoint
Tambah endpoint `POST /devices/{id}/resolve-phones` di Go yang query WA server via `IsOnWhatsApp()`.  
Python panggil endpoint ini saat allowlist check, resolve `+6282299312107` → actual WA JID.  
**Status**: Kode sudah di-push tapi hasil di server masih sama — kemungkinan:
- Docker belum rebuild dengan kode terbaru
- `IsOnWhatsApp` rate-limited atau belum dipanggil dengan benar
- Masih ada issue di alur resolve

---

## File yang Dimodifikasi

| File | Perubahan |
|------|-----------|
| `wa-service/handlers.go` | Tambah handler `resolvePhones` — endpoint `POST /devices/{id}/resolve-phones` |
| `wa-service/main.go` | Register route `resolve-phones` |
| `wa-service/device_manager.go` | Tambah field `phone_from` di payload, coba `GetPNForLID` |
| `app/core/wa_client.py` | Tambah fungsi `resolve_wa_phones()` |
| `app/api/channels.py` | Allowlist check pakai `resolve_wa_phones` + JID comparison |
| `app/models/agent.py` | Tambah kolom `allowed_senders` (JSONB) |
| `app/models/session.py` | Tambah kolom `ai_disabled` (Boolean) |

---

## Root Cause Sebenarnya (Ditemukan)

`wa-dev-service` menghasilkan device ID dengan prefix `wadev_` (format: `wadev_{agentID}`). Di `app/core/wa_client.py`, fungsi `resolve_wa_phones` langsung return `{}` untuk device ID dengan prefix `wadev_`:

```python
if device_id.startswith("wadev_"):
    return {}  # ← bypass total, allowed_set hanya berisi phone asli
```

Akibatnya:
- `allowed_set = {"6282299312107"}` (phone biasa saja)
- `candidates = {"236116347228384"}` (LID number dari incoming)
- **Tidak pernah intersect → selalu blocked**

## Fix yang Diimplementasikan

### 1. `wa-dev-service/whatsapp.go`
Tambah method `ResolvePhones()` ke `WhatsAppClient` yang memanggil `client.IsOnWhatsApp()` — sama persis dengan wa-service.

### 2. `wa-dev-service/api.go`
Tambah handler `POST /resolve-phones` yang memanggil method di atas.

### 3. `wa-dev-service/main.go`
Register route `POST /resolve-phones`.

### 4. `app/core/wa_client.py`
Ganti early-return `wadev_` dengan routing ke `_wa_dev_base_url()/resolve-phones`:
```python
if device_id.startswith("wadev_"):
    url = f"{_wa_dev_base_url()}/resolve-phones"
else:
    url = f"{_base_url()}/devices/{device_id}/resolve-phones"
```

### Setelah fix, flow yang benar:
1. Incoming: `from_phone = "236116347228384"` (LID)
2. `resolve_wa_phones(device_id, ["+6282299312107"])` → wa-dev `/resolve-phones` → `IsOnWhatsApp(["6282299312107"])` → `{"6282299312107": "236116347228384@lid"}`
3. `allowed_set = {"6282299312107", "236116347228384"}` (phone + LID part)
4. `candidates = {"236116347228384"}` → **intersect! → allowed**

### Deploy
Rebuild wa-dev-service binary: `make wa-dev-build`

### Hasil
✅ **Verified working** — nomor phone biasa (`+6282299312107`) di `allowed_senders` berhasil dikenali meskipun incoming adalah LID account.

---

## Yang Perlu Diinvestigasi Lebih Lanjut

1. **Verifikasi endpoint resolve-phones berjalan**: Test manual dengan curl dari VPS:
   ```bash
   curl -X POST http://localhost:8080/devices/{device_id}/resolve-phones \
     -H "Content-Type: application/json" \
     -d '{"phones": ["+6282299312107"]}'
   ```
   Lihat apakah response-nya mengandung JID yang benar (LID atau phone).

2. **Cek apakah `IsOnWhatsApp` return LID JID**: Untuk akun LID, `res.JID` dari `IsOnWhatsApp` seharusnya berisi `236116347228384@lid`. Kalau tidak, berarti WA API tidak mengekspos mapping ini.

3. **Alternatif: Simpan mapping LID↔phone di DB**: Saat session pertama dibuat untuk akun LID, simpan `external_user_id = LID` tapi juga simpan `phone_number` jika tersedia. Operator kemudian bisa query session untuk tahu LID dari phone number.

4. **Alternatif: Ubah cara input allowed_senders**: Buat operator bisa input dalam format LID juga, atau buat sistem dimana operator bisa "learn" nomor yang masuk sebelum di-allowlist.

5. **Cek whatsmeow versi terbaru**: Ada kemungkinan versi terbaru whatsmeow punya API lain untuk resolve LID yang lebih reliable.

---

## Context Code Penting

### Allowlist check saat ini (`app/api/channels.py` ~line 248)
```python
if not _is_operator:
    allowed = getattr(agent, "allowed_senders", None)
    if allowed:
        resolved = await resolve_wa_phones(body.device_id, [p for p in allowed if p])
        allowed_set: set[str] = set()
        for p in allowed:
            normalized = normalize_phone(p)
            allowed_set.add(normalized)
            jid = resolved.get(normalized)
            if jid:
                allowed_set.add(normalize_phone(jid))
        candidates = {normalize_phone(from_phone)}
        if reply_target:
            candidates.add(normalize_phone(reply_target))
        if not candidates.intersection(allowed_set):
            return {"status": "ignored", "reason": "sender not in allowlist"}
```

### Go resolve-phones handler (`wa-service/handlers.go`)
```go
results, err := info.Client.IsOnWhatsApp(r.Context(), stripped)
// stripped = ["6282299312107"]
// results[0].JID.String() seharusnya = "236116347228384@lid" untuk akun LID
```

### Dari mana `from_phone` di Python
```
Go wa-service → body.from_ = "+" + evt.Info.Sender.User  (LID number untuk akun LID)
Go wa-service → body.phone_from = hasil GetPNForLID (fallback ke from_ jika gagal)
Python → from_phone = body.phone_from or body.from_
```