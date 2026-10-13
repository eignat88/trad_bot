from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models import Candle
from app.research.me_reverse_long_v3.entry_policy import next_executable_entry_at
from app.research.me_reverse_long_v3.evaluator import MEReverseLongV3ExecutionEvaluator
from app.research.me_reverse_long_v3.historical_source import make_v3_historical_source

UTC = timezone.utc
DETECTED = datetime(2026, 10, 12, 17, 0, 1, tzinfo=UTC)
ENTRY = next_executable_entry_at(DETECTED)
DEADLINE = ENTRY + timedelta(minutes=240)


def test_historical_provider_to_finalized_outcome():
    client = MagicMock()

    rows = []
    for i in reversed(range(48)):
        opened = ENTRY + timedelta(minutes=5 * i)
        rows.append([
            str(int(opened.timestamp() * 1000)),
            "100", "101", "99", "100", "1",
        ])

    client._public_get.return_value = {
        "result": {"list": rows}
    }

    source = make_v3_historical_source(client)
    conn = MagicMock()
    evaluator = MEReverseLongV3ExecutionEvaluator(conn, source)

    evaluator._eligible = MagicMock(return_value=[(
        101,
        "BTCUSDT",
        DETECTED,
        None,
        None,
        None,
        {
            "_v3_filter_group": "PASS",
            "_v3_execution_entry_at": ENTRY.isoformat(),
        },
        DETECTED - timedelta(minutes=1),
    )])

    evaluator.repository = MagicMock()
    evaluator.repository.upsert.return_value = "INSERTED_OR_UPDATED"

    stats = evaluator.run_evaluation_cycle(DEADLINE)

    assert stats["finalized"] == 1, stats
    record = evaluator.repository.upsert.call_args.args[0]

    assert record.status == "FINALIZED"
    assert record.execution_entry_at == ENTRY
    assert record.execution_entry_price == 100
    assert record.execution_stop_price == 97.5
    assert record.execution_target_price == 103
    assert record.candle_count == 48
    assert record.exit_reason == "TIMEOUT"

    assert client._public_get.called
    _, kwargs = client._public_get.call_args
    assert kwargs["interval"] == "5"
    assert kwargs["end"] == int(
        (DEADLINE - timedelta(minutes=5)).timestamp() * 1000
    )
