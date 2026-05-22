"""Backward-compatible import alias for sandbox internals."""
from __future__ import annotations

import sys

from app.core.infra import sandbox as _impl

sys.modules[__name__] = _impl
