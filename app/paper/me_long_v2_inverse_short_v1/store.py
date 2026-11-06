"""Persistence for the ME_LONG_V2_INVERSE_SHORT_V1 prospective paper contour.

Two implementations behind ONE small interface:

``InverseShortStore`` / ``InMemoryInverseShortStore``
    Process-local, transaction-free store used by unit tests and by the
    pre-activation dry-run.  It enforces exactly the same invariants as the
    PostgreSQL store (activation guard, idempotent event keys, immutable
    ``entered_at`` / ``deadline_at``) so dry-run behaviour is representative.

``PgInverseShortStore``
    PostgreSQL implementation over a DEDICATED connection (never the scanner
    or paper runner transaction), writing only to the contour's own tables:

        dds.me_long_v2_inverse_short_v1_activation
        dds.me_long_v2_inverse_short_v1_event
        dds.me_long_v2_inverse_short_v1_position
        dds.me_long_v2_inverse_short_v1_close_outbox   (durable close staging)

Isolation invariants enforced here:
  * events are idempotent on ``event_key`` (``ON CONFLICT DO NOTHING``);
  * positions are idempotent on the NAMESPACED ``position_key``;
  * the position UPDATE statement never touches ``entered_at`` /
    ``deadline_at`` (or any entry column) — restart recovery reads back the
    original entry time and the original deadline verbatim.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable

from .activation import evaluate_activation
from .models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_ENTERED,
    ActivationRecord,
    InverseShortEvent,
    InverseShortPosition,
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from .protocol import EXPERIMENT_ID, protocol_fingerprint

logger = logging.getLogger(__name__)

SCHEMA = "dds"
EVENT_TABLE = f"{SCHEMA}.me_long_v2_inverse_short_v1_event"
POSITION_TABLE = f"{SCHEMA}.me_long_v2_inverse_short_v1_position"
ACTIVATION_TABLE = f"{SCHEMA}.me_long_v2_inverse_short_v1_activation"
# Durable close outbox (P1-3): terminal snapshots staged BEFORE the position
# row flips to CLOSED, so an exit can never lose its facts to a failed/lost
# UPDATE or a restart.  Populated only while a close is being persisted.
CLOSE_OUTBOX_TABLE = f"{SCHEMA}.me_long_v2_inverse_short_v1_close_outbox"

# Honest save_event outcomes.
SAVE_CAPTURED = "CAPTURED"
SAVE_DUPLICATE = "DUPLICATE"
SAVE_NOT_ACTIVE = "NOT_ACTIVE"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def contour_balance_components(
    positions: Iterable[InverseShortPosition],
    initial_balance: float,
) -> dict[str, float]:
    """Reconstruct the contour balance from its own append-only ledger.

    ``balance = initial + Σ_closed net_pnl − Σ_open(entry costs + funding)``

    Exact under the engine's accounting (entry costs are booked at entry,
    funding at settlement, exit costs + gross at close), so a restart with no
    account snapshot restores the same balance the process had before.
    """
    closed_net = 0.0
    open_costs = 0.0
    for position in positions:
        if position.status == POSITION_STATUS_CLOSED:
            closed_net += position.net_pnl
        else:
            open_costs += position.entry_fee + position.entry_slippage_cost
            open_costs += position.funding_paid
    return {
        "initial_balance": float(initial_balance),
        "closed_net_pnl": closed_net,
        "open_costs": open_costs,
        "balance": initial_balance + closed_net - open_costs,
    }


class InverseShortStore:
    """Interface of the contour's persistence layer."""

    # -- activation -------------------------------------------------------
    def load_activation(self) -> ActivationRecord | None:
        raise NotImplementedError

    # -- events -----------------------------------------------------------
    def save_event(self, event: InverseShortEvent) -> str:
        raise NotImplementedError

    def get_event(self, event_key: str) -> InverseShortEvent | None:
        raise NotImplementedError

    def list_events(self, status: str | None = None) -> list[InverseShortEvent]:
        raise NotImplementedError

    def mark_event_entered(self, event_key: str, position_key: str) -> None:
        raise NotImplementedError

    # -- positions --------------------------------------------------------
    def save_position(self, position: InverseShortPosition) -> bool:
        raise NotImplementedError

    def update_position(self, position: InverseShortPosition) -> None:
        raise NotImplementedError

    def load_open_positions(self) -> list[InverseShortPosition]:
        raise NotImplementedError

    def has_position(self, position_key: str) -> bool:
        raise NotImplementedError

    def list_positions(self, status: str | None = None) -> list[InverseShortPosition]:
        raise NotImplementedError

    def balance_components(self) -> dict[str, float]:
        raise NotImplementedError

    def net_pnl_summary(self) -> dict[str, float]:
        raise NotImplementedError

    # -- durable close outbox (P1-3) --------------------------------------
    def stage_close(self, position: InverseShortPosition) -> bool:
        """Durably stage the EXACT terminal snapshot BEFORE the status flip.

        Returns True when the snapshot is durable (outbox row written),
        False when durability is degraded (outbox table absent, migration
        086 not applied) — the caller keeps the in-process snapshot either
        way.  An exception means the write did NOT commit: the engine must
        retain the snapshot and retry staging BEFORE attempting the
        terminal UPDATE on subsequent cycles (P1 contract).
        """
        raise NotImplementedError

    def list_pending_closes(self) -> list[InverseShortPosition]:
        """Terminal snapshots staged but not yet confirmed CLOSED."""
        raise NotImplementedError

    def clear_pending_close(self, position_key: str) -> None:
        """Drop the staged snapshot after the status flip is confirmed."""
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - trivial
        return None


