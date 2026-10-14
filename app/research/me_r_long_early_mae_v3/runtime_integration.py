"""Disabled-by-default scanner runtime integration preparation.

This module exposes a guarded callback only. It has no production scanner
side effect and is not installed or invoked by existing scanner modules.
"""
from __future__ import annotations

from .guards import require_capture_opt_in, require_runtime_configuration


def optionally_capture_source(pipeline, source: dict, now) -> int | None:
    """Return None unless explicit approved V3 opt-in is configured."""
    try:
        require_runtime_configuration(now)
    except RuntimeError:
        return None
    return pipeline.capture_source(source)
