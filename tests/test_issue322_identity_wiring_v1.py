"""Issue #322: offline OOS candle identity wiring contract."""

import inspect
from pathlib import Path

import pytest

from app.research.prospective_observer import ProspectiveOOSObserver


RUNNER = Path(__file__).resolve().parents[1] / "scanner_runner.py"


def test_observer_accepts_candle_identity():
    params = inspect.signature(ProspectiveOOSObserver.observe).parameters

    assert "entry_timeframe" in params
    assert "signal_candle_open_time" in params


@pytest.mark.parametrize(
    "candidate",
    ["c", "oc", "erc", "rrc"],
)
def test_generic_capture_passes_candle_identity(candidate):
    source = RUNNER.read_text(encoding="utf-8")

    marker = (
        "_observe_prospective_with_fill(client, prospective_obs,"
        "\n"
    )

    blocks = source.split(marker)[1:]

    matching = [
        block.split(")", 1)[0]
        for block in blocks
        if f"signal_time={candidate}.detected_at" in block[:500]
    ]

    assert matching, f"Missing capture for {candidate}"

    for block in matching:
        assert (
            f"entry_timeframe={candidate}.entry_timeframe" in block
        )
        assert (
            f"signal_candle_open_time="
            f"{candidate}.signal_candle_open_time" in block
        )


def test_new_identity_does_not_replace_specialized_methods():
    source = (
        Path(__file__).resolve().parents[1]
        / "app/research/prospective_observer.py"
    ).read_text(encoding="utf-8")

    assert "def _make_htf_source_key(" in source
    assert "v3_source_key(" in source
