"""Issue #322: actual observer SQL parameters, offline only."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.research.prospective_observer import ProspectiveOOSObserver


EXPERIMENT = "BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1"
CANDLE = 1795680000000
START = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)


def test_sql_identity_stable_on_rescan_and_changes_on_new_candle():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = None

    observer = ProspectiveOOSObserver(
        conn,
        {
            EXPERIMENT: {
                "scanner_name": "BREAKOUT_RETEST",
                "direction": "LONG",
                "status": "RUNNING",
            }
        },
    )

    def observe(detected_at, candle):
        return observer.observe(
            scanner_name="BREAKOUT_RETEST",
            direction="LONG",
            symbol="BTCUSDT",
            signal_time=detected_at,
            entry_timeframe="5m",
            signal_candle_open_time=candle,
            reference_price=100.0,
            invalidation_price=99.0,
            target_1=102.0,
            target_2=None,
            score=80.0,
            features={},
            parameters={},
            market_regime="RANGE",
        )

    with (
        patch.object(observer, "_is_db_active", return_value=True),
        patch.object(observer, "_is_direction_active", return_value=True),
    ):
        observe(START, CANDLE)
        observe(START + timedelta(seconds=20), CANDLE)
        observe(START + timedelta(minutes=5), CANDLE + 300000)
        observe(START + timedelta(minutes=6), None)

    inserts = [
        call
        for call in cursor.execute.call_args_list
        if "INSERT INTO research.prospective_observation"
        in call.args[0]
    ]

    assert len(inserts) == 3

    params = [call.args[1] for call in inserts]
    ids = [p[1] for p in params]

    assert ids[0] == ids[1]
    assert ids[0] != ids[2]
    assert all(isinstance(value, int) and value < 0 for value in ids)

    # Detection timestamp remains separate from source identity.
    assert params[0][5] != params[1][5]

    # Existing DB uniqueness remains the deduplication mechanism.
    assert all(
        "ON CONFLICT (experiment_id, source_signal_id) DO NOTHING"
        in call.args[0]
        for call in inserts
    )

    assert conn.commit.call_count == 4
