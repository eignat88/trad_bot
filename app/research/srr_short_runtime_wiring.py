"""Fail-closed SRR SHORT runtime wiring.

Preparation only. Production outcome writes remain disabled.
"""

from contextlib import contextmanager
from typing import Any, Iterator

from app.research.srr_short_writer_connection_manager import (
    open_srr_outcome_writer,
)


class SrrRuntimeWriteBlocked(RuntimeError):
    """SRR SHORT outcome persistence has not been activated."""


@contextmanager
def prepare_srr_short_writer(
    *,
    reader_conn: Any,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str | None = None,
    enable_writes: bool = False,
) -> Iterator[Any]:
    """Prepare writer integration without allowing runtime activation."""

    # Deliberate hard gate; no DB connection may be opened here.
    if enable_writes:
        raise SrrRuntimeWriteBlocked(
            "SRR_SHORT_RUNTIME_WRITES_BLOCKED: "
            "activation review and explicit authorization required"
        )

    # No callback is exposed in observe-only mode.
    yield None