class InMemoryInverseShortStore(InverseShortStore):
    """Process-local store with the same invariants as the PG store."""

    def __init__(
        self,
        activation: ActivationRecord | None = None,
        *,
        initial_balance: float = 10_000.0,
    ) -> None:
        self._activation = activation
        self._events: dict[str, InverseShortEvent] = {}
        self._positions: dict[str, InverseShortPosition] = {}
        self.initial_balance = float(initial_balance)
        self.immutability_repairs = 0
        # Durable-close mirror: terminal snapshots staged but not confirmed.
        self._pending_closes: dict[str, InverseShortPosition] = {}

    # -- activation -------------------------------------------------------
    def set_activation(self, activation: ActivationRecord | None) -> None:
        self._activation = activation

    def load_activation(self) -> ActivationRecord | None:
        return self._activation

    # -- events -----------------------------------------------------------
    def save_event(self, event: InverseShortEvent) -> str:
        # Same atomic guard as the PG INSERT ... WHERE EXISTS clause.
        active, _ = evaluate_activation(self._activation, enabled=True)
        if active is None:
            return SAVE_NOT_ACTIVE
        if event.event_key in self._events:
            return SAVE_DUPLICATE
        self._events[event.event_key] = replace(event, lineage=dict(event.lineage))
        return SAVE_CAPTURED

    def get_event(self, event_key: str) -> InverseShortEvent | None:
        return self._events.get(event_key)

    def list_events(self, status: str | None = None) -> list[InverseShortEvent]:
        events = list(self._events.values())
        if status is not None:
            events = [e for e in events if e.status == status]
        return sorted(events, key=lambda e: (e.signal_time, e.event_key))

    def mark_event_entered(self, event_key: str, position_key: str) -> None:
        event = self._events.get(event_key)
        if event is None:
            raise KeyError(event_key)
        event.status = EVENT_STATUS_ENTERED
        event.position_key = position_key

    # -- positions --------------------------------------------------------
    def save_position(self, position: InverseShortPosition) -> bool:
        if position.position_key in self._positions:
            return False
        self._positions[position.position_key] = replace(position)
        return True

    def update_position(self, position: InverseShortPosition) -> None:
        stored = self._positions.get(position.position_key)
        if stored is None:
            raise KeyError(position.position_key)
        # entered_at / deadline_at are IMMUTABLE: a divergence is repaired
        # back to the stored values, never persisted forward.
        if position.entered_at != stored.entered_at:
            position.entered_at = stored.entered_at
            self.immutability_repairs += 1
        if position.deadline_at != stored.deadline_at:
            position.deadline_at = stored.deadline_at
            self.immutability_repairs += 1
        self._positions[position.position_key] = replace(position)

    def load_open_positions(self) -> list[InverseShortPosition]:
        return [
            replace(p) for p in self._positions.values()
            if p.status == POSITION_STATUS_OPEN
        ]

    def has_position(self, position_key: str) -> bool:
        return position_key in self._positions

    def list_positions(self, status: str | None = None) -> list[InverseShortPosition]:
        positions = [replace(p) for p in self._positions.values()]
        if status is not None:
            positions = [p for p in positions if p.status == status]
        return sorted(positions, key=lambda p: p.entered_at)

    def balance_components(self) -> dict[str, float]:
        return contour_balance_components(
            self._positions.values(), self.initial_balance,
        )

    def net_pnl_summary(self) -> dict[str, float]:
        closed = [p for p in self._positions.values() if p.status == POSITION_STATUS_CLOSED]
        net = sum(p.net_pnl for p in closed)
        wins = sum(1 for p in closed if p.net_pnl > 0)
        losses = sum(1 for p in closed if p.net_pnl <= 0)
        return {
            "closed_trades": float(len(closed)),
            "wins": float(wins),
            "losses": float(losses),
            "net_pnl": net,
        }

    # -- durable close outbox (P1-3) --------------------------------------
    def stage_close(self, position: InverseShortPosition) -> bool:
        """Stage the exact terminal snapshot (process-local durability)."""
        if (
            position.status != POSITION_STATUS_CLOSED
            or not position.exit_reason
            or position.closed_at is None
        ):
            raise ValueError("STAGE_CLOSE_NOT_TERMINAL")
        self._pending_closes[position.position_key] = replace(position)
        return True

    def list_pending_closes(self) -> list[InverseShortPosition]:
        return [
            replace(self._pending_closes[key])
            for key in sorted(self._pending_closes)
        ]

    def clear_pending_close(self, position_key: str) -> None:
        self._pending_closes.pop(position_key, None)


