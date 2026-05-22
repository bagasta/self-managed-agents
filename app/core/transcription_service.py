"""Backward-compatible import alias for transcription service internals."""
from __future__ import annotations

import sys

from app.core.infra import transcription_service as _impl

sys.modules[__name__] = _impl
