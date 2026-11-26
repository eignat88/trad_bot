"""Issue #298 / #312 — executable delayed-entry model tests (D-1, D-2, D-4).

Offline, pure: synthetic closed-candle data only.  Covers the admissible
entry price classes, the strict-after-confirmation timing rule, the
non-executable confirmation close, ``ENTRY_EXPIRED`` (D-4) and the
``MISSED_ENTRY`` gap-through / lock-limit / no-liquidity handling (D-2),
plus closed-candle-only causality and prefix stability.
"""
from __future__ import annotations

from app.research.me_entry_quality_h2_outcome_v1.entry_model import (
    ENTRY_LABEL_FIRST_TRADE,
    ENTRY_LABEL_LIMIT_AT_LEVEL,
    ENTRY_LABEL_NEXT_CANDLE_OPEN,
    ENTRY_TIMING_POLICY,
    REASON_FAVORABLE_GAP_THROUGH_FILL,
    REASON_FAVORABLE_GAP_THROUGH_MISSED,
    REASON_LEVEL_TOUCH_FILL,
    resolve_entry,
)
from app.research.me_entry_quality_h2_outcome_v1.policy import (
    ENTRY_PRICE_CLASS_FIRST_TRADE,
    ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL,
    ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN,
    GAP_THROUGH_FILL_AT_OPEN,
    GAP_THROUGH_MISSED_ENTRY,
    MISSED_STAT_ZERO_NET,
    EntryDeadline,
    EntrySpec,
    FillPolicy,
)
from app.research.me_entry_quality_h2_outcome_v1.types import (
    CONFIRMATION_CLOSE_REFERENCE_LABEL,
    ENTRY_ENTERED,
    ENTRY_EXPIRED,
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
    closed_candles,
)

BASE_MS = 1_700_000_100_000
BAR_MS = 300_000
CONFIRM_US = (BASE_MS + BAR_MS) * 1_000  # close of candle 0 == open of candle 1
ASOF_US = (BASE_MS + 9 * BAR_MS) * 1_000

CONFIRMATION = ConfirmationInfo(
    state="CONFIRMED_DELAYED",
    confirmation_time_us=CONFIRM_US,
    confirmation_candle_open_ms=BASE_MS,
    confirmation_close_reference=100.2,
)


