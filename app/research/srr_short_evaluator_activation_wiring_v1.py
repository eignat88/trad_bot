"""Read-only, fail-closed SRR evaluator activation boundary at startup.

This module does not authorize outcome writes or modify the activation record.
"""
from __future__ import annotations

from typing import Any

from app.research.srr_short_writer_activation_boundary_v1 import (
    SRR_ACTIVATION_EXPERIMENT_ID,
    SrrActivationConfigError,
    SrrActivationRecordUnavailable,
    SrrShortWriterActivationBoundary,
    SrrShortWriterActivationGate,
    SrrShortWriterActivationMode,
)


def resolve_srr_evaluator_activation_boundary(
    conn: Any,
    *,
    writer_enabled: bool,
    activation_ts: Any,
) -> SrrShortWriterActivationBoundary | None:
    """Return a verified boundary only for explicitly enabled writer mode.

    An enabled SRR writer may never run an unfiltered evaluator queue. The
    canonical DB record is checked before the evaluator is constructed.
    In observe-only mode existing dry-run evaluation is preserved.
    """
    if not writer_enabled:
        return None
    if activation_ts is None or not str(activation_ts).strip():
        raise SrrActivationConfigError(
            "SRR_EVALUATOR_ACTIVATION_TS_MISSING: enabled writer needs explicit activation_ts"
        )
    boundary = SrrShortWriterActivationBoundary.create(activation_ts)
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    # SELECT only; no writer connection, mutation or migration.
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT experiment_id, direction, boundary_version, activation_ts, status
            FROM research.srr_short_writer_activation
            WHERE experiment_id = %s
            """,
            (SRR_ACTIVATION_EXPERIMENT_ID,),
        )
        row = cursor.fetchone()
    except Exception as exc:
        raise SrrActivationRecordUnavailable(
            "SRR_EVALUATOR_ACTIVATION_RECORD_UNAVAILABLE: cannot load authoritative record"
        ) from exc
    finally:
        cursor.close()
    if row is None:
        raise SrrActivationRecordUnavailable(
            "SRR_EVALUATOR_ACTIVATION_RECORD_MISSING: writer enabled but no activation record"
        )
    gate.verify_persisted_record(
        {
            "experiment_id": row[0],
            "direction": row[1],
            "boundary_version": row[2],
            "activation_ts": row[3],
            "status": row[4],
        }
    )
    return boundary
