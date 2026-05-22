"""Backward-compatible import alias for the Deep Agents Docker backend."""
from __future__ import annotations

import sys

from app.core.engine import deep_agent_backend as _impl

sys.modules[__name__] = _impl