def bar(index: int, o: float, h: float, low: float, c: float, v: float = 1.0):
    return CandleBar(
        open_time_ms=BASE_MS + index * BAR_MS,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def default_fill_policy(
    gap: str = GAP_THROUGH_FILL_AT_OPEN,
) -> FillPolicy:
    return FillPolicy(
        gap_through_policy=gap,
        missed_entry_stat_policy=MISSED_STAT_ZERO_NET,
    )


def resolve(
    closed,
    *,
    spec: EntrySpec | None = None,
    fill: FillPolicy | None = None,
    deadline: EntryDeadline | None = None,
    asof_us: int = ASOF_US,
    direction: str = "SHORT",
    prints=(),
):
    return resolve_entry(
        confirmation=CONFIRMATION,
        closed=closed_candles(closed, asof_us),
        entry_spec=spec
        or EntrySpec(price_class=ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN),
        fill_policy=fill or default_fill_policy(),
        deadline=deadline or EntryDeadline(max_delay_ms=30 * 60 * 1000),
        evaluation_asof_us=asof_us,
        direction=direction,
        trade_prints=prints,
    )


class TestNextCandleOpen:
    def test_entry_at_first_admissible_open_strictly_after_confirmation(self):
        candles = [
            bar(0, 100.0, 100.5, 99.8, 100.2),
            bar(1, 100.3, 100.6, 100.1, 100.4),
            bar(2, 100.4, 100.5, 99.5, 99.7),
        ]
        result = resolve(candles)
        assert result.status == ENTRY_ENTERED
        assert result.entry_price == 100.3
        assert result.entry_price_label == ENTRY_LABEL_NEXT_CANDLE_OPEN
        # entry instant is the first admissible open (the boundary case of
        # ENTRY_TIMING_POLICY) — never before the confirmation
        assert result.entry_time_us >= CONFIRM_US
        assert result.fill_at_bar_open is True
        assert "STRICTLY_AFTER_CONFIRMING_CANDLE" in ENTRY_TIMING_POLICY

    def test_confirmation_candle_close_is_never_executable(self):
        candles = [
            bar(0, 100.0, 100.5, 99.8, 100.2),
            bar(1, 100.3, 100.6, 100.1, 100.4),
        ]
        result = resolve(candles)
        # the confirmation close is carried ONLY as a labeled reference
        assert result.confirmation_close_reference == 100.2
        assert result.confirmation_close_label == (
            CONFIRMATION_CLOSE_REFERENCE_LABEL
        )
        # and is never the executable entry price
        assert result.entry_price != result.confirmation_close_reference

    def test_confirming_candle_itself_is_never_an_entry_candidate(self):
        # the confirming candle (index 0) opens before the confirmation and
        # must never be used, even if it is the only bar
        candles = [bar(0, 100.0, 100.5, 99.8, 100.2)]
        result = resolve(candles, deadline=EntryDeadline(max_delay_ms=1_800_000))
        assert result.status in (ENTRY_PENDING, ENTRY_EXPIRED)
        assert result.entry_price is None

    def test_non_entered_leg_carries_undetermined_price(self):
        candles = [bar(3, 99.7, 99.9, 98.2, 98.5)]
        result = resolve(candles, deadline=EntryDeadline(max_delay_ms=120_000))
        assert result.entry_price is None
        assert result.entry_price_label == ENTRY_PRICE_UNDETERMINED

    def test_first_bar_locked_is_missed_entry(self):
        candles = [
            bar(1, 100.3, 100.3, 100.3, 100.3),  # locked: high == low
            bar(2, 100.3, 100.5, 100.0, 100.1),
        ]
        result = resolve(candles)
        assert result.status == ENTRY_MISSED
        assert result.reason == MISSED_REASON_LOCK_LIMIT

    def test_first_bar_without_volume_is_missed_entry(self):
        candles = [bar(1, 100.3, 100.6, 100.1, 100.4, v=0.0)]
        result = resolve(candles)
        assert result.status == ENTRY_MISSED
        assert result.reason == MISSED_REASON_NO_LIQUIDITY


class TestExpiryAndPending:
    def test_entry_expired_when_deadline_passes_without_admissible_bar(self):
        # no candidate bar opens before the deadline
        candles = [bar(3, 99.7, 99.9, 98.2, 98.5)]
        result = resolve(candles, deadline=EntryDeadline(max_delay_ms=120_000))
        assert result.status == ENTRY_EXPIRED
        assert result.reason == EXPIRY_REASON_DEADLINE
        assert result.deadline_us == CONFIRM_US + 120_000 * 1_000

    def test_entry_pending_while_deadline_not_reached(self):
        # ASOF is before the deadline: nothing is guessed
        asof = CONFIRM_US + 60 * 1_000_000  # +60s
        candles = [bar(1, 100.3, 100.6, 100.1, 100.4)]
        result = resolve(
            candles,
            deadline=EntryDeadline(max_delay_ms=120_000),
            asof_us=asof,
        )
        assert result.status == ENTRY_PENDING
        assert result.entry_price is None

    def test_all_locked_bars_at_deadline_are_missed_entry_lock_limit(self):
        candles = [
            bar(1, 100.3, 100.3, 100.3, 100.3),
            bar(2, 100.2, 100.2, 100.2, 100.2),
        ]
        result = resolve(
            candles,
            spec=EntrySpec(
                price_class=ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL,
                limit_level_price=100.9,
            ),
            deadline=EntryDeadline(max_delay_ms=60_000),
        )
        assert result.status == ENTRY_MISSED
        assert result.reason == MISSED_REASON_LOCK_LIMIT

    def test_all_empty_bars_at_deadline_are_missed_entry_no_liquidity(self):
        candles = [
            bar(1, 100.3, 100.6, 100.1, 100.4, v=0.0),
            bar(2, 100.4, 100.5, 100.0, 100.1, v=0.0),
        ]
        result = resolve(
            candles,
            spec=EntrySpec(
                price_class=ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL,
                limit_level_price=100.9,
            ),
            deadline=EntryDeadline(max_delay_ms=60_000),
        )
        assert result.status == ENTRY_MISSED
        assert result.reason == MISSED_REASON_NO_LIQUIDITY

    def test_tradable_bars_without_fill_expire_normally(self):
        # LIMIT_AT_LEVEL whose level is never touched by tradable bars is a
        # normal D-4 expiry — not a missed entry
        candles = [
            bar(1, 100.3, 100.6, 100.1, 100.4),
            bar(2, 100.4, 100.5, 100.0, 100.1),
        ]
        result = resolve(
            candles,
            spec=EntrySpec(
                price_class=ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL,
                limit_level_price=100.9,
            ),
            deadline=EntryDeadline(max_delay_ms=60_000),
        )
        assert result.status == ENTRY_EXPIRED
        assert result.reason == EXPIRY_REASON_DEADLINE


class TestLimitAtLevel:
    def _spec(self, level: float) -> EntrySpec:
        return EntrySpec(
            price_class=ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL,
            limit_level_price=level,
        )

    def test_level_touch_fills_at_the_level(self):
        candles = [bar(1, 100.3, 100.6, 100.1, 100.4)]
        result = resolve(candles, spec=self._spec(100.35))
        assert result.status == ENTRY_ENTERED
        assert result.entry_price == 100.35
        assert result.reason == REASON_LEVEL_TOUCH_FILL
        assert result.fill_at_bar_open is False

    def test_favorable_gap_through_fills_at_open(self):
        candles = [bar(1, 100.6, 100.9, 100.5, 100.7)]
        result = resolve(candles, spec=self._spec(100.35))
        assert result.status == ENTRY_ENTERED
        assert result.entry_price == 100.6
        assert result.reason == REASON_FAVORABLE_GAP_THROUGH_FILL
        assert result.fill_at_bar_open is True

    def test_favorable_gap_through_policy_can_declare_missed_entry(self):
        candles = [bar(1, 100.6, 100.9, 100.5, 100.7)]
        result = resolve(
            candles,
            spec=self._spec(100.35),
            fill=default_fill_policy(gap=GAP_THROUGH_MISSED_ENTRY),
        )
        assert result.status == ENTRY_MISSED
        assert result.reason == MISSED_REASON_GAP_THROUGH
        assert result.detail == REASON_FAVORABLE_GAP_THROUGH_MISSED

    def test_never_touched_level_expires_at_deadline(self):
        candles = [bar(1, 100.1, 100.2, 100.0, 100.1)]
        result = resolve(
            candles, spec=self._spec(100.35), deadline=EntryDeadline(
                max_delay_ms=60_000
            )
        )
        assert result.status == ENTRY_EXPIRED

    def test_long_direction_buy_limit_fills_on_low_touch(self):
        candles = [bar(1, 100.3, 100.6, 100.1, 100.4)]
        result = resolve(
            candles, spec=self._spec(100.15), direction="LONG"
        )
        assert result.status == ENTRY_ENTERED
        assert result.entry_price == 100.15


class TestFirstTradeAtOrAfterOpen:
    def _spec(self) -> EntrySpec:
        return EntrySpec(
            price_class=ENTRY_PRICE_CLASS_FIRST_TRADE,
            trade_print_source="TEST_TAPE_SNAPSHOT",
        )

    def test_first_print_at_or_after_open_fills(self):
        candles = [bar(1, 100.3, 100.6, 100.1, 100.4)]
        prints = [
            TradePrint(time_us=CONFIRM_US + 1_000, price=100.25),
            TradePrint(time_us=CONFIRM_US + 5_000, price=100.31),
        ]
        result = resolve(candles, spec=self._spec(), prints=prints)
        assert result.status == ENTRY_ENTERED
        assert result.entry_price == 100.25
        assert result.entry_price_label == ENTRY_LABEL_FIRST_TRADE

    def test_prints_after_deadline_are_ignored(self):
        candles = [bar(1, 100.3, 100.6, 100.1, 100.4)]
        prints = [TradePrint(time_us=CONFIRM_US + 10 * 60 * 1_000_000, price=99.0)]
        result = resolve(
            candles,
            spec=self._spec(),
            prints=prints,
            deadline=EntryDeadline(max_delay_ms=60_000),
        )
        assert result.status in (ENTRY_EXPIRED, ENTRY_MISSED)

    def test_future_prints_beyond_asof_are_not_trusted(self):
        asof = CONFIRM_US + 120 * 1_000_000
        candles = [bar(1, 100.3, 100.6, 100.1, 100.4)]
        prints = [TradePrint(time_us=asof + 60 * 1_000_000, price=100.2)]
        result = resolve(
            candles,
            spec=self._spec(),
            prints=prints,
            asof_us=asof,
            deadline=EntryDeadline(max_delay_ms=60_000),
        )
        assert result.status in (ENTRY_EXPIRED, ENTRY_MISSED)
        assert result.entry_price is None

    def test_no_prints_at_deadline_is_missed_entry(self):
        candles = [bar(1, 100.3, 100.6, 100.1, 100.4)]
        result = resolve(
            candles, spec=self._spec(), deadline=EntryDeadline(max_delay_ms=60_000)
        )
        assert result.status == ENTRY_MISSED
        assert result.reason == MISSED_REASON_NO_TRADE_PRINT


class TestCausality:
    def test_only_closed_candles_participate(self):
        # candle 1 is still forming at the ASOF (its close is later): it
        # must not produce an entry
        asof = CONFIRM_US + 150 * 1_000_000  # mid-candle 1
        candles = [bar(1, 100.3, 100.6, 100.1, 100.4)]
        result = resolve(candles, asof_us=asof)
        assert result.status == ENTRY_PENDING
        assert result.entry_price is None

    def test_prefix_stability_appending_future_candles_never_changes_result(self):
        early = [
            bar(0, 100.0, 100.5, 99.8, 100.2),
            bar(1, 100.3, 100.6, 100.1, 100.4),
            bar(2, 100.4, 100.5, 99.5, 99.7),
        ]
        asof_early = (BASE_MS + 3 * BAR_MS) * 1_000
        before = resolve(early, asof_us=asof_early).to_dict()
        later = early + [
            bar(3, 99.7, 101.0, 97.0, 98.5),
            bar(4, 98.5, 102.0, 96.0, 99.0),
        ]
        after = resolve(later, asof_us=asof_early).to_dict()
        assert before == after

    def test_deterministic_repeatability(self):
        candles = [
            bar(0, 100.0, 100.5, 99.8, 100.2),
            bar(1, 100.3, 100.6, 100.1, 100.4),
        ]
        first = resolve(candles).to_dict()
        second = resolve(candles).to_dict()
        assert first == second

    def test_duplicate_candle_rows_are_deduplicated_by_open_time(self):
        candles = [
            bar(1, 100.3, 100.6, 100.1, 100.4),
            bar(1, 100.3, 100.6, 100.1, 100.4),
        ]
        result = resolve(candles)
        assert result.status == ENTRY_ENTERED
        assert result.entry_price == 100.3
