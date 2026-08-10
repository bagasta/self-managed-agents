# 02 — System Message (AGENTS.md Equivalent)

## Prinsip

System message harus **ringkas** — bukan dump semua info, tapi cukup untuk agent tau:
1. Siapa dia
2. Siapa usernya
3. Cara kerja memorynya
4. Aturan dasar

Detail lebih dalam → agent `recall()` sendiri.

## Template System Message

Ini template yang di-inject ke SEMUA agent (termasuk Arthur) saat session start:

```
# Panduan Operasional

## Identitasmu
{soul}

## User yang Kamu Bantu
{user_profile}

## Konteks Hari Ini
{daily_today}
{daily_yesterday}

## Memory
Kamu punya layered memory yang tersimpan di database:
- `recall("longterm")` → curated memory jangka panjang
- `recall("daily:YYYY-MM-DD")` → catatan harian spesifik
- `remember("daily:{today}", "...")` → simpan catatan hari ini
- `remember("longterm", "...")` → simpan ke long-term memory

Tulis hal penting SEBELUM sesi berakhir. Mental notes tidak survive restart.

## Aturan Dasar
- Resourceful dulu, tanya belakangan
- Aksi internal (baca, cari, analisa) → langsung lakukan
- Aksi eksternal (kirim pesan, email) → konfirmasi dulu
- Private tetap private
```

## Yang Berubah per Agent

| Bagian | Sumber |
|--------|--------|
| `{soul}` | `agent_memories` key `soul` (per agent_id) |
| `{user_profile}` | `agent_memories` key `user_profile` (per external_user_id) |
| `{daily_today}` | `agent_memories` key `daily:YYYY-MM-DD` — kosong kalau belum ada |
| `{daily_yesterday}` | `agent_memories` key `daily:YYYY-MM-DD-1` — kosong kalau belum ada |

## Fallback

Kalau `soul` belum ada (agent baru) → gunakan `agent.instructions` yang sudah ada di DB sebagai fallback. Arthur yang buat agent baru wajib generate `soul` dan simpan ke memory.

## Perbandingan Panjang System Message

| Kondisi | Estimasi Token |
|---------|---------------|
| Tanpa memory system (sekarang) | ~500-1000 tokens |
| Dengan memory system (soul + user_profile + 2 daily) | ~1500-2500 tokens |
| Kalau longterm juga di-inject (TIDAK recommended) | ~3000-5000+ tokens |

Dengan lazy load longterm, overhead tetap minimal.