# ---------------------------------------------------------------------------
# PostgreSQL implementation
# ---------------------------------------------------------------------------

_EVENT_COLUMNS = (
    "event_key", "strategy_id", "protocol_version", "protocol_hash",
    "source_scanner", "source_scanner_version", "source_direction",
    "source_setup_id", "symbol", "direction", "entry_timeframe",
    "signal_time", "captured_at", "activation_freeze_ts",
    "signal_candle_open_time", "source_reference_price",
    "source_invalidation_price", "source_target_price", "source_target_2",
    "source_score", "market_regime", "capture_route", "status",
    "position_key", "lineage",
)

_POSITION_COLUMNS = (
    "position_key", "event_key", "strategy_id", "symbol", "direction",
    "source_scanner", "source_setup_id", "market_regime", "source_score",
    "entry_market_price", "entry_price", "entry_fee", "entry_slippage",
    "stop_price", "target_price", "position_size", "risk_usdt",
    "balance_before", "entered_at", "deadline_at", "status",
    "funding_paid", "funding_periods", "highest_since_entry",
    "lowest_since_entry", "mfe", "mae", "exit_price", "exit_reason",
    "exit_fee", "slippage", "gross_pnl", "net_pnl", "closed_at",
    "duration_sec",
)

_EVENT_VALUES_SQL = ", ".join(
    "%s::jsonb" if column == "lineage" else "%s" for column in _EVENT_COLUMNS
)
_POSITION_VALUES_SQL = ", ".join("%s" for _ in _POSITION_COLUMNS)

INSERT_EVENT_SQL = f"""
    INSERT INTO {EVENT_TABLE} ({", ".join(_EVENT_COLUMNS)})
    SELECT {_EVENT_VALUES_SQL}
    WHERE EXISTS (
        SELECT 1 FROM {ACTIVATION_TABLE}
        WHERE experiment_id = %s
          AND status = 'RUNNING'
          AND protocol_fingerprint = %s
          AND freeze_ts IS NOT NULL
    )
    ON CONFLICT (event_key) DO NOTHING
"""

