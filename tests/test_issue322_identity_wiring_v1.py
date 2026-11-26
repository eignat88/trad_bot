"""Issue #322: offline OOS candle identity wiring contract.

The new candle identity is provided by the adapter
(:class:`ScannerSignalIntegrityObserver`).  The frozen
:class:`ProspectiveOOSObserver` keeps its original ``observe()`` signature — it
must NOT grow candle parameters (that would violate the frozen HTF Point-B
contract).  ``scanner_runner`` passes the candle fields to the adapter, and the
specialized HTF / ME Reverse Long V3 identities remain untouched.
"""

import inspect
from pathlib import Path

import pytest

from app.research.prospective_observer import ProspectiveOOSObserver
from app.research.scanner_signal_integrity_observer import (
    ScannerSignalIntegrityObserver,
)


RUNNER = Path(__file__).resolve().parents[1] / "scanner_runner.py"


def test_adapter_accepts_candle_identity():
    params = inspect.signature(ScannerSignalIntegrityObserver.observe).parameters

    assert "entry_timeframe" in params
    assert "signal_candle_open_time" in params


def test_frozen_observer_signature_is_unchanged():
    """The frozen base must NOT grow candle parameters (byte-identity guard)."""
    params = inspect.signature(ProspectiveOOSObserver.observe).parameters

    assert "entry_timeframe" not in params
    assert "signal_candle_open_time" not in params


def test_adapter_is_a_drop_in_subclass_of_the_frozen_observer():
    assert issubclass(ScannerSignalIntegrityObserver, ProspectiveOOSObserver)


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


def test_runner_wires_the_adapter_not_the_frozen_observer():
    """_get_prospective_observer must construct the signal-integrity adapter."""
    source = RUNNER.read_text(encoding="utf-8")

    assert "_prospective_observer = ScannerSignalIntegrityObserver(" in source
    # The construction site must NOT build the frozen base class directly.
    assert "_prospective_observer = ProspectiveOOSObserver(" not in source


def test_new_identity_does_not_replace_specialized_methods():
    source = (
        Path(__file__).resolve().parents[1]
        / "app/research/prospective_observer.py"
    ).read_text(encoding="utf-8")

    # Specialized HTF and ME Reverse Long V3 identity contracts stay in place.
    assert "def _make_htf_source_key(" in source
    assert "v3_source_key(" in source

    # The adapter delegates non-ME_SHORT families to the frozen base rather
    # than reimplementing / replacing those identities.
    adapter = (
        Path(__file__).resolve().parents[1]
        / "app/research/scanner_signal_integrity_observer.py"
    ).read_text(encoding="utf-8")
    assert "super()._make_source_key(" in adapter
    assert "super().observe(" in adapter
