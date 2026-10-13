"""LangChain tools for an owner-assigned interactive computer."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.tools import tool

from app.core.infra.computer_runtime import ComputerRuntime


def build_computer_tools(
    runtime: ComputerRuntime,
    *,
    owner_id: str | None,
    visual_observation: bool = False,
) -> list:
    # The model sees screenshots, but successful input injection alone does not
    # prove that the application accepted it. Keep a small local state so the
    # tool output tells the model when it is repeating an action on a stable UI.
    last_screen_digest: str | None = None
    unchanged_screen_streak = 0
    last_action: str | None = None
    coordinate_space: dict[str, int] = {}

    def screen_content(screen: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
        nonlocal last_screen_digest, unchanged_screen_streak, coordinate_space
        coordinate_space = {
            key: screen[key] for key in ("desktop_width", "desktop_height", "image_width", "image_height")
            if isinstance(screen.get(key), int) and screen[key] > 0
        }
        digest = hashlib.sha256(screen["image_base64"].encode("ascii")).hexdigest()[:16]
        changed = last_screen_digest is not None and digest != last_screen_digest
        if last_screen_digest is None or changed:
            unchanged_screen_streak = 0
        else:
            unchanged_screen_streak += 1
        last_screen_digest = digest
        verification = {
            "screen_digest": digest,
            "screen_changed": changed,
            "unchanged_screen_streak": unchanged_screen_streak,
            "instruction": (
                "Layar tidak berubah. Jangan ulangi aksi atau screenshot yang sama; gunakan decide_next_action untuk menilai layar dan ganti pendekatan."
                if unchanged_screen_streak >= 2 else
                "Bandingkan layar ini dengan tujuan sebelum melakukan aksi berikutnya."
            ),
        }
        return [
            {
                "type": "text",
                "text": json.dumps(
                    {**result, "observation": "Screenshot terbaru terlampir. Gunakan koordinat pixel pada gambar ini; konversi ke desktop dilakukan oleh tool.", "coordinate_space": coordinate_space, "verification": verification},
                    ensure_ascii=False,
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:{screen['mime_type']};base64,{screen['image_base64']}"},
            },
        ]

    def observed(result: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
        if not visual_observation or not result.get("ok"):
            return result
        screen = runtime.capture_screen(owner_id=owner_id)
        if not screen.get("ok"):
            return {**result, "observation": {"ok": False, "code": screen.get("code"), "message": screen.get("message")}}
        return screen_content(screen, result)

    def repeated_action_block(action: str) -> dict[str, Any] | None:
        nonlocal last_action
        if action == last_action and unchanged_screen_streak >= 2:
            return {
                "ok": False,
                "code": "replan_required",
                "message": "Aksi yang sama diblokir karena dua screenshot terakhir tidak berubah. Ambil screenshot, nilai UI, lalu gunakan pendekatan lain.",
            }
        last_action = action
        return None

    @tool
    def computer_get_status() -> dict:
        """Check the shared computer and which bot currently holds its turn. Bots keep their own permissions in group chat. Computer actions wait for a turn automatically."""
        return runtime.status(owner_id=owner_id)

    @tool
    def computer_release_turn() -> dict:
        """Release the shared desktop when your computer work is done, before waiting for a colleague, or before handing work off. Later use requires observing the screen again; another bot may have changed it."""
        return runtime.release_turn()

    @tool
    def computer_open_url(url: str) -> dict | list[dict[str, Any]]:
        """Open one http(s) URL in Chrome, then inspect the returned screen before claiming navigation succeeded."""
        blocked = repeated_action_block(f"open_url:{url}")
        return blocked if blocked else observed(runtime.open_url(owner_id=owner_id, url=url))

    @tool
    def computer_click(x: int, y: int) -> dict | list[dict[str, Any]]:
        """Click pixel coordinates in the latest screenshot image; the tool converts them to desktop coordinates. Inspect the returned screen. Never use for sensitive confirmations without owner approval."""
        if len(coordinate_space) == 4:
            if not (0 <= x < coordinate_space["image_width"] and 0 <= y < coordinate_space["image_height"]):
                return {"ok": False, "code": "invalid_coordinates", "message": "Gunakan koordinat di dalam screenshot terbaru."}
            x = min(coordinate_space["desktop_width"] - 1, round(x * coordinate_space["desktop_width"] / coordinate_space["image_width"]))
            y = min(coordinate_space["desktop_height"] - 1, round(y * coordinate_space["desktop_height"] / coordinate_space["image_height"]))
        blocked = repeated_action_block(f"click:{x}:{y}")
        return blocked if blocked else observed(runtime.click(owner_id=owner_id, x=x, y=y))

    @tool
    def computer_type_text(text: str) -> dict | list[dict[str, Any]]:
        """Type ordinary non-sensitive text, then inspect the returned screen. Never type passwords, OTPs, API keys, or access tokens."""
        blocked = repeated_action_block(f"type:{text}")
        return blocked if blocked else observed(runtime.type_text(owner_id=owner_id, text=text))

    @tool
    def computer_request_human_takeover(reason: str) -> dict:
        """Record the exact blocked sensitive step for a human takeover. Call this only after completing all safe independent work still possible in this request; it does not end the run by itself."""
        return runtime.request_human_takeover(owner_id=owner_id, reason=reason)

    @tool
    def computer_press_key(key: str) -> dict | list[dict[str, Any]]:
        """Press one navigation key/combo, then inspect the returned screen before assuming its effect."""
        blocked = repeated_action_block(f"key:{key}")
        return blocked if blocked else observed(runtime.press_key(owner_id=owner_id, key=key))

    if visual_observation:
        @tool
        def computer_screenshot() -> dict | list[dict[str, Any]]:
            """Observe the actual current computer screen. Always call this before the first visual action and after uncertainty or a failed verification."""
            screen = runtime.capture_screen(owner_id=owner_id)
            if not screen.get("ok"):
                return screen
            return screen_content(screen, {"ok": True, "status": "screen_captured"})
        return [computer_get_status, computer_screenshot, computer_open_url, computer_click, computer_type_text, computer_request_human_takeover, computer_press_key, computer_release_turn]

    return [computer_get_status, computer_open_url, computer_click, computer_type_text, computer_request_human_takeover, computer_press_key, computer_release_turn]
