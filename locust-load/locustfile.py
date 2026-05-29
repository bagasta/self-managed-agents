from __future__ import annotations

import itertools
import os
import random
import time
import uuid

import gevent
from gevent.pool import Pool
from locust import HttpUser, between, events, tag, task


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


API_KEY = os.getenv("LOCUST_API_KEY", "")
ARTHUR_DEVICE_ID = os.getenv("LOCUST_ARTHUR_DEVICE_ID", "").strip()
AGENT_TARGETS = _csv_env("LOCUST_AGENT_TARGETS")
USER_PREFIX = os.getenv("LOCUST_USER_PREFIX", "62888001")
SPAM_PHONE = os.getenv("LOCUST_SPAM_PHONE", "628889990001")
SPAM_BURST_SIZE = int(os.getenv("LOCUST_SPAM_BURST_SIZE", "6"))
ALLOW_SEND_FAILED = os.getenv("LOCUST_ALLOW_SEND_FAILED", "").lower() in {"1", "true", "yes", "on"}

_user_counter = itertools.count(1)


@events.init.add_listener
def _print_config(environment, **_kwargs):
    print(
        "Locust WA config: "
        f"arthur={'yes' if ARTHUR_DEVICE_ID else 'no'}, "
        f"agent_targets={len(AGENT_TARGETS)}, "
        f"spam_burst_size={SPAM_BURST_SIZE}"
    )


class WhatsAppWebhookUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        idx = next(_user_counter)
        self.phone = f"{USER_PREFIX}{idx:04d}"
        self.push_name = f"Locust User {idx}"

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if API_KEY:
            headers["X-API-Key"] = API_KEY
        return headers

    def _payload(self, device_id: str, phone: str, message: str, *, timestamp: int | None = None) -> dict:
        clean_phone = phone.lstrip("+")
        return {
            "device_id": device_id,
            "from": f"+{clean_phone}",
            "phone_from": f"+{clean_phone}",
            "chat_id": f"{clean_phone}@s.whatsapp.net",
            "message": message,
            "message_id": f"locust-{uuid.uuid4().hex}",
            "timestamp": timestamp or int(time.time()),
            "push_name": self.push_name,
            "media_type": None,
            "media_data": None,
            "media_filename": None,
        }

    def _post_wa(
        self,
        name: str,
        device_id: str,
        phone: str,
        message: str,
        *,
        timestamp: int | None = None,
        allow_send_failed: bool = False,
    ):
        payload = self._payload(device_id, phone, message, timestamp=timestamp)
        with self.client.post(
            "/v1/channels/wa/incoming",
            json=payload,
            headers=self._headers,
            name=name,
            catch_response=True,
            timeout=180,
        ) as response:
            if response.status_code >= 500:
                response.failure(f"HTTP {response.status_code}: {response.text[:300]}")
                return None
            try:
                data = response.json()
            except Exception:
                response.failure(f"non-json response: {response.text[:300]}")
                return None
            if data.get("status") == "send_failed" and not (ALLOW_SEND_FAILED or allow_send_failed):
                response.failure(f"reply send_failed: {data.get('reply_delivery')}")
            return data

    @tag("normal")
    @task(10)
    def many_users_to_agents(self):
        if not AGENT_TARGETS:
            return
        target = random.choice(AGENT_TARGETS)
        msg = random.choice(
            [
                "Halo, jelaskan layanan kamu singkat aja.",
                "Apa yang bisa kamu bantu?",
                "Tolong jawab singkat dalam 2 kalimat.",
                "Saya mau tanya harga dan cara order.",
            ]
        )
        self._post_wa("wa normal agent", target, self.phone, msg)

    @tag("arthur")
    @task(3)
    def arthur_probe(self):
        if not ARTHUR_DEVICE_ID:
            return
        msg = random.choice(
            [
                "Arthur, jelasin singkat kamu bisa bantu bikin agent apa aja.",
                "Arthur, saya mau bikin agent CS WhatsApp. Tanya satu hal paling penting dulu.",
                "Arthur, apakah agent bisa dicoba via nomor demo?",
            ]
        )
        self._post_wa("wa arthur probe", ARTHUR_DEVICE_ID, self.phone, msg)

    @tag("spam")
    @task(1)
    def spam_burst_same_user(self):
        targets = AGENT_TARGETS or ([ARTHUR_DEVICE_ID] if ARTHUR_DEVICE_ID else [])
        if not targets:
            return
        target = targets[0]
        fixed_ts = int(time.time())
        pool = Pool(SPAM_BURST_SIZE)
        jobs = [
            pool.spawn(
                self._post_wa,
                "wa spam burst",
                target,
                SPAM_PHONE,
                f"spam test {i} {uuid.uuid4().hex[:8]}",
                timestamp=fixed_ts,
                allow_send_failed=True,
            )
            for i in range(SPAM_BURST_SIZE)
        ]
        gevent.joinall(jobs, timeout=240)
        results = [job.value for job in jobs if job.value]
        if not any(
            r.get("status") == "ai_disabled"
            and r.get("reason") in {"spam_auto_disabled", None, ""}
            for r in results
        ):
            events.request.fire(
                request_type="ASSERT",
                name="spam auto disable",
                response_time=0,
                response_length=0,
                exception=AssertionError(f"spam burst did not auto-disable; statuses={[r.get('status') for r in results]}"),
            )
