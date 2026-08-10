#!/usr/bin/env python3
"""Drive black-box Arthur tests through a dedicated real WhatsApp session."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8082"
DEFAULT_DEVICE_ID = "watest_arthur"
DEFAULT_SCENARIO = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "e2e"
    / "scenarios"
    / "arthur_interview_smoke.json"
)
GLOBAL_FORBIDDEN_MARKERS = (
    "plan_agent",
    "discovery_progress",
    "capability_clarifications",
    "next_questions",
    "evidence format",
    "tool call",
    "panggil tool",
    "runtime gate",
)


class RunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunnerConfig:
    base_url: str
    api_key: str
    device_id: str
    target_phone: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "RunnerConfig":
        base_url = str(args.base_url or DEFAULT_BASE_URL).rstrip("/")
        api_key = str(args.api_key or "").strip()
        device_id = str(args.device_id or DEFAULT_DEVICE_ID).strip()
        target_phone = normalize_phone(str(args.target or ""))
        if not api_key:
            raise RunnerError("WA_TEST_API_KEY belum diisi")
        if not device_id:
            raise RunnerError("WA_TEST_DEVICE_ID tidak boleh kosong")
        if args.command == "run" and not target_phone:
            raise RunnerError("ARTHUR_TEST_TARGET_PHONE belum diisi")
        return cls(
            base_url=base_url,
            api_key=api_key,
            device_id=device_id,
            target_phone=target_phone,
        )


def normalize_phone(value: str) -> str:
    return "".join(char for char in value.split("@", 1)[0] if char.isdigit())


class TestServiceClient:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 45,
    ) -> dict[str, Any]:
        data = None
        headers = {"X-Test-Key": self.config.api_key}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.config.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RunnerError(f"{method} {path} gagal: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RunnerError(
                f"Tidak bisa menghubungi wa-test-service di {self.config.base_url}: {exc}"
            ) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"Respons bukan JSON dari {method} {path}") from exc

    def connect(self) -> dict[str, Any]:
        return self.request(
            "POST",
            "/devices",
            {"device_id": self.config.device_id},
            timeout=40,
        )

    def qr(self) -> dict[str, Any]:
        quoted_id = urllib.parse.quote(self.config.device_id, safe="")
        return self.request("GET", f"/devices/{quoted_id}/qr")

    def status(self) -> dict[str, Any]:
        quoted_id = urllib.parse.quote(self.config.device_id, safe="")
        return self.request("GET", f"/devices/{quoted_id}/status")

    def test_config(self) -> dict[str, Any]:
        return self.request("GET", "/test/config")

    def send(self, message: str) -> dict[str, Any]:
        quoted_id = urllib.parse.quote(self.config.device_id, safe="")
        return self.request(
            "POST",
            f"/devices/{quoted_id}/send",
            {"to": self.config.target_phone, "message": message},
            timeout=45,
        )

    def messages(self, after_seq: int) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"device_id": self.config.device_id, "after_seq": after_seq}
        )
        response = self.request("GET", f"/test/messages?{query}")
        items = response.get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def reset_inbox(self) -> None:
        self.request(
            "POST",
            "/test/messages/reset",
            {"device_id": self.config.device_id},
        )


def save_qr(response: dict[str, Any], output: Path) -> Path | None:
    encoded = str(response.get("qr_image") or "").strip()
    if not encoded:
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.write_bytes(base64.b64decode(encoded, validate=True))
    except (ValueError, binascii.Error) as exc:
        raise RunnerError("QR dari service bukan base64 PNG yang valid") from exc
    return output


def evaluate_reply(reply: str, expect: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    normalized = reply.casefold()
    if not reply.strip():
        failures.append("balasan kosong")
        return failures

    forbidden = [*GLOBAL_FORBIDDEN_MARKERS, *(expect.get("must_not_include") or [])]
    leaked = [marker for marker in forbidden if str(marker).casefold() in normalized]
    if leaked:
        failures.append(f"memuat teks terlarang: {', '.join(map(str, leaked))}")

    include_any = [str(value) for value in expect.get("must_include_any") or []]
    if include_any and not any(value.casefold() in normalized for value in include_any):
        failures.append(f"tidak memuat salah satu konteks: {', '.join(include_any)}")

    include_all = [str(value) for value in expect.get("must_include_all") or []]
    missing = [value for value in include_all if value.casefold() not in normalized]
    if missing:
        failures.append(f"konteks wajib tidak ditemukan: {', '.join(missing)}")

    max_questions = expect.get("max_questions")
    if max_questions is not None and reply.count("?") > int(max_questions):
        failures.append(
            f"terlalu banyak pertanyaan: {reply.count('?')} > {int(max_questions)}"
        )

    max_length = expect.get("max_length")
    if max_length is not None and len(reply) > int(max_length):
        failures.append(f"balasan terlalu panjang: {len(reply)} > {int(max_length)}")
    return failures


def wait_for_reply(
    client: TestServiceClient,
    after_seq: int,
    *,
    timeout_seconds: float,
    settle_seconds: float = 1.5,
) -> tuple[list[dict[str, Any]], int]:
    deadline = time.monotonic() + timeout_seconds
    collected: list[dict[str, Any]] = []
    latest_seq = after_seq
    settle_deadline: float | None = None
    while time.monotonic() < deadline:
        new_messages = client.messages(latest_seq)
        if new_messages:
            collected.extend(new_messages)
            latest_seq = max(int(item.get("seq") or 0) for item in collected)
            settle_deadline = time.monotonic() + settle_seconds
        if collected and settle_deadline is not None and time.monotonic() >= settle_deadline:
            return collected, latest_seq
        time.sleep(0.7)
    raise RunnerError(f"Arthur tidak membalas dalam {timeout_seconds:.0f} detik")


def message_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(item.get("message") or "").strip()
        for item in messages
        if str(item.get("message") or "").strip()
    ).strip()


def load_scenario(path: Path) -> dict[str, Any]:
    try:
        scenario = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunnerError(f"Scenario tidak ditemukan: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RunnerError(f"Scenario JSON tidak valid: {exc}") from exc
    turns = scenario.get("turns")
    if not isinstance(turns, list) or not turns:
        raise RunnerError("Scenario wajib memiliki turns yang tidak kosong")
    for index, turn in enumerate(turns, start=1):
        if not isinstance(turn, dict) or not str(turn.get("send") or "").strip():
            raise RunnerError(f"Turn {index} tidak memiliki pesan send")
    return scenario


def run_scenario(
    client: TestServiceClient,
    scenario: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    service_config = client.test_config()
    configured_target = normalize_phone(str(service_config.get("target_phone") or ""))
    if configured_target != client.config.target_phone:
        raise RunnerError(
            "Target runner tidak sama dengan target yang dikunci di wa-test-service"
        )
    status = client.status()
    if status.get("status") != "connected":
        raise RunnerError(
            f"Session tester belum connected (status={status.get('status')!r}); jalankan connect"
        )
    client.reset_inbox()
    latest_seq = 0
    transcript: list[dict[str, Any]] = []
    started_at = datetime.now(UTC).isoformat()

    if scenario.get("reset_before", True):
        client.send("/reset")
        reset_messages, latest_seq = wait_for_reply(
            client,
            latest_seq,
            timeout_seconds=timeout_seconds,
        )
        transcript.append(
            {
                "kind": "reset",
                "sent": "/reset",
                "reply": message_text(reset_messages),
                "passed": True,
                "failures": [],
            }
        )

    for index, turn in enumerate(scenario["turns"], start=1):
        sent = str(turn["send"]).strip()
        client.send(sent)
        messages, latest_seq = wait_for_reply(
            client,
            latest_seq,
            timeout_seconds=float(turn.get("timeout_seconds") or timeout_seconds),
        )
        reply = message_text(messages)
        failures = evaluate_reply(reply, turn.get("expect") or {})
        transcript.append(
            {
                "kind": "turn",
                "turn": index,
                "sent": sent,
                "reply": reply,
                "message_ids": [item.get("message_id") for item in messages],
                "passed": not failures,
                "failures": failures,
            }
        )
        state = "PASS" if not failures else "FAIL"
        print(f"[{state}] Turn {index}: {sent}")
        print(f"Arthur: {reply}\n")

    failed = [item for item in transcript if not item["passed"]]
    return {
        "scenario": scenario.get("name") or "unnamed",
        "target_phone": client.config.target_phone,
        "device_id": client.config.device_id,
        "started_at": started_at,
        "passed": not failed,
        "failed_turns": len(failed),
        "transcript": transcript,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("WA_TEST_SERVICE_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument("--api-key", default=os.getenv("WA_TEST_API_KEY", ""))
    parser.add_argument(
        "--device-id",
        default=os.getenv("WA_TEST_DEVICE_ID", DEFAULT_DEVICE_ID),
    )
    parser.add_argument("--target", default=os.getenv("ARTHUR_TEST_TARGET_PHONE", ""))
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect = subparsers.add_parser("connect", help="buat/reconnect session dan simpan QR")
    connect.add_argument(
        "--qr-output",
        type=Path,
        default=Path("artifacts/wa-test/arthur-tester-qr.png"),
    )

    qr = subparsers.add_parser("qr", help="ambil QR terbaru")
    qr.add_argument(
        "--qr-output",
        type=Path,
        default=Path("artifacts/wa-test/arthur-tester-qr.png"),
    )
    subparsers.add_parser("status", help="cek koneksi session tester")
    subparsers.add_parser("reset-inbox", help="hapus inbox test lokal")

    run = subparsers.add_parser("run", help="jalankan scenario ke nomor Arthur")
    run.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    run.add_argument("--timeout", type=float, default=180)
    run.add_argument(
        "--report",
        type=Path,
        default=Path("artifacts/wa-test/arthur-e2e-report.json"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = RunnerConfig.from_args(args)
        client = TestServiceClient(config)
        if args.command == "connect":
            response = client.connect()
            qr_path = save_qr(response, args.qr_output)
            print(json.dumps({key: value for key, value in response.items() if key != "qr_image"}, indent=2))
            if qr_path:
                print(f"Scan QR: {qr_path.resolve()}")
            return 0
        if args.command == "qr":
            response = client.qr()
            qr_path = save_qr(response, args.qr_output)
            print(f"status={response.get('status')}")
            print(f"QR: {qr_path.resolve()}" if qr_path else "QR kosong; session mungkin sudah connected")
            return 0
        if args.command == "status":
            print(json.dumps(client.status(), indent=2))
            return 0
        if args.command == "reset-inbox":
            client.reset_inbox()
            print("Inbox test sudah dikosongkan.")
            return 0
        if args.command == "run":
            scenario = load_scenario(args.scenario)
            report = run_scenario(client, scenario, timeout_seconds=args.timeout)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Report: {args.report.resolve()}")
            return 0 if report["passed"] else 1
        raise RunnerError(f"Command tidak didukung: {args.command}")
    except RunnerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
