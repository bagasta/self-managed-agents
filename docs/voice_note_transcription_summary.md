# Voice Note Transcription Implementation Summary

Integrasi fitur transkripsi pesan suara (Voice Note/Audio) dari WhatsApp menggunakan OpenRouter (`openai/gpt-audio-mini`) telah selesai dan lulus TDD.

## Perubahan yang Dilakukan:

1. **Go Service (`wa-service` & `wa-dev-service`)**
   - Menambahkan _handler_ untuk `evt.Message.GetAudioMessage()`.
   - Mengunduh data binary audio dan melakukan _encoding_ ke format `base64`.
   - Membedakan jenis audio:
     - Menggunakan `audio.GetPTT()` untuk mendeteksi _Voice Note_ (Push-to-Talk) dan mengeset `media_type="ptt"`.
     - File audio biasa (kirim lagu/rekaman file) diset sebagai `media_type="audio"`.

2. **Python Backend (`app/api/channels.py` & `app/api/wa_helpers.py`)**
   - Memperbarui skema `WAIncomingMessage` agar menerima `media_type` dengan nilai `"ptt"` atau `"audio"`.
   - Menambahkan _handler_ di `process_wa_media()` untuk tipe `ptt` dan `audio`.
   - Mengurai ekstensi file (default ke `ogg` untuk PTT WhatsApp).

3. **Transcription Service (`app/core/transcription_service.py`)**
   - Dibuat layanan baru `transcribe_audio` yang memanggil API OpenRouter.
   - Model yang digunakan: `openai/gpt-audio-mini`.
   - Skema payload menggunakan `input_audio` (format kompatibel dengan OpenAI API):
     ```json
     {
       "type": "input_audio",
       "input_audio": {
         "data": "<base64>",
         "format": "ogg"
       }
     }
     ```
   - Menyediakan _fallback_ yang aman (`[Voice note: tidak dapat ditranskripsi]`) jika API _key_ tidak ada atau request ke OpenRouter gagal (sehingga chat ke agen tetap berjalan).

4. **TDD (Test-Driven Development)**
   - Semua tes `transcription_service.py` berjalan sempurna (11/11 tests passed). Termasuk _mocking_ dari request HTTP, fallback, dan lokalisasi _monkeypatch_ dari `app.core.sandbox`.

## Status
Binary Go telah dibangun ulang (`make wa-build` dan `make wa-dev-build`). Fitur siap digunakan! Anda dapat mulai mengirim pesan suara ke bot WhatsApp, dan itu akan ditranskripsi otomatis.