INSERT_POSITION_SQL = f"""
    INSERT INTO {POSITION_TABLE} ({", ".join(_POSITION_COLUMNS)})
    VALUES ({_POSITION_VALUES_SQL})
    ON CONFLICT (position_key) DO NOTHING
"""

# NOTE: entered_at / deadline_at and every entry column are deliberately
# absent from the mutable column list — the original entry time and the
# original deadline survive restarts untouched.
UPDATE_POSITION_SQL = f"""
    UPDATE {POSITION_TABLE} SET
        funding_paid = %s,
        funding_periods = %s,
        highest_since_entry = %s,
        lowest_since_entry = %s,
        mfe = %s,
        mae = %s,
        status = %s,
        exit_price = %s,
        exit_reason = %s,
        exit_fee = %s,
        slippage = %s,
        gross_pnl = %s,
        net_pnl = %s,
        closed_at = %s,
        duration_sec = %s,
        updated_at = now()
    WHERE position_key = %s
"""

MARK_EVENT_ENTERED_SQL = f"""
    UPDATE {EVENT_TABLE}
    SET status = %s, position_key = %s, updated_at = now()
    WHERE event_key = %s AND status = %s
"""

# Durable close outbox (P1-3): staged BEFORE the position row flips to
# CLOSED.  Re-staging the same key is an idempotent overwrite (same terminal
# facts), so a retry can never change the exit record.
INSERT_CLOSE_OUTBOX_SQL = f"""
    INSERT INTO {CLOSE_OUTBOX_TABLE}
        (position_key, symbol, exit_reason, closed_at, snapshot)
    VALUES (%s, %s, %s, %s, %s::jsonb)
    ON CONFLICT (position_key) DO UPDATE SET
        symbol = EXCLUDED.symbol,
        exit_reason = EXCLUDED.exit_reason,
        closed_at = EXCLUDED.closed_at,
        snapshot = EXCLUDED.snapshot,
        staged_at = now()
"""

LIST_CLOSE_OUTBOX_SQL = f"""
    SELECT position_key, snapshot
    FROM {CLOSE_OUTBOX_TABLE}
    ORDER BY staged_at, position_key
"""

DELETE_CLOSE_OUTBOX_SQL = f"""
    DELETE FROM {CLOSE_OUTBOX_TABLE} WHERE position_key = %s
"""


def _is_missing_relation(exc: Exception) -> bool:
    """PostgreSQL 42P01 (undefined table) — outbox migration not applied yet.

    The close path must stay functional on a database that only has
    migrations 084/085 (migration-safe rollout): durability degrades to the
    in-process snapshot instead of failing the close.
    """
    if str(getattr(exc, "sqlstate", "") or "") == "42P01":
        return True
    text = str(exc).lower()
    return "does not exist" in text and ("relation" in text or "table" in text)


class _QueryResult:
    """Materialized result of one statement.

    The cursor is read (``description`` / rows / ``rowcount``) INSIDE
    ``_execute`` before it is closed, so a caller can never observe a closed
    cursor and a cursor can never leak (P1-1).
    """

    __slots__ = ("columns", "rows", "rowcount")

    def __init__(self, columns: list[str], rows: list[tuple], rowcount: int) -> None:
        self.columns = columns
        self.rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple]:
        return list(self.rows)

    def fetchone_dict(self) -> dict[str, Any] | None:
        row = self.fetchone()
        return dict(zip(self.columns, row)) if row is not None else None

    def fetchall_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


