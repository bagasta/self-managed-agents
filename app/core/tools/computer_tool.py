"""LangChain tools for an owner-assigned interactive computer."""
from __future__ import annotations

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
    def observed(result: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
        """Attach the framebuffer to successful visual-agent actions.

        LangChain preserves standard content blocks in a ToolMessage, letting a
        vision-capable chat model inspect the same VNC frame a human sees.
        """
        if not visual_observation or not result.get("ok"):
            return result
        screen = runtime.capture_screen(owner_id=owner_id)
        if not screen.get("ok"):
            return {**result, "observation": {"ok": False, "code": screen.get("code"), "message": screen.get("message")}}
        return [
            {
                "type": "text",
                "text": json.dumps(
                    {**result, "observation": "Screenshot terbaru terlampir. Verifikasi hasil aksi dari layar sebelum melanjutkan atau mengklaim berhasil."},
                    ensure_ascii=False,
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:{screen['mime_type']};base64,{screen['image_base64']}"},
            },
        ]

    @tool
    def computer_get_status() -> dict:
        """Check whether the owner-assigned interactive computer is online. Return the viewer URL when the owner needs to take over, especially for sign-in or sensitive actions."""
        return runtime.status(owner_id=owner_id)

    @tool
    def computer_open_url(url: str) -> dict | list[dict[str, Any]]:
        """Open one http(s) URL in Chrome, then inspect the returned screen before claiming navigation succeeded."""
        return observed(runtime.open_url(owner_id=owner_id, url=url))

    @tool
    def computer_click(x: int, y: int) -> dict | list[dict[str, Any]]:
        """Click visible coordinates, then inspect the returned screen before choosing the next action. Never use for sensitive confirmations without owner approval."""
        return observed(runtime.click(owner_id=owner_id, x=x, y=y))

    @tool
    def computer_type_text(text: str) -> dict | list[dict[str, Any]]:
        """Type ordinary non-sensitive text, then inspect the returned screen. Never type passwords, OTPs, API keys, or access tokens."""
        return observed(runtime.type_text(owner_id=owner_id, text=text))

    @tool
    def computer_press_key(key: str) -> dict | list[dict[str, Any]]:
        """Press one navigation key/combo, then inspect the returned screen before assuming its effect."""
        return observed(runtime.press_key(owner_id=owner_id, key=key))

    if visual_observation:
        @tool
        def computer_screenshot() -> dict | list[dict[str, Any]]:
            """Observe the actual current computer screen. Always call this before the first visual action and after uncertainty or a failed verification."""
            screen = runtime.capture_screen(owner_id=owner_id)
            if not screen.get("ok"):
                return screen
            return [
                {"type": "text", "text": "Screenshot komputer terbaru terlampir. Tentukan satu aksi kecil berdasarkan apa yang benar-benar terlihat."},
                {"type": "image_url", "image_url": {"url": f"data:{screen['mime_type']};base64,{screen['image_base64']}"}},
            ]
        return [computer_get_status, computer_screenshot, computer_open_url, computer_click, computer_type_text, computer_press_key]

    return [computer_get_status, computer_open_url, computer_click, computer_type_text, computer_press_key]
