"""JSON parsing and repair helpers for Arthur builder tools."""
from __future__ import annotations

import ast
import json
import re
from typing import Any


def parse_json_arg(value: Any, default: Any, *, expected: type | tuple[type, ...]) -> tuple[Any, str | None]:
    """Accept tool-call args as already-parsed objects or JSON strings."""
    if value is None or value == "":
        return default, None
    if isinstance(value, expected):
        return value, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            try:
                parsed = ast.literal_eval(value)
            except Exception:
                return default, str(exc)
        if isinstance(parsed, expected):
            return parsed, None
        return default, f"expected {expected}, got {type(parsed).__name__}"
    return default, f"expected JSON string or {expected}, got {type(value).__name__}"


def strip_json_wrapper(raw: str) -> str:
    """Remove common LLM wrappers before parsing a JSON object."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text


def extract_balanced_json_object(raw: str) -> str:
    """Extract the first balanced JSON object, even when the JSON is not fully valid."""
    text = strip_json_wrapper(raw)
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object found in model output")

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]

    return text[start:]


def repair_llm_json_text(text: str) -> str:
    """Repair conservative JSON mistakes common in model output."""
    repaired = text.strip().lstrip("\ufeff")
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

    # Missing comma between object fields:
    # {"a": "x"\n "b": "y"} -> {"a": "x",\n "b": "y"}
    repaired = re.sub(
        r'(?<=[}\]"0-9eE])\s*\n\s*(?="[^"\n]+"\s*:)',
        ",\n",
        repaired,
    )
    for literal in ("true", "false", "null"):
        repaired = re.sub(
            rf'(?<={literal})\s*\n\s*(?="[^"\n]+"\s*:)',
            ",\n",
            repaired,
        )

    # Missing comma between array values, especially object/string entries.
    repaired = re.sub(r'(?<=[}\]"])\s*\n\s*(?=\{)', ",\n", repaired)
    repaired = re.sub(r'(?<=")\s*\n\s*(?=")', ",\n", repaired)
    return repaired


def complete_truncated_json(text: str) -> str:
    """Best-effort completion of JSON truncated mid-output (e.g. token limit).

    Closes an open string, drops a dangling trailing comma/key, and balances
    any still-open objects/arrays so json.loads can recover the partial blueprint.
    """
    # Per-open-object state:
    #   'key'      awaiting a key (object start or right after a comma)
    #   'afterkey' key string done, awaiting ':'
    #   'colon'    ':' seen, awaiting a value
    #   'value'    a value is present / complete
    stack: list[str] = []          # '{' or '[' currently open
    obj_state: list[str] = []
    in_string = False
    escaped = False

    def _saw_value() -> None:
        if stack and stack[-1] == "{" and obj_state and obj_state[-1] == "colon":
            obj_state[-1] = "value"

    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                if stack and stack[-1] == "{" and obj_state and obj_state[-1] == "key":
                    obj_state[-1] = "afterkey"
                else:
                    _saw_value()
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            _saw_value()
            stack.append("{")
            obj_state.append("key")
        elif char == "[":
            _saw_value()
            stack.append("[")
        elif char in "}]":
            if stack:
                if stack.pop() == "{" and obj_state:
                    obj_state.pop()
                _saw_value()
        elif char == ":":
            if obj_state and stack and stack[-1] == "{" and obj_state[-1] == "afterkey":
                obj_state[-1] = "colon"
        elif char == ",":
            if obj_state and stack and stack[-1] == "{":
                obj_state[-1] = "key"
        elif char not in " \t\r\n":
            # Start of a bare value (number, true/false/null).
            _saw_value()

    result = text
    if in_string:
        result += '"'
        # An open string is a complete value (or key) once closed.
        if stack and stack[-1] == "{" and obj_state and obj_state[-1] == "key":
            obj_state[-1] = "afterkey"
        else:
            _saw_value()
    result = result.rstrip()

    # Complete a truncated bare literal (e.g. "tru" -> "true").
    literal_match = re.search(r"(?<![\w.])(t|tr|tru|f|fa|fal|fals|n|nu|nul)$", result)
    if literal_match:
        frag = literal_match.group(1)
        for full in ("true", "false", "null"):
            if full.startswith(frag):
                result = result[: literal_match.start(1)] + full
                break

    while stack:
        opener = stack.pop()
        if opener == "[":
            result = re.sub(r",\s*$", "", result.rstrip())
            result += "]"
            continue
        state = obj_state.pop() if obj_state else "value"
        result = result.rstrip()
        if state == "key" and result.endswith(","):
            # Trailing comma with no following member: the prior member is complete.
            result = re.sub(r",\s*$", "", result)
        elif state in ("key", "afterkey"):
            # Dangling key (no colon/value): drop it.
            result = re.sub(r'\s*"(?:[^"\\]|\\.)*"\s*$', "", result)
            result = re.sub(r",\s*$", "", result.rstrip())
        elif state == "colon":
            # Key + colon but no value yet: fill a null so the object parses.
            result += " null"
        result += "}"
    return result


def parse_llm_json_object(raw: str) -> tuple[dict[str, Any], bool]:
    """Parse model JSON with a small deterministic repair pass."""
    candidate = extract_balanced_json_object(raw)
    try:
        parsed = json.loads(candidate)
        repaired = False
    except json.JSONDecodeError:
        repaired_text = repair_llm_json_text(candidate)
        try:
            parsed = json.loads(repaired_text)
        except json.JSONDecodeError:
            # Output was cut off (e.g. token limit) — recover the partial object.
            repaired_text = complete_truncated_json(repaired_text)
            parsed = json.loads(repaired_text)
        repaired = repaired_text != candidate

    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
    return parsed, repaired

