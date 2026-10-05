import shutil
import subprocess
from pathlib import Path

import pytest


def test_arthur_ui_session_lifecycle_in_node_vm():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the Arthur UI lifecycle test")
    test_file = Path(__file__).with_name("arthur_ui_session_lifecycle.test.cjs")
    result = subprocess.run(
        [node, "--test", str(test_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