class PgInverseShortStore(InverseShortStore):
    """PostgreSQL store over a dedicated connection.

    All methods commit their own transaction and roll back on failure so the
    shared connection is never left aborted.  Every write is idempotent.
    """

    def __init__(
        self,
        conn: Any,
        *,
        experiment_id: str = EXPERIMENT_ID,
        initial_balance: float = 10_000.0,
        expected_fingerprint: str | None = None,
    ) -> None:
        self._conn = conn
        self._experiment_id = experiment_id
        self.initial_balance = float(initial_balance)
        self._expected_fingerprint = expected_fingerprint or protocol_fingerprint()

    # -- helpers ----------------------------------------------------------
    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:  # pragma: no cover - defensive
            logger.debug("me inverse short store rollback failed", exc_info=True)

    def _execute(
        self, sql: str, params: tuple | list = (), *, fetch: str = "all",
    ) -> _QueryResult:
        """Execute one statement and MATERIALIZE its results (P1-1).

        ``description``, rows and ``rowcount`` are read inside the ``try``
        block BEFORE the cursor is closed in ``finally`` — a result is never
        read from a closed cursor, and every cursor is closed exactly once.
        ``fetch``: ``"all"`` / ``"one"`` (SELECT paths) or ``"none"``
        (INSERT/UPDATE/DELETE paths — only ``rowcount`` is kept).
        """
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, params)
            description = cursor.description
            columns = [d[0] for d in (description or [])]
            if description is None:
                rows: list[tuple] = []
            elif fetch == "one":
                row = cursor.fetchone()
                rows = [row] if row is not None else []
            elif fetch == "none":
                rows = []
            else:
                rows = list(cursor.fetchall())
            rowcount = int(getattr(cursor, "rowcount", 0) or 0)
            return _QueryResult(columns=columns, rows=rows, rowcount=rowcount)
        finally:
            try:
                cursor.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def _query(
        self, sql: str, params: tuple | list = (), *, fetch: str = "all",
    ) -> _QueryResult:
        """Read helper: materialize, then promptly commit the read.

        On failure the transaction is rolled back, so a failed read never
        leaves the shared connection with a hanging/aborted transaction.
        """
        try:
            result = self._execute(sql, params, fetch=fetch)
            self._conn.commit()
            return result
        except Exception:
            self._rollback()
            raise

    # -- activation -------------------------------------------------------
    def load_activation(self) -> ActivationRecord | None:
        result = self._query(
            "SELECT experiment_id, status, protocol_fingerprint, freeze_ts "
            f"FROM {ACTIVATION_TABLE} WHERE experiment_id = %s",
            (self._experiment_id,),
            fetch="one",
        )
        row = result.fetchone()
        if row is None:
            return None
        return ActivationRecord(
            experiment_id=row[0],
            status=row[1],
            protocol_fingerprint=row[2] or "",
            freeze_ts=row[3],
        )

    # -- events -----------------------------------------------------------
    def save_event(self, event: InverseShortEvent) -> str:
        active, reason = evaluate_activation(
            self.load_activation(), enabled=True,
            expected_fingerprint=self._expected_fingerprint,
        )
        if active is None:
            logger.debug("me inverse short event refused: %s", reason)
            return SAVE_NOT_ACTIVE

        row = event.to_dict()
        lineage = json.dumps(row.pop("lineage") or {}, ensure_ascii=False)
        params: list[Any] = [
            row.get("event_key"), row.get("strategy_id"),
            row.get("protocol_version"), row.get("protocol_hash"),
            row.get("source_scanner"), row.get("source_scanner_version"),
            row.get("source_direction"), row.get("source_setup_id"),
            row.get("symbol"), row.get("direction"), row.get("entry_timeframe"),
            row.get("signal_time"), row.get("captured_at"),
            row.get("activation_freeze_ts"), row.get("signal_candle_open_time"),
            row.get("source_reference_price"), row.get("source_invalidation_price"),
            row.get("source_target_price"), row.get("source_target_2"),
            row.get("source_score"), row.get("market_regime"),
            row.get("capture_route"), row.get("status"), row.get("position_key"),
            lineage,
            # WHERE EXISTS guard:
            self._experiment_id, self._expected_fingerprint,
        ]
        try:
            result = self._execute(INSERT_EVENT_SQL, tuple(params), fetch="none")
            inserted = result.rowcount == 1
            self._conn.commit()
        except Exception:
            self._rollback()
            logger.exception("me inverse short event insert failed")
            raise
        if inserted:
            return SAVE_CAPTURED
        # Guard failure (activation gone) vs. idempotent replay.
        if self.get_event(event.event_key) is not None:
            return SAVE_DUPLICATE
        return SAVE_NOT_ACTIVE

    def get_event(self, event_key: str) -> InverseShortEvent | None:
        rows = self._query(
            "SELECT * FROM " + EVENT_TABLE + " WHERE event_key = %s",
            (event_key,),
        ).fetchall_dicts()
        return InverseShortEvent.from_dict(rows[0]) if rows else None

    def list_events(self, status: str | None = None) -> list[InverseShortEvent]:
        if status is None:
            result = self._query(
                "SELECT * FROM " + EVENT_TABLE + " ORDER BY signal_time, event_key",
            )
        else:
            result = self._query(
                "SELECT * FROM " + EVENT_TABLE +
                " WHERE status = %s ORDER BY signal_time, event_key",
                (status,),
            )
        return [
            InverseShortEvent.from_dict(r) for r in result.fetchall_dicts()
        ]

    def mark_event_entered(self, event_key: str, position_key: str) -> None:
        try:
            self._execute(
                MARK_EVENT_ENTERED_SQL,
                (EVENT_STATUS_ENTERED, position_key, event_key, EVENT_STATUS_CAPTURED),
                fetch="none",
            )
            self._conn.commit()
        except Exception:
            self._rollback()
            raise

    # -- positions --------------------------------------------------------
    def _position_params(self, position: InverseShortPosition) -> tuple:
        row = position.to_dict()
        return tuple(row.get(column) for column in _POSITION_COLUMNS)

    def save_position(self, position: InverseShortPosition) -> bool:
        try:
            result = self._execute(
                INSERT_POSITION_SQL, self._position_params(position),
                fetch="none",
            )
            inserted = result.rowcount == 1
            self._conn.commit()
            return inserted
        except Exception:
            self._rollback()
            raise

    def update_position(self, position: InverseShortPosition) -> None:
        row = position.to_dict()
        params = (
            row.get("funding_paid"), row.get("funding_periods"),
            row.get("highest_since_entry"), row.get("lowest_since_entry"),
            row.get("mfe"), row.get("mae"), row.get("status"),
            row.get("exit_price"), row.get("exit_reason"), row.get("exit_fee"),
            row.get("slippage"), row.get("gross_pnl"), row.get("net_pnl"),
            row.get("closed_at"), row.get("duration_sec"),
            position.position_key,
        )
        try:
            result = self._execute(UPDATE_POSITION_SQL, params, fetch="none")
            updated = result.rowcount
            self._conn.commit()
        except Exception:
            self._rollback()
            raise
        if updated == 0:
            # A confirmed write MUST have matched the row: silently losing a
            # close/funding update would corrupt the ledger.
            raise RuntimeError(
                f"POSITION_UPDATE_MATCHED_NO_ROWS:{position.position_key}"
            )

    def load_open_positions(self) -> list[InverseShortPosition]:
        rows = self._query(
            "SELECT * FROM " + POSITION_TABLE +
            " WHERE status = %s ORDER BY entered_at",
            (POSITION_STATUS_OPEN,),
        ).fetchall_dicts()
        return [InverseShortPosition.from_dict(r) for r in rows]

    def has_position(self, position_key: str) -> bool:
        result = self._query(
            "SELECT 1 FROM " + POSITION_TABLE + " WHERE position_key = %s",
            (position_key,),
            fetch="one",
        )
        return result.fetchone() is not None

    def list_positions(self, status: str | None = None) -> list[InverseShortPosition]:
        if status is None:
            result = self._query(
                "SELECT * FROM " + POSITION_TABLE + " ORDER BY entered_at",
            )
        else:
            result = self._query(
                "SELECT * FROM " + POSITION_TABLE +
                " WHERE status = %s ORDER BY entered_at",
                (status,),
            )
        return [InverseShortPosition.from_dict(r) for r in result.fetchall_dicts()]

    def balance_components(self) -> dict[str, float]:
        result = self._query(
            "SELECT "
            " COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN net_pnl ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN status = 'OPEN' THEN "
            "   entry_fee + entry_slippage + funding_paid ELSE 0 END), 0)"
            f" FROM {POSITION_TABLE}",
            fetch="one",
        )
        row = result.fetchone() or (0.0, 0.0)
        closed_net = float(row[0] or 0.0)
        open_costs = float(row[1] or 0.0)
        return {
            "initial_balance": self.initial_balance,
            "closed_net_pnl": closed_net,
            "open_costs": open_costs,
            "balance": self.initial_balance + closed_net - open_costs,
        }

    def net_pnl_summary(self) -> dict[str, float]:
        result = self._query(
            "SELECT COUNT(*), "
            " COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(net_pnl), 0)"
            f" FROM {POSITION_TABLE} WHERE status = 'CLOSED'",
            fetch="one",
        )
        row = result.fetchone() or (0, 0, 0, 0.0)
        return {
            "closed_trades": float(row[0] or 0),
            "wins": float(row[1] or 0),
            "losses": float(row[2] or 0),
            "net_pnl": float(row[3] or 0.0),
        }

    # -- durable close outbox (P1-3) --------------------------------------
    def stage_close(self, position: InverseShortPosition) -> bool:
        if (
            position.status != POSITION_STATUS_CLOSED
            or not position.exit_reason
            or position.closed_at is None
        ):
            raise ValueError("STAGE_CLOSE_NOT_TERMINAL")
        snapshot = json.dumps(position.to_dict(), ensure_ascii=False)
        params = (
            position.position_key, position.symbol, position.exit_reason,
            position.closed_at, snapshot,
        )
        try:
            result = self._execute(INSERT_CLOSE_OUTBOX_SQL, params, fetch="none")
            staged = result.rowcount == 1
            self._conn.commit()
            return staged
        except Exception as exc:
            self._rollback()
            if _is_missing_relation(exc):
                logger.warning(
                    "me inverse short close outbox unavailable (migration 086 "
                    "not applied): terminal close durability degraded to "
                    "in-process retry: key=%s",
                    position.position_key[:48],
                )
                return False
            raise

    def list_pending_closes(self) -> list[InverseShortPosition]:
        try:
            result = self._query(LIST_CLOSE_OUTBOX_SQL)
        except Exception as exc:
            if _is_missing_relation(exc):
                logger.warning(
                    "me inverse short close outbox table missing (migration 086 "
                    "not applied): no durable closes to resume",
                )
                return []
            raise
        pending: list[InverseShortPosition] = []
        for row in result.fetchall_dicts():
            raw = row.get("snapshot")
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
                pending.append(InverseShortPosition.from_dict(data))
            except Exception:
                logger.error(
                    "me inverse short close outbox row unreadable: key=%s",
                    str(row.get("position_key"))[:48], exc_info=True,
                )
        return pending

    def clear_pending_close(self, position_key: str) -> None:
        try:
            self._execute(DELETE_CLOSE_OUTBOX_SQL, (position_key,), fetch="none")
            self._conn.commit()
        except Exception as exc:
            self._rollback()
            if _is_missing_relation(exc):
                return  # degraded mode: nothing staged, nothing to clear
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("me inverse short store close failed", exc_info=True)


def connect_pg_store(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    initial_balance: float = 10_000.0,
    unix_sock: str | None = None,
) -> PgInverseShortStore:
    """Open a DEDICATED pg8000 connection for the contour.

    The connection is never shared with the scanner/paper runners: contour
    commits and rollbacks can never touch a production transaction.
    """
    import pg8000

    kwargs: dict[str, Any] = {
        "database": database,
        "user": user,
        "password": password,
    }
    if unix_sock:
        kwargs["unix_sock"] = unix_sock
    else:
        kwargs["host"] = host
        kwargs["port"] = port
    conn = pg8000.connect(**kwargs)
    return PgInverseShortStore(conn, initial_balance=initial_balance)
