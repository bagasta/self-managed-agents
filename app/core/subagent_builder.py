"""Backward-compatible import alias for subagent builder internals."""
from __future__ import annotations

import sys

from app.core.engine import subagent_builder as _impl

sys.modules[__name__] = _impl
