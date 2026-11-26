"""Issue #298 / #312 — executable delayed-entry model (D-1, D-2, D-4).

Research-only, pure: no I/O, no network, no database, no clock reads.

Hard rules enforced here:

* an entry may occur ONLY at an admissible price class STRICTLY AFTER the
  confirmation instant — in candle-sequence order strictly after the
  confirming candle (see :data:`ENTRY_TIMING_POLICY` for the exact µs
  boundary semantics); the confirming candle's close instant/price is
  never an entry;
* the confirmation-candle close is NEVER executable.  It is carried only
  under its reference label
  :data:`~...types.CONFIRMATION_CLOSE_REFERENCE_LABEL`
  (``CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE``);
* entry deadline/expiry per D-4 (:data:`ENTRY_EXPIRED`);
* fill/no-fill per D-2: gap-through, lock-limit and no-liquidity produce
  :data:`~...types.ENTRY_MISSED` — every event stays in the denominator
  accounting (D-8); nothing is dropped;
* only FULLY CLOSED candles at the evaluation ASOF are used (closed-candle
  only path evaluation; ASOF strict at microsecond precision).  A fill that
  is not yet resolvable from closed candles is
  :data:`~...types.ENTRY_PENDING` — never guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.research.me_entry_quality_h2_outcome_v1.policy import (
    ENTRY_PRICE_CLASS_FIRST_TRADE,
    ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL,
    ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN,
    GAP_THROUGH_FILL_AT_OPEN,
    GAP_THROUGH_MISSED_ENTRY,
    EntryDeadline,
    EntrySpec,
    FillPolicy,
)
from app.research.me_entry_quality_h2_outcome_v1.types import (
    CONFIRMATION_CLOSE_REFERENCE_LABEL,
    ENTRY_ENTERED,
    ENTRY_EXPIRED,
    ENTRY_INVALID,
    ENTRY_MISSED,
    ENTRY_PENDING,
    ENTRY_PRICE_UNDETERMINED,
    EXPIRY_REASON_DEADLINE,
    MISSED_REASON_GAP_THROUGH,
    MISSED_REASON_LOCK_LIMIT,
    MISSED_REASON_NO_LIQUIDITY,
    MISSED_REASON_NO_TRADE_PRINT,
    CandleBar,
    ConfirmationInfo,
    TradePrint,
)

ENTRY_MODEL_VERSION = "H2_OUTCOME_ENTRY_MODEL_1.0.0"

#: Executable entry-price labels per admissible price class (D-1).  The
#: confirmation-candle close is NOT a member of any class.
ENTRY_LABEL_NEXT_CANDLE_OPEN = "EXECUTABLE_NEXT_CANDLE_OPEN_V1"
ENTRY_LABEL_FIRST_TRADE = "EXECUTABLE_FIRST_TRADE_AT_OR_AFTER_OPEN_V1"
ENTRY_LABEL_LIMIT_AT_LEVEL = "EXECUTABLE_LIMIT_AT_LEVEL_V1"

#: Intra-candle event times (a level fill inside a bar) have no observable
#: instant in OHLCV data; they are recorded at the bar-open bound and the
#: entry candle is excluded from the wick path (see
#: :data:`~...types.ENTRY_CANDLE_PATH_POLICY`).
INTRACANDLE_TIME_BOUND_POLICY = (
    "INTRACANDLE_EVENT_TIME_RECORDED_AT_BAR_OPEN_BOUND_V1"
)

#: Entry-timing policy (D-1): the entry may occur only at an admissible
#: price class STRICTLY AFTER the confirmation instant in CANDLE-SEQUENCE
#: order — the confirming candle itself (and its close price) is never an
#: entry source.  On a continuous 5m series the IMMEDIATELY NEXT candle's
#: open instant equals the confirming candle's close instant exactly (same
#: µs timestamp, strictly later sequence position); that boundary open is
#: the FIRST admissible entry instant/price class — it is a NEW price formed
#: after the confirmation, and the confirmation-candle close remains
#: reference-only and never executable.  Any entry instant that is not that
#: boundary open must be strictly greater than the confirmation instant at
#: microsecond precision.
ENTRY_TIMING_POLICY = (
    "ENTRY_CANDLE_SEQUENCE_STRICTLY_AFTER_CONFIRMING_CANDLE__"
    "NEXT_OPEN_BOUNDARY_AT_CONFIRMATION_INSTANT_FIRST_ADMISSIBLE_V1"
)

REASON_OK = "OK"
REASON_NO_CONFIRMATION = "NO_CONFIRMED_DELAYED_ENTRY"
REASON_DEADLINE_BEFORE_WINDOW = "DEADLINE_NOT_REACHED_NOT_RESOLVABLE"
REASON_LOCKED_FIRST_BAR = "FIRST_ADMISSIBLE_BAR_LOCKED"
REASON_EMPTY_FIRST_BAR = "FIRST_ADMISSIBLE_BAR_NO_VOLUME"
REASON_FAVORABLE_GAP_THROUGH_FILL = "FAVORABLE_GAP_THROUGH_FILL_AT_OPEN"
REASON_FAVORABLE_GAP_THROUGH_MISSED = "FAVORABLE_GAP_THROUGH_POLICY_MISSED_ENTRY"
REASON_LEVEL_TOUCH_FILL = "LEVEL_TOUCH_FILL_WITHIN_BAR"


@dataclass(frozen=True)
class EntryResolution:
    """Executable entry resolution for ONE leg of ONE source event."""

    status: str
    reason: str
    entry_time_us: int | None
    entry_price: float | None
    entry_price_label: str
    price_class: str
    #: True iff the fill is exactly at the bar open (the whole entry candle
    #: is then post-entry and stays in the wick path)
    fill_at_bar_open: bool
    deadline_us: int
    confirmation_time_us: int | None
    confirmation_close_reference: float | None
    confirmation_close_label: str
    detail: str = ""

    @property
    def entered(self) -> bool:
        return self.status == ENTRY_ENTERED

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_model_version": ENTRY_MODEL_VERSION,
            "status": self.status,
            "reason": self.reason,
            "entry_time_us": self.entry_time_us,
            "entry_price": self.entry_price,
            "entry_price_label": self.entry_price_label,
            "price_class": self.price_class,
            "fill_at_bar_open": self.fill_at_bar_open,
            "deadline_us": self.deadline_us,
            "confirmation_time_us": self.confirmation_time_us,
            "confirmation_close_reference": self.confirmation_close_reference,
            "confirmation_close_label": self.confirmation_close_label,
            "intracandle_time_bound_policy": INTRACANDLE_TIME_BOUND_POLICY,
            "detail": self.detail,
        }


def _not_entered(
    status: str,
    reason: str,
    *,
    price_class: str,
    deadline_us: int,
    confirmation_time_us: int | None,
    confirmation_close_reference: float | None,
    detail: str = "",
) -> EntryResolution:
    return EntryResolution(
        status=status,
        reason=reason,
        entry_time_us=None,
        entry_price=None,
        entry_price_label=ENTRY_PRICE_UNDETERMINED,
        price_class=price_class,
        fill_at_bar_open=False,
        deadline_us=deadline_us,
        confirmation_time_us=confirmation_time_us,
        confirmation_close_reference=confirmation_close_reference,
        confirmation_close_label=CONFIRMATION_CLOSE_REFERENCE_LABEL,
        detail=detail,
    )


def resolve_entry(
    *,
    confirmation: ConfirmationInfo,
    closed: Sequence[CandleBar],
    entry_spec: EntrySpec,
    fill_policy: FillPolicy,
    deadline: EntryDeadline,
    evaluation_asof_us: int,
    direction: str = "SHORT",
    trade_prints: Sequence[TradePrint] = (),
) -> EntryResolution:
    """Resolve the delayed entry strictly after ``confirmation``.

    ``closed`` MUST contain only candles fully closed at
    ``evaluation_asof_us`` (see
    :func:`~...types.closed_candles`).  ``trade_prints`` are used only by
    the FIRST_TRADE_AT_OR_AFTER_OPEN class and are causally filtered to
    ``time_us <= evaluation_asof_us``.
    """
    confirmation_us = confirmation.confirmation_time_us
    close_ref = confirmation.confirmation_close_reference
    if confirmation_us is None:
        return _not_entered(
            ENTRY_INVALID,
            REASON_NO_CONFIRMATION,
            price_class=entry_spec.price_class,
            deadline_us=0,
            confirmation_time_us=None,
            confirmation_close_reference=close_ref,
        )
    deadline_us = confirmation_us + deadline.max_delay_us

    # candidate bars: candle-sequence STRICTLY AFTER the confirming candle
    # (its close == ``confirmation_us``).  The boundary case — the next
    # candle opening at exactly ``confirmation_us`` — is the FIRST
    # admissible entry class (see ENTRY_TIMING_POLICY); the confirming
    # candle itself is structurally excluded (its open precedes its close)
    # and, defensively, also excluded by its open time when known.
    confirming_open_ms = confirmation.confirmation_candle_open_ms
    candidates = [
        bar
        for bar in closed
        if bar.open_time_us >= confirmation_us
        and (
            confirming_open_ms is None
            or bar.open_time_ms != confirming_open_ms
        )
    ]

    def _entry(
        status: str,
        reason: str,
        *,
        time_us: int | None,
        price: float | None,
        label: str,
        at_open: bool,
        detail: str = "",
    ) -> EntryResolution:
        return EntryResolution(
            status=status,
            reason=reason,
            entry_time_us=time_us,
            entry_price=price,
            entry_price_label=label if price is not None else ENTRY_PRICE_UNDETERMINED,
            price_class=entry_spec.price_class,
            fill_at_bar_open=at_open,
            deadline_us=deadline_us,
            confirmation_time_us=confirmation_us,
            confirmation_close_reference=close_ref,
            confirmation_close_label=CONFIRMATION_CLOSE_REFERENCE_LABEL,
            detail=detail,
        )

    in_window = [bar for bar in candidates if bar.open_time_us <= deadline_us]
    deadline_reached = deadline_us <= evaluation_asof_us

    def _no_fill_resolution() -> EntryResolution:
        if not in_window:
            if deadline_reached:
                return _not_entered(
                    ENTRY_EXPIRED,
                    EXPIRY_REASON_DEADLINE,
                    price_class=entry_spec.price_class,
                    deadline_us=deadline_us,
                    confirmation_time_us=confirmation_us,
                    confirmation_close_reference=close_ref,
                )
            return _not_entered(
                ENTRY_PENDING,
                REASON_DEADLINE_BEFORE_WINDOW,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
            )
        if not deadline_reached:
            return _not_entered(
                ENTRY_PENDING,
                REASON_DEADLINE_BEFORE_WINDOW,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
            )
        # deadline reached with admissible bars but no fill: D-2 decides
        # between lock-limit / no-liquidity MISSED_ENTRY and plain expiry
        if all(bar.is_locked for bar in in_window):
            return _not_entered(
                ENTRY_MISSED,
                MISSED_REASON_LOCK_LIMIT,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
            )
        if all(bar.volume == 0.0 for bar in in_window):
            return _not_entered(
                ENTRY_MISSED,
                MISSED_REASON_NO_LIQUIDITY,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
            )
        return _not_entered(
            ENTRY_EXPIRED,
            EXPIRY_REASON_DEADLINE,
            price_class=entry_spec.price_class,
            deadline_us=deadline_us,
            confirmation_time_us=confirmation_us,
            confirmation_close_reference=close_ref,
        )

    # ── D-1: NEXT_CANDLE_OPEN ────────────────────────────────────────────────
    if entry_spec.price_class == ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN:
        if not in_window:
            return _no_fill_resolution()
        first = in_window[0]
        if first.is_locked:
            return _not_entered(
                ENTRY_MISSED,
                MISSED_REASON_LOCK_LIMIT,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
                detail=REASON_LOCKED_FIRST_BAR,
            )
        if first.volume == 0.0:
            return _not_entered(
                ENTRY_MISSED,
                MISSED_REASON_NO_LIQUIDITY,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
                detail=REASON_EMPTY_FIRST_BAR,
            )
        return _entry(
            ENTRY_ENTERED,
            REASON_OK,
            time_us=first.open_time_us,
            price=first.open,
            label=ENTRY_LABEL_NEXT_CANDLE_OPEN,
            at_open=True,
        )

    # ── D-1: FIRST_TRADE_AT_OR_AFTER_OPEN ────────────────────────────────────
    if entry_spec.price_class == ENTRY_PRICE_CLASS_FIRST_TRADE:
        first_open_us = in_window[0].open_time_us if in_window else None
        prints = sorted(
            (print_ for print_ in trade_prints if print_.time_us <= evaluation_asof_us),
            key=lambda print_: (print_.time_us, print_.price),
        )
        if first_open_us is not None:
            for print_ in prints:
                if print_.time_us < first_open_us or print_.time_us > deadline_us:
                    continue
                at_open = print_.time_us == first_open_us
                return _entry(
                    ENTRY_ENTERED,
                    REASON_OK,
                    time_us=print_.time_us,
                    price=print_.price,
                    label=ENTRY_LABEL_FIRST_TRADE,
                    at_open=at_open,
                )
        if not deadline_reached:
            return _not_entered(
                ENTRY_PENDING,
                REASON_DEADLINE_BEFORE_WINDOW,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
            )
        if not in_window:
            return _not_entered(
                ENTRY_EXPIRED,
                EXPIRY_REASON_DEADLINE,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
            )
        if all(bar.is_locked for bar in in_window):
            return _not_entered(
                ENTRY_MISSED,
                MISSED_REASON_LOCK_LIMIT,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
            )
        return _not_entered(
            ENTRY_MISSED,
            MISSED_REASON_NO_TRADE_PRINT,
            price_class=entry_spec.price_class,
            deadline_us=deadline_us,
            confirmation_time_us=confirmation_us,
            confirmation_close_reference=close_ref,
        )

    # ── D-1: LIMIT_AT_LEVEL ──────────────────────────────────────────────────
    assert entry_spec.price_class == ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL
    if direction not in ("SHORT", "LONG"):
        return _not_entered(
            ENTRY_INVALID,
            "INVALID_DIRECTION",
            price_class=entry_spec.price_class,
            deadline_us=deadline_us,
            confirmation_time_us=confirmation_us,
            confirmation_close_reference=close_ref,
        )
    is_short = direction == "SHORT"
    level = float(entry_spec.limit_level_price)  # validated in EntrySpec
    for bar in in_window:
        if bar.is_locked or bar.volume == 0.0:
            continue  # locked / empty bars cannot produce a fill
        # favorable gap = the market opens beyond the limit level on the
        # fill side (SHORT sell-limit: at/above; LONG buy-limit: at/below)
        favorable_gap = (bar.open >= level) if is_short else (bar.open <= level)
        if favorable_gap:
            if fill_policy.gap_through_policy == GAP_THROUGH_FILL_AT_OPEN:
                return _entry(
                    ENTRY_ENTERED,
                    REASON_FAVORABLE_GAP_THROUGH_FILL,
                    time_us=bar.open_time_us,
                    price=bar.open,
                    label=ENTRY_LABEL_LIMIT_AT_LEVEL,
                    at_open=True,
                )
            return _not_entered(
                ENTRY_MISSED,
                MISSED_REASON_GAP_THROUGH,
                price_class=entry_spec.price_class,
                deadline_us=deadline_us,
                confirmation_time_us=confirmation_us,
                confirmation_close_reference=close_ref,
                detail=REASON_FAVORABLE_GAP_THROUGH_MISSED,
            )
        touches_level = (bar.high >= level) if is_short else (bar.low <= level)
        if touches_level:
            return _entry(
                ENTRY_ENTERED,
                REASON_LEVEL_TOUCH_FILL,
                time_us=bar.open_time_us,
                price=level,
                label=ENTRY_LABEL_LIMIT_AT_LEVEL,
                at_open=False,
                detail=INTRACANDLE_TIME_BOUND_POLICY,
            )
    return _no_fill_resolution()
