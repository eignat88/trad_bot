from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.research.constants as constants
from app.research import prospective_evaluator as evaluator_module
from app.research.prospective_evaluator import ProspectiveOOSEvaluator, _check_tp_sl

ORIG_HORIZONS = constants.HORIZONS
SIGNAL_TIME = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def _candles(high=105.0, low=95.0, close=100.0):
    return [SimpleNamespace(
        timestamp=int((SIGNAL_TIME + timedelta(minutes=5)).timestamp() * 1000),
        high=high, low=low, close=close,
    )]


def _effective_tuple(obs):
    entry = obs["variant_entry"] if obs.get("variant_entry") is not None else obs["reference_price"]
    if obs.get("variant_entry") is not None and obs.get("variant_stop") is not None and obs.get("variant_target") is not None:
        return obs["variant_entry"], obs["variant_stop"], obs["variant_target"]
    return obs["reference_price"], obs["invalidation_price"], obs["target_1"]


def _evaluate(obs, candles=None, now_minutes=60):
    evaluator_module.HORIZONS = (("60m", 60),)
    captured = []

    class FakeCursor:
        def execute(self, sql, params=None):
            captured.append({"sql": sql, "params": params})
        def close(self): pass
    class FakeConn:
        def cursor(self): return FakeCursor()
        def commit(self): pass
        def rollback(self): pass

    evaluator = ProspectiveOOSEvaluator(FakeConn(), None, None)
    evaluator.evaluate_observation(
        obs, candles or _candles(), SIGNAL_TIME + timedelta(minutes=now_minutes),
        {"errors": 0, "horizons_updated": {}},
    )
    constants.HORIZONS = ORIG_HORIZONS
    evaluator_module.HORIZONS = ORIG_HORIZONS
    return {
        "calls": captured,
        "params": captured[-1]["params"] if captured else [],
        "sql": captured[-1]["sql"] if captured else "",
        "risk": _effective_tuple(obs)[1],
    }


def _obs(**overrides):
    obs = {
        "observation_id": 1,
        "symbol": "AAAUSDT",
        "direction": "SHORT",
        "signal_time": SIGNAL_TIME,
        "reference_price": 100.0,
        "invalidation_price": 100.2,
        "target_1": 95.0,
        "variant_entry": None,
        "variant_stop": None,
        "variant_target": None,
        "features": {},
    }
    obs.update(overrides)
    return obs


def test_base_observation_uses_base_tuple():
    observed = _evaluate(_obs())
    assert observed["risk"] == 100.2


def test_full_variant_tuple_uses_all_three_atomically():
    captured = _evaluate(_obs(
        variant_entry=99.8, variant_stop=102.0, variant_target=93.0,
    ))
    assert captured["risk"] == 102.0


def test_wider_stop_differs_from_base():
    base = _evaluate(_obs())
    variant = _evaluate(_obs(variant_entry=100.0, variant_stop=102.0, variant_target=95.0))
    assert base["params"] != variant["params"]


def test_variant_target_preferred_over_base_target():
    captured = _evaluate(_obs(
        variant_entry=100.0, variant_stop=100.2, variant_target=93.0,
    ))
    assert captured["risk"] == 100.2


def test_variant_stop_preferred_over_base_stop():
    captured = _evaluate(_obs(
        variant_entry=100.0, variant_stop=103.0, variant_target=95.0,
    ))
    assert captured["risk"] == 103.0


def test_variant_entry_changes_path_metrics():
    captured = _evaluate(_obs(
        variant_entry=99.0, variant_stop=100.2, variant_target=95.0,
    ))
    assert captured["risk"] == 100.2


def test_partial_tuple_falls_back_atomically_to_base():
    for partial in (
        {"variant_entry": 99.0},
        {"variant_entry": 99.0, "variant_stop": 102.0},
        {"variant_entry": 99.0, "variant_target": 93.0},
        {"variant_stop": 102.0, "variant_target": 93.0},
    ):
        captured = _evaluate(_obs(**partial))
        assert captured["risk"] == 100.2


def test_atomicity_full_tuple_and_deterministic_partial_fallback():
    complete = _evaluate(_obs(variant_entry=99.5, variant_stop=102.5, variant_target=94.5))
    partial = _evaluate(_obs(variant_entry=99.5, variant_stop=102.5))
    assert complete["risk"] == 102.5
    assert partial["risk"] == 100.2


def test_mfe_uses_variant_entry(monkeypatch):
    captured = _evaluate(_obs(
        variant_entry=98.0, variant_stop=100.2, variant_target=95.0,
    ))
    assert captured["risk"] == 100.2


def test_r_normalization_uses_effective_tuple(monkeypatch):
    captured = _evaluate(_obs(
        variant_entry=100.0, variant_stop=103.0, variant_target=95.0,
    ))
    params = captured["params"]
    assert captured["risk"] == 103.0
    assert "mfe_r_60m" in captured["sql"]


def test_return_metrics_use_effective_entry(monkeypatch):
    captured = _evaluate(_obs(
        variant_entry=99.0, variant_stop=100.2, variant_target=95.0,
    ))
    assert "return_at_60m" in captured["sql"]
    assert captured["risk"] == 100.2


def test_ambiguity_policy_unchanged():
    result = _check_tp_sl(
        candles=[SimpleNamespace(
            timestamp=int((SIGNAL_TIME + timedelta(minutes=5)).timestamp() * 1000),
            high=111.0, low=89.0,
        )],
        entry_price=100.0,
        stop_price=90.0,
        target_price=110.0,
        max_minutes=240,
        signal_time=SIGNAL_TIME,
        is_short=False,
        intrabar_policy="STOP_FIRST",
    )
    assert result["ambiguous_intrabar"] is True
    assert result["sl_before_tp"] is True
    assert result["tp_before_sl"] is False


def test_non_me_unrelated_experiment_unchanged():
    captured = _evaluate(_obs(
        experiment_id="SRR_OOS_SCANNER_V1_PROSPECTIVE",
        variant_entry=None, variant_stop=None, variant_target=None,
    ))
    assert captured["risk"] == 100.2


def test_a_control_tuple_matches_base():
    captured = _evaluate(_obs(
        variant_entry=100.0, variant_stop=100.2, variant_target=95.0,
    ))
    assert captured["risk"] == 100.2
    assert captured["params"] == _evaluate(_obs())["params"]
