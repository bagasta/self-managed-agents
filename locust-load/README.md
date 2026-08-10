# Locust Load Tests

Load test lokal untuk endpoint WhatsApp backend:

- traffic normal dari banyak user berbeda
- probe Arthur
- burst spam dari satu nomor agar `ai_disabled` dan eskalasi operator bisa diuji

## Install

```bash
cd locust-load
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Environment

Minimal:

```bash
export LOCUST_HOST=http://localhost:8000
export LOCUST_AGENT_TARGETS=wadev_<agent_id_1>,wadev_<agent_id_2>
```

Opsional:

```bash
export LOCUST_ARTHUR_DEVICE_ID=<arthur_wa_device_id>
export LOCUST_API_KEY=change-me
export LOCUST_USER_PREFIX=62888001
export LOCUST_SPAM_PHONE=628889990001
export LOCUST_SPAM_BURST_SIZE=6
export LOCUST_ALLOW_SEND_FAILED=false
```

Catatan:

- Untuk Arthur, pakai `LOCUST_ARTHUR_DEVICE_ID` dari device WhatsApp Arthur yang dedicated.
- Untuk agent trial via wa-dev, target bisa berupa `wadev_<agent_id>`.
- `LOCUST_AGENT_TARGETS` boleh berisi device id WA normal atau `wadev_<agent_id>`, dipisah koma.
- Spam burst mengirim banyak request dengan timestamp sama tapi `message_id` unik. Ini memvalidasi dedupe berbasis message ID, bukan timestamp detik.
- Untuk task normal/Arthur, `send_failed` dianggap failure karena itu berarti balasan tidak terkirim ke WA. Kalau hanya mau stress test backend tanpa device WA tersambung, set `LOCUST_ALLOW_SEND_FAILED=true`.
- Untuk task spam, `send_failed` pre-threshold tidak dianggap failure agar test tetap bisa memvalidasi `ai_disabled` walaupun device WA lokal belum tersambung.

## Run

UI:

```bash
locust -f locustfile.py --host "$LOCUST_HOST"
```

Headless normal multi-user:

```bash
locust -f locustfile.py --headless --users 20 --spawn-rate 5 --run-time 2m --tags normal
```

Headless Arthur:

```bash
locust -f locustfile.py --headless --users 5 --spawn-rate 1 --run-time 1m --tags arthur
```

Headless spam guard:

```bash
locust -f locustfile.py --headless --users 1 --spawn-rate 1 --run-time 30s --tags spam
```

Expected untuk spam: burst memicu sesi menjadi `ai_disabled`. Pada burst pertama biasanya ada satu response dengan `reason=spam_auto_disabled`; request sesudahnya bisa hanya mengembalikan `status=ai_disabled`.
