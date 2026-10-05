"""LangChain tools for an owner-assigned interactive computer."""
from __future__ import annotations

from langchain_core.tools import tool

from app.core.infra.computer_runtime import ComputerRuntime


def build_computer_tools(runtime: ComputerRuntime, *, owner_id: str | None) -> list:
    @tool
    def computer_get_status() -> dict:
        """Check whether the owner-assigned interactive computer is online. Return the viewer URL when the owner needs to take over, especially for sign-in or sensitive actions."""
        return runtime.status(owner_id=owner_id)

    @tool
    def computer_open_url(url: str) -> dict:
        """Open one http(s) URL in Chrome on the assigned computer. Use this only after computer_get_status returns ready."""
        return runtime.open_url(owner_id=owner_id, url=url)

    @tool
    def computer_click(x: int, y: int) -> dict:
        """Click visible screen coordinates on the assigned computer. Do not use this for destructive actions or sensitive confirmations without explicit owner approval."""
        return runtime.click(owner_id=owner_id, x=x, y=y)

    @tool
    def computer_type_text(text: str) -> dict:
        """Type ordinary non-sensitive text into the currently focused field. Never type passwords, OTPs, API keys, or access tokens. Ask the owner to take over for those."""
        return runtime.type_text(owner_id=owner_id, text=text)

    @tool
    def computer_press_key(key: str) -> dict:
        """Press a navigation key or supported combination such as ENTER, TAB, CTRL-L, or ALT-LEFT on the assigned computer."""
        return runtime.press_key(owner_id=owner_id, key=key)

    return [computer_get_status, computer_open_url, computer_click, computer_type_text, computer_press_key]
