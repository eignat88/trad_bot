"""Isolated PostgreSQL connection lifecycle for SRR outcome writer.

This module does not enable runtime persistence.
"""

from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pg8000

from app.research.srr_short_execution_outcome_persistence import SrrOutcomeWriter


@contextmanager
def open_srr_outcome_writer(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str | None = None,
    reader_conn: Any = None,
    connect_factory: Callable[..., Any] | None = None,
    activation_gate: Any = None,
) -> Iterator[SrrOutcomeWriter]:
    """Create and own a separate writer connection.

    ``activation_gate`` is an optional ``SrrShortWriterActivationGate``. When
    provided, the gate is consulted for every write before any SQL is issued.
    Without an explicitly authorized gate the writer remains observe-only at
    the evaluator level; this manager itself does not enable writes.
    """

    factory = connect_factory or pg8000.connect
    conn = None

    try:
        conn = factory(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            timeout=5,
        )

        if conn is None or conn is reader_conn:
            raise RuntimeError(
                "SRR writer must own an independent PostgreSQL connection"
            )

        yield SrrOutcomeWriter(conn, activation_gate=activation_gate)

    finally:
        if conn is not None and conn is not reader_conn:
            import sys

            original_error_active = sys.exc_info()[0] is not None
            try:
                conn.rollback()
            except Exception:
                pass

            try:
                conn.close()
            except Exception:
                if not original_error_active:
                    raise
