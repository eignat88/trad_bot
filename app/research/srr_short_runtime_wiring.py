"""Fail-closed SRR SHORT runtime wiring.

Preparation only. Production outcome writes remain disabled unless an
explicitly authorized activation boundary and writer mode are supplied.
"""

from contextlib import contextmanager
from typing import Any, Iterator

from app.research.srr_short_writer_activation_boundary_v1 import (
    SrrActivationConfigError,
    SrrShortWriterActivationBoundary,
    SrrShortWriterActivationMode,
)
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
    activation_ts: Any = None,
    activation_mode: SrrShortWriterActivationMode | None = None,
    activation_record: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Prepare writer integration without allowing runtime activation.

    The context manager remains strictly observe-only unless *all* of the
    following are satisfied:

    - ``enable_writes=True`` is explicitly passed;
    - ``activation_mode.allows_writes`` is True;
    - a valid ``activation_ts`` with explicit timezone is supplied;
    - the runtime boundary matches the persisted activation record.

    Any missing or inconsistent input raises and no database connection is
    opened. The production runner calls this with ``enable_writes=False`` and
    therefore never reaches the activation checks.
    """

    # Deliberate hard gate; no DB connection may be opened here.
    if enable_writes:
        # Fail closed if any activation input is missing or inconsistent.
        if activation_mode is None or not activation_mode.allows_writes:
            raise SrrRuntimeWriteBlocked(
                "SRR_SHORT_RUNTIME_WRITES_BLOCKED: "
                "activation review and explicit authorization required"
            )
        if activation_ts is None:
            raise SrrActivationConfigError(
                "SRR_SHORT_ACTIVATION_TS_MISSING: explicit ACTIVATION_TS required"
            )
        boundary = SrrShortWriterActivationBoundary.create(activation_ts)

        from app.research.srr_short_writer_activation_boundary_v1 import (
            SrrShortWriterActivationGate,
        )

        gate = SrrShortWriterActivationGate(
            boundary=boundary, mode=activation_mode
        )
        if activation_record is not None:
            gate.verify_persisted_record(activation_record)
        # Without a persisted record the gate is never cleared for writes;
        # a direct writer call is still rejected by the gate itself.
        writer_activation_gate = gate

        with open_srr_outcome_writer(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            reader_conn=reader_conn,
            activation_gate=writer_activation_gate,
        ) as writer:
            yield writer
        return

    # No callback is exposed in observe-only mode.
    yield None
