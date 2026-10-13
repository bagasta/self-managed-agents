"""One-shot human input worker. Values arrive through stdin, never argv or files."""
from __future__ import annotations

import json
import logging
import sys
import time


def main() -> int:
    logging.disable(logging.CRITICAL)
    from vncdotool import api

    try:
        payload = json.loads(sys.stdin.read(16384))
        with api.connect(payload["server"], timeout=10) as client:
            for key in ("ctrl", "alt", "shift", "super"):
                client.keyUp(key)
            client.mouseMove(payload["x"], payload["y"])
            client.mousePress(1)
            time.sleep(0.25)
            if payload["replace"]:
                client.keyDown("ctrl")
                time.sleep(0.08)
                client.keyPress("a")
                time.sleep(0.08)
                client.keyUp("ctrl")
            for character in payload["value"]:
                code = ord(character)
                keysym = code if code <= 255 else 0x01000000 | code
                client.keyEvent(keysym, down=True)
                client.keyEvent(keysym, down=False)
                time.sleep(0.035)
            time.sleep(0.15)
        return 0
    except Exception:
        # VNC errors can carry key events. Never print the exception or input.
        return 1
    finally:
        api.shutdown()


if __name__ == "__main__":
    sys.exit(main())
