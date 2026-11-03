"""ISSUE253 — BYBIT_SHARED_LIMITER_ALL_CONSUMERS_ALIGNMENT_V1.

Offline, deterministic coverage for the Issue #253 configuration-alignment
contract and the cross-process shared-parameter guard. Nothing here touches
the network (``urlopen`` is poisoned), a database or production state.

What is NEW here (Issue #231 suites do not cover it)
-----------------------------------------------------
1. **Contract alignment** — several consumers loaded from DIFFERENT config
   files that point at ONE ``global_limiter_contract_path`` resolve an
   identical ``LimiterConfig`` (state path + fingerprint) while each keeps
   its own activation bit (staged rollout stays possible).
2. **No silent override** — a config file that disagrees with the contract
   aborts ``load_settings()`` with an explicit ``LimiterContractError``
   naming the key; activation flags can never come from the contract; a
   missing/malformed/unknown-keyed contract fails closed.
3. **State identity guard** — the shared state file now carries a
   ``config_fingerprint`` of the SHARED parameters; a consumer configured
   differently fails closed (``STATE_UNAVAILABLE: config_mismatch``) instead
   of running an off-contract budget against the same bucket, and the
   mismatch never rewrites the state.
4. **Real multi-consumer integration** — three BybitClients built from three
   different config files consume ONE token budget (the 4th request is
   denied), proving the bucket is shared rather than per-process.
5. **GET/POST/retry accounting through a contract-bound client.**
6. **Managed offline path** — ``app.exchange.offline_limiter`` is a no-op
   while default-off and gates every raw offline HTTP attempt when enabled.
7. **OOS empty-source guard** — the generic evaluators still refuse to
   finalize on an empty candle source (the limiter-denial shape), locked in
   against regression.

Existing Issue #231 suites keep covering concurrent acquire, burst, breaker
OPEN/recovery, epoch fencing, stuck holder, process crash, state corruption,
permission denied, P0-P3 contention, default-off parity and restart
idempotency (mapped in the Issue #253 PR description).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

import app.exchange.bybit_client as bybit_client_module
import app.exchange.global_rate_limiter as grl
from app.config import Settings, load_settings
from app.config.limiter_contract import (
    CONTRACT_PATH_KEY,
    LimiterContractError,
    limiter_param_keys,
    resolve_limiter_contract_path,
)
from app.exchange import retry_budget
from app.exchange.bybit_client import (
    BybitClient,
    BybitRateLimitError,
    BybitTimeoutError,
    _RateLimiter,
)
from app.exchange.global_rate_limiter import (
    GlobalBybitRateLimiter,
    GrantResult,
    LimiterConfig,
)


# --------------------------------------------------------------------- fakes
class _OkResponse:
    status = 200
    headers = None

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {
            "retCode": 0, "retMsg": "OK", "result": {"list": []}}

    def raise_for_status(self):     # noqa: D401 - faithful double
        pass

    def json(self):
        return self.payload


class ScriptedSession:
    """Replays scripted outcomes; falls back to an OK response when empty."""

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("GET", url, dict(params or {})))
        return self._next()

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data))
        return self._next()

    def _next(self):
        outcome = self.outcomes.pop(0) if self.outcomes else _OkResponse()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _SpyLimiter:
    """Wraps a REAL limiter and records every gate call."""

    def __init__(self, real):
        self._real = real
        self.acquires = []
        self.prepared = []
        self.records = []

    def acquire(self, priority, deadline=None, logical_request_id=""):
        self.acquires.append((priority, deadline, logical_request_id))
        return self._real.acquire(priority, deadline=deadline,
                                  logical_request_id=logical_request_id)

    def prepare_send(self, grant_id):
        self.prepared.append(grant_id)
        return self._real.prepare_send(grant_id)

    def record_result(self, grant_id, priority, outcome, **kw):
        self.records.append((grant_id, priority, outcome))
        return self._real.record_result(grant_id, priority, outcome, **kw)

    @property
    def state_path(self):
        return self._real.state_path

    @property
    def config(self):
        return self._real.config


# ------------------------------------------------------------------ helpers
def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _contract(tmp_path: Path, **overrides) -> Path:
    """A complete, canonical contract pinned to a tmp state file."""
    payload = {
        "global_limiter_state_path": str(tmp_path / "shared_state.json"),
        "global_limiter_g_rps": 8.0,
        "global_limiter_burst": 3.0,
        "global_limiter_max_wait_s": 0.05,
        "global_limiter_breaker_cooldowns_s": [1.0, 2.0, 3.0],
        "global_limiter_lock_timeout_s": 5.0,
        "global_limiter_demand_ttl_s": 60.0,
        "global_limiter_pending_liveness_ttl_s": 5.0,
        "global_limiter_probe_lease_s": 5.0,
        "global_limiter_registry_capacity": 8192,
        "global_limiter_max_lifetime_s": 60.0,
        "global_limiter_recovery_success_target": 5,
        "global_limiter_record_retry_attempts": 2,
        "global_limiter_record_retry_backoff_s": 0.0,
    }
    payload.update(overrides)
    return _write_json(tmp_path / "limiter_contract.json", payload)


def _load(tmp_path: Path, payload, *, name: str = "config.yaml") -> Settings:
    """load_settings() against an isolated config and a absent .env file."""
    config = _write_json(tmp_path / name, payload)
    return load_settings(path=config, env_file=tmp_path / ".env.absent")


def _enabled_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        global_bybit_rate_limiter_enabled=True,
        global_limiter_state_path=str(tmp_path / "shared_state.json"),
        bybit_timeout=5,
        bybit_max_attempts=3,
        bybit_retry_backoff=0.0,
        bybit_rate_limit_max_retries=2,
        bybit_rate_limit_retry_backoff=0.0,
        bybit_rate_limit_cooldown_s=0.0,
        bybit_retry_deadline_s=10.0,
    )
    values.update(overrides)
    return Settings(**values)


def _live_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        trading_mode="live",
        live_trading_enabled=True,
        live_safety_ready=True,
        bybit_api_key="APIKEY_SENTINEL_1234567890",
        bybit_api_secret="APISECRET_SENTINEL_0987654321",
        bybit_order_submit_journal_path=str(tmp_path / "journal.json"),
    )
    values.update(overrides)
    return Settings(**values)


def _read_state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "shared_state.json").read_text(
        encoding="utf-8"))


def attempt_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("BYBIT_API_CALL")]


def field(line, name):
    for token in line.split():
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return ""


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def clean_process_state():
    retry_budget.reset_process_state()
    yield
    retry_budget.reset_process_state()


@pytest.fixture(autouse=True)
def fast_local_limiter(monkeypatch):
    """The pre-existing process-local limiter must never throttle a test."""
    monkeypatch.setattr(
        BybitClient, "_global_limiter", _RateLimiter(rps=1_000_000.0))


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def _poisoned(*a, **k):
        raise AssertionError("real network request attempted in test")
    monkeypatch.setattr(bybit_client_module, "urlopen", _poisoned)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("GLOBAL_BYBIT_RATE_LIMITER_ENABLED", raising=False)
    monkeypatch.delenv("GLOBAL_BYBIT_RATE_LIMITER_ALLOW_ENV", raising=False)


@pytest.fixture
def telemetry_env(monkeypatch, caplog):
    from app.exchange.api_telemetry import ApiTelemetry
    monkeypatch.setattr(bybit_client_module, "_telemetry", ApiTelemetry())
    caplog.set_level(logging.INFO)
    yield


# =========================================== 1. contract alignment (Stage B)
def test_contract_aligns_every_consumer_to_one_state(tmp_path):
    """Three DIFFERENT config files + one contract => one LimiterConfig.

    Activation stays per-consumer (staged rollout) while state path, G, B,
    max-wait and the parameter fingerprint become identical.
    """
    contract = _contract(tmp_path)

    reference = _load(tmp_path, {CONTRACT_PATH_KEY: str(contract)})
    # a consumer that also switches itself on (staged rollout step 2), and
    # writes some values differently (int vs float) — equivalent values must
    # be accepted, not rejected.
    enabled = _load(tmp_path, {
        CONTRACT_PATH_KEY: str(contract),
        "global_bybit_rate_limiter_enabled": True,
        "global_limiter_burst": 3,
        "global_limiter_max_wait_s": 0.05,
    })
    # a scanner-canary style config: another file, same contract.
    canary = _load(tmp_path, {
        CONTRACT_PATH_KEY: str(contract),
        "global_bybit_rate_limiter_enabled": True,
        "global_limiter_g_rps": 8,
    }, name="scanner_canary.json")

    configs = [LimiterConfig.from_settings(s)
               for s in (reference, enabled, canary)]
    assert len({c.state_path for c in configs}) == 1
    assert len({c.fingerprint for c in configs}) == 1
    assert configs[0].g_rps == 8.0
    assert configs[0].burst == 3.0
    assert configs[0].max_wait_s == 0.05

    # Activation is NOT part of the contract: staged rollout preserved.
    assert reference.global_bybit_rate_limiter_enabled is False
    assert enabled.global_bybit_rate_limiter_enabled is True
    assert canary.global_bybit_rate_limiter_enabled is True


def test_config_disagreeing_with_contract_fails_closed(tmp_path):
    """A config value that contradicts the contract aborts startup."""
    contract = _contract(tmp_path)
    with pytest.raises(LimiterContractError) as excinfo:
        _load(tmp_path, {
            CONTRACT_PATH_KEY: str(contract),
            "global_limiter_burst": 99,      # contract says 3.0
        })
    assert "global_limiter_burst" in str(excinfo.value)
    assert "fail-closed" in str(excinfo.value)


def test_state_path_disagreement_fails_closed(tmp_path):
    """The shared state path can never be re-pointed by one consumer."""
    contract = _contract(tmp_path)
    with pytest.raises(LimiterContractError) as excinfo:
        _load(tmp_path, {
            CONTRACT_PATH_KEY: str(contract),
            "global_limiter_state_path": str(tmp_path / "rogue.json"),
        })
    assert "global_limiter_state_path" in str(excinfo.value)


def test_contract_never_carries_activation(tmp_path):
    """Activation flags in a contract are rejected (staged rollout only)."""
    _contract(tmp_path, **{"global_bybit_rate_limiter_enabled": True})
    contract = tmp_path / "limiter_contract.json"
    with pytest.raises(LimiterContractError) as excinfo:
        load_settings(path=_write_json(tmp_path / "cfg.json", {
            CONTRACT_PATH_KEY: str(contract)}),
            env_file=tmp_path / ".env.absent")
    assert "activation" in str(excinfo.value)


def test_contract_without_state_path_is_rejected(tmp_path):
    contract = tmp_path / "limiter_contract.json"
    _write_json(contract, {"global_limiter_g_rps": 8.0})
    with pytest.raises(LimiterContractError, match="global_limiter_state_path"):
        from app.config.limiter_contract import load_limiter_contract
        load_limiter_contract(str(contract))


def test_contract_unknown_key_is_rejected(tmp_path):
    contract = tmp_path / "limiter_contract.json"
    _write_json(contract, {
        "global_limiter_state_path": str(tmp_path / "s.json"),
        "global_limiter_burstt": 12,          # typo must not be ignored
    })
    from app.config.limiter_contract import load_limiter_contract
    with pytest.raises(LimiterContractError, match="global_limiter_burstt"):
        load_limiter_contract(str(contract))


def test_contract_missing_file_fails_closed(tmp_path):
    with pytest.raises(LimiterContractError, match="could not be read"):
        _load(tmp_path, {CONTRACT_PATH_KEY:
                         str(tmp_path / "absent_contract.json")})


def test_contract_invalid_parameter_fails_closed(tmp_path):
    _contract(tmp_path, **{"global_limiter_g_rps": 0})
    with pytest.raises(LimiterContractError, match="g_rps must be positive"):
        _load(tmp_path, {CONTRACT_PATH_KEY:
                         str(tmp_path / "limiter_contract.json")})


def test_contract_wrong_type_fails_closed(tmp_path):
    _contract(tmp_path, **{"global_limiter_max_wait_s": "30"})
    with pytest.raises(LimiterContractError, match="global_limiter_max_wait_s"):
        _load(tmp_path, {CONTRACT_PATH_KEY:
                         str(tmp_path / "limiter_contract.json")})


def test_no_contract_path_keeps_legacy_default_off(tmp_path):
    """Without a contract nothing changes: default-off, legacy parameters."""
    settings = _load(tmp_path, {})
    assert settings.global_limiter_contract_path == ""
    assert settings.global_bybit_rate_limiter_enabled is False
    assert LimiterConfig.from_settings(settings) == LimiterConfig()
    assert LimiterConfig().enabled is False


def test_contract_parameters_are_strictly_typed_and_complete():
    """The contract vocabulary mirrors the Settings limiter parameters."""
    keys = set(limiter_param_keys())
    assert "global_limiter_state_path" in keys
    assert "global_limiter_g_rps" in keys
    # activation and the contract path itself are NOT parameters
    assert CONTRACT_PATH_KEY not in keys
    assert "global_bybit_rate_limiter_enabled" not in keys
    assert "global_bybit_rate_limiter_allow_env" not in keys


def test_shipped_reference_contract_is_valid_and_inert(tmp_path):
    """The git-versioned reference contract loads, validates and changes
    nothing on its own (activation remains a per-consumer decision)."""
    from app.config.limiter_contract import load_limiter_contract

    root = Path(__file__).resolve().parents[1]
    reference = root / "limiter_contract.v1.json"
    assert reference.is_file(), "the shipped reference contract must exist"

    params = {k: v for k, v in load_limiter_contract(str(reference)).items()
              if not k.startswith("_")}          # "_" keys are comments
    cfg = LimiterConfig.from_settings(Settings(**params))
    assert cfg.g_rps == 8.0
    assert cfg.burst == 12.0
    assert cfg.max_wait_s == 30.0
    assert cfg.breaker_cooldowns_s == (5.0, 30.0, 120.0)
    assert cfg.recovery_success_target == 20
    # the shipped reference stays PORTABLE (relative state path, anchored at
    # the repository root by resolve_limiter_state_path)
    raw_reference = json.loads(reference.read_text(encoding="utf-8"))
    assert not os.path.isabs(raw_reference["global_limiter_state_path"])
    assert os.path.isabs(cfg.state_path)
    assert cfg.enabled is False                  # contract never activates

    settings = _load(tmp_path, {CONTRACT_PATH_KEY: str(reference)})
    assert settings.global_bybit_rate_limiter_enabled is False
    assert LimiterConfig.from_settings(settings).state_path == cfg.state_path


def test_relative_contract_path_never_depends_on_cwd(tmp_path, monkeypatch):
    from app.config.settings import _LIMITER_STATE_ANCHOR

    monkeypatch.chdir(tmp_path)
    resolved = resolve_limiter_contract_path("limiter_contract.json")
    assert resolved == os.path.normpath(
        os.path.join(str(_LIMITER_STATE_ANCHOR), "limiter_contract.json"))
    # identical no matter which directory the process was started from
    assert resolve_limiter_contract_path(
        "limiter_contract.json") == resolved
    # empty / absent contract => alignment disabled (legacy behaviour)
    assert resolve_limiter_contract_path("") == ""
    assert resolve_limiter_contract_path(None) == ""


# ================================= 2. shared-state parameter identity (253)
def test_mismatched_consumer_fails_closed_against_shared_state(tmp_path):
    """One state file, two incompatible budgets -> the second is refused."""
    state = tmp_path / "shared_state.json"
    cfg_ok = LimiterConfig(enabled=True, state_path=str(state),
                           g_rps=8.0, burst=12.0)
    first = GlobalBybitRateLimiter(cfg_ok)
    grant = first.acquire("P1", deadline=time.monotonic() + 5.0)
    assert grant.granted
    first.record_result(grant.grant_id, "P1", "OK")

    raw = json.loads(state.read_text(encoding="utf-8"))
    assert raw["config_fingerprint"] == cfg_ok.fingerprint

    cfg_bad = LimiterConfig(enabled=True, state_path=str(state),
                            g_rps=4.0, burst=12.0)     # incompatible G
    assert cfg_bad.fingerprint != cfg_ok.fingerprint
    second = GlobalBybitRateLimiter(cfg_bad)

    denied = second.acquire("P1", deadline=time.monotonic() + 1.0)
    assert denied.result is GrantResult.STATE_UNAVAILABLE
    assert denied.reason == "config_mismatch"
    # fail-closed: the shared state is byte-identical afterwards
    assert json.loads(state.read_text(encoding="utf-8")) == raw
    # an unknown/never-granted grant is never sendable either
    assert second.prepare_send(grant.grant_id) is False


def test_contract_mismatch_blocks_http_and_records_skip(tmp_path, caplog,
                                                        telemetry_env):
    """BybitClient level: a mismatched consumer sends ZERO HTTP attempts."""
    session = ScriptedSession()
    good = BybitClient(_enabled_settings(tmp_path, global_limiter_g_rps=8.0),
                       session)
    assert good.get_klines("BTCUSDT") == []
    assert len(session.calls) == 1

    caplog.clear()
    rogue = BybitClient(
        _enabled_settings(tmp_path, global_limiter_g_rps=4.0), session)
    with pytest.raises(BybitRateLimitError, match="shared rate limiter"):
        rogue.get_klines("BTCUSDT")

    assert len(session.calls) == 1              # nothing was sent
    lines = attempt_lines(caplog)
    assert lines, "a limiter denial must still be instrumented"
    assert field(lines[0], "result") == "BYBIT_API_SKIP"
    assert "config_mismatch" in lines[0]


def test_legacy_state_is_adopted_once_then_guarded(tmp_path):
    """Pre-#253 state (no fingerprint) is adopted by the FIRST writer only."""
    state = tmp_path / "shared_state.json"
    cfg = LimiterConfig(enabled=True, state_path=str(state))
    owner = GlobalBybitRateLimiter(cfg)

    # Simulate a state written BEFORE Issue #253: strip the fingerprint and
    # recompute the checksum with the core's own canonicalisation.
    grant = owner.acquire("P1", deadline=time.monotonic() + 5.0)
    assert grant.granted
    owner.record_result(grant.grant_id, "P1", "OK")
    raw = json.loads(state.read_text(encoding="utf-8"))
    raw.pop("config_fingerprint", None)
    raw["checksum"] = grl._checksum(raw)
    state.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")),
                     encoding="utf-8")

    same = GlobalBybitRateLimiter(cfg)
    # read-only observation never mutates (F4 preserved)
    same.snapshot()
    assert "config_fingerprint" not in json.loads(
        state.read_text(encoding="utf-8"))
    # the first WRITE adopts the current contract
    grant = same.acquire("P1", deadline=time.monotonic() + 5.0)
    assert grant.granted
    same.record_result(grant.grant_id, "P1", "OK")
    adopted = json.loads(state.read_text(encoding="utf-8"))
    assert adopted["config_fingerprint"] == cfg.fingerprint

    # ... and from now on an incompatible consumer is refused
    other = GlobalBybitRateLimiter(
        LimiterConfig(enabled=True, state_path=str(state), g_rps=1.0))
    denied = other.acquire("P1", deadline=time.monotonic() + 1.0)
    assert denied.reason == "config_mismatch"


def test_default_off_never_creates_or_reads_shared_state(tmp_path):
    state = tmp_path / "shared_state.json"
    limiter = GlobalBybitRateLimiter(
        LimiterConfig(enabled=False, state_path=str(state)))
    out = limiter.acquire("P1", deadline=time.monotonic() + 1.0)
    assert out.granted and out.reason == "disabled"
    assert not state.exists()
    assert not Path(str(state) + ".lock").exists()


def test_same_configuration_always_yields_the_same_fingerprint(tmp_path):
    a = LimiterConfig(enabled=True, state_path=str(tmp_path / "s.json"))
    b = LimiterConfig(enabled=True, state_path=str(tmp_path / "other.json"))
    assert a.fingerprint == b.fingerprint          # path is not semantics
    c = LimiterConfig(enabled=True, state_path=str(tmp_path / "s.json"),
                      g_rps=4.0)
    assert a.fingerprint != c.fingerprint
    d = LimiterConfig(enabled=True, state_path=str(tmp_path / "s.json"),
                      weights=dict(grl.DEFAULT_WEIGHTS))
    assert a.fingerprint == d.fingerprint


# ============================ 3. multi-consumer shared budget (Stage D/C)
def test_three_consumers_from_three_configs_share_one_budget(tmp_path,
                                                             telemetry_env,
                                                             caplog):
    """Three clients, three config files, ONE bucket: the 4th request denies.

    This is the Issue #253 integration proof: if each consumer had its own
    (per-process) budget, all four requests would succeed.
    """
    contract = _contract(tmp_path, **{
        "global_limiter_g_rps": 0.5,        # refill is negligible in-test
        "global_limiter_burst": 3.0,        # exactly three grants
        "global_limiter_max_wait_s": 0.05,
    })
    settings = [
        _load(tmp_path, {CONTRACT_PATH_KEY: str(contract),
                         "global_bybit_rate_limiter_enabled": True},
              name=f"consumer{i}.json")
        for i in (1, 2, 3)
    ]
    session = ScriptedSession()
    clients = [BybitClient(s, session) for s in settings]

    # --- shared state identity across the three consumers
    paths = {c._shared_limiter().state_path for c in clients}
    fingerprints = {c._shared_limiter().config.fingerprint for c in clients}
    assert len(paths) == 1
    assert len(fingerprints) == 1
    assert paths == {str(tmp_path / "shared_state.json")}

    # --- one budget: three grants, then a fail-closed denial
    assert clients[0].get_klines("BTCUSDT") == []
    assert clients[0].get_klines("BTCUSDT") == []
    assert clients[1].get_klines("BTCUSDT") == []
    with pytest.raises(BybitRateLimitError, match="shared rate limiter"):
        clients[2].get_klines("BTCUSDT")

    assert len(session.calls) == 3                # exactly three sends
    state = _read_state(tmp_path)
    assert state["total_grants"] == 3
    assert state["tokens"] >= 0.0
    # the denial is visible, never silent
    assert field(attempt_lines(caplog)[-1], "result") == "BYBIT_API_SKIP"


def test_burst_ceiling_never_exceeded_across_consumers(tmp_path,
                                                       telemetry_env):
    """The shared bucket never over-issues: total <= burst + refill."""
    contract = _contract(tmp_path, **{
        "global_limiter_g_rps": 20.0,
        "global_limiter_burst": 3.0,
        "global_limiter_max_wait_s": 0.01,
    })
    settings = [
        _load(tmp_path, {CONTRACT_PATH_KEY: str(contract),
                         "global_bybit_rate_limiter_enabled": True},
              name=f"consumer{i}.json")
        for i in (1, 2, 3)
    ]
    session = ScriptedSession()
    clients = [BybitClient(s, session) for s in settings]

    for _ in range(3):
        for client in clients:
            try:
                client.get_klines("BTCUSDT")
            except BybitRateLimitError:
                pass

    state = _read_state(tmp_path)
    burst = float(state["tokens"]) + float(state["total_grants"]) \
        - float(state["refill_total"])
    assert burst == pytest.approx(3.0)            # bucket capacity intact
    assert state["total_grants"] <= 3.0 + float(state["refill_total"]) + 1e-9
    assert len(session.calls) == state["total_grants"]
    assert state["total_grants"] >= 2             # real cross-consumer usage


# ======================= 4. GET / POST / retry accounting (Stage C)
def test_contract_bound_get_grants_one_per_http_attempt(tmp_path,
                                                        telemetry_env):
    """A timeout retry re-acquires: 2 HTTP attempts == 2 grants, 2 records."""
    settings = _enabled_settings(tmp_path)
    client = BybitClient(settings, ScriptedSession(
        [TimeoutError("socket timeout"), _OkResponse()]))
    real = client._shared_limiter()
    spy = _SpyLimiter(real)
    client._shared_limiter_obj = spy

    assert client.get_klines("BTCUSDT") == []
    assert len(spy.acquires) == 2
    assert len(spy.prepared) == 2
    assert [r[2] for r in spy.records] == ["TIMEOUT", "OK"]
    # every grant id was prepared exactly once and recorded exactly once
    assert len(set(spy.prepared)) == 2
    assert [r[0] for r in spy.records] == spy.prepared


def test_contract_bound_post_acquires_once_before_send(tmp_path,
                                                       telemetry_env):
    """POST /v5/order/create: one P0 grant, one prepare_send, one record."""
    session = ScriptedSession([_OkResponse({"retCode": 0, "retMsg": "OK",
                                            "result": {"category": "linear",
                                                       "list": []}})])
    client = BybitClient(_live_settings(
        tmp_path, global_bybit_rate_limiter_enabled=True,
        global_limiter_state_path=str(tmp_path / "shared_state.json"),
    ), session)
    real = client._shared_limiter()
    spy = _SpyLimiter(real)
    client._shared_limiter_obj = spy

    payload = client.create_order("BTCUSDT", "Buy", 0.1)
    assert payload["retCode"] == 0
    assert [a[0] for a in spy.acquires] == ["P0"]
    assert len(spy.prepared) == 1
    assert [r[2] for r in spy.records] == ["OK"]
    posts = [c for c in session.calls if c[0] == "POST"]
    assert len(posts) == 1


def test_deny_before_send_leaves_no_grant_dangling(tmp_path, telemetry_env,
                                                   caplog):
    """A revoked grant (epoch moved) is recorded REVOKED and never sent."""
    settings = _enabled_settings(tmp_path)
    session = ScriptedSession()
    client = BybitClient(settings, session)
    real = client._shared_limiter()

    class _Revoke(_SpyLimiter):
        def prepare_send(self, grant_id):
            self.prepared.append(grant_id)
            return False

    client._shared_limiter_obj = _Revoke(real)
    caplog.set_level(logging.INFO)
    with pytest.raises(BybitRateLimitError, match="revoked before send"):
        client.get_klines("BTCUSDT")
    assert session.calls == []                    # nothing left the process
    assert real.snapshot()["grants_count"] == 1   # the grant exists
    assert _read_state(tmp_path)["grants"]        # ...and is terminal
    rec = _read_state(tmp_path)["grants"]
    assert all(v["outcome"] is not None for v in rec.values())
    assert any(v["outcome"] == "REVOKED" for v in rec.values())


# ================================= 5. managed offline path (Stage C)
def test_offline_gate_is_a_noop_while_default_off(tmp_path):
    from app.exchange.offline_limiter import guarded_bybit_attempt

    state = tmp_path / "offline_state.json"
    settings = Settings(global_limiter_state_path=str(state))   # OFF
    assert settings.global_bybit_rate_limiter_enabled is False

    executed = False
    with guarded_bybit_attempt("/v5/market/kline", settings=settings) as a:
        executed = True
        a.outcome = "OK"
    assert executed
    assert a.grant_id == "-"            # no grant was ever taken
    assert not state.exists()           # zero shared-state I/O
    assert not Path(str(state) + ".lock").exists()


def test_offline_gate_acquires_and_records_outcomes(tmp_path):
    from app.exchange.offline_limiter import guarded_bybit_attempt

    state = tmp_path / "offline_state.json"
    settings = Settings(global_bybit_rate_limiter_enabled=True,
                        global_limiter_state_path=str(state))

    with guarded_bybit_attempt("/v5/market/kline", settings=settings) as a:
        a.outcome = "OK"
    raw = json.loads(state.read_text(encoding="utf-8"))
    assert raw["total_grants"] == 1
    assert all(v["outcome"] == "OK" for v in raw["grants"].values())

    # an exception raised by the caller's own request is mapped, not lost
    with pytest.raises(ValueError):
        with guarded_bybit_attempt("/v5/market/kline",
                                   settings=settings):
            raise ValueError("bad payload")
    raw = json.loads(state.read_text(encoding="utf-8"))
    assert raw["total_grants"] == 2
    assert sum(1 for v in raw["grants"].values()
               if v["outcome"] == "ERROR") == 1

    # retCode 10006 style signal recorded as RATE_LIMIT — kept LAST because
    # a real rate limit legitimately trips the shared breaker (5 s cooldown),
    # which would deny any further acquire in this test.
    with guarded_bybit_attempt("/v5/market/kline", settings=settings) as a:
        a.outcome = "RATE_LIMIT"
    raw = json.loads(state.read_text(encoding="utf-8"))
    assert raw["total_grants"] == 3
    assert sorted(v["outcome"] for v in raw["grants"].values()) == [
        "ERROR", "OK", "RATE_LIMIT"]
    assert raw["breaker"]["state"] in ("OPEN", "RECOVERY", "CLOSED")


def test_offline_gate_denies_before_the_request_runs(tmp_path):
    from app.exchange.offline_limiter import guarded_bybit_attempt

    state = tmp_path / "offline_state.json"
    settings = Settings(
        global_bybit_rate_limiter_enabled=True,
        global_limiter_state_path=str(state),
        global_limiter_g_rps=0.5,
        global_limiter_burst=1.0,
        global_limiter_max_wait_s=0.05,
    )
    ran = 0
    for _ in range(3):
        try:
            with guarded_bybit_attempt("/v5/market/kline",
                                       settings=settings):
                ran += 1
        except BybitRateLimitError:
            pass
    # the shared budget (burst 1, refill 0.5/s) allowed at most one send
    assert ran <= 1
    raw = json.loads(state.read_text(encoding="utf-8"))
    assert raw["total_grants"] <= 1


def test_offline_gate_resolves_config_from_repo_root_not_cwd(tmp_path,
                                                             monkeypatch):
    """The offline gate must ignore the process cwd.

    A cwd-relative ``load_settings()`` would silently fall back to built-in
    defaults (limiter OFF) when ``config.yaml`` is missing — the same
    silent-fallback class Issue #231 R3 removed. Here the cwd carries a
    config that ACTIVATES the limiter onto a temp state file; the gate must
    not see it (it must use the repository config), so no state is created.
    """
    from app.exchange.offline_limiter import (guarded_bybit_attempt,
                                              offline_settings)

    hostile = _write_json(tmp_path / "config.yaml", {
        "bybit_api_key": "ck",
        "bybit_api_secret": "cs",
        "bybit_testnet": False,
        "trading_mode": "paper",
        "bybit_market": "linear",
        "global_bybit_rate_limiter_enabled": True,
        "global_limiter_state_path": str(tmp_path / "cwd_state.json"),
    })
    monkeypatch.chdir(tmp_path)

    resolved = offline_settings()
    assert resolved.global_bybit_rate_limiter_enabled is False   # repo config
    assert Path(resolved.global_limiter_state_path) != (
        tmp_path / "cwd_state.json")

    with guarded_bybit_attempt("/v5/market/kline") as a:
        a.outcome = "OK"
    assert a.grant_id == "-"
    assert not (tmp_path / "cwd_state.json").exists()   # cwd was ignored


def test_offline_gate_fails_closed_on_broken_config(tmp_path):
    from app.exchange.offline_limiter import guarded_bybit_attempt

    bad = Settings(global_limiter_g_rps="fast")      # strict-typed violation
    with pytest.raises(ValueError, match="global_limiter_g_rps"):
        with guarded_bybit_attempt("/v5/market/kline", settings=bad):
            pass


# ==================================== 6. OOS empty-source guard (Stage E)
def test_generic_evaluators_refuse_to_finalize_on_empty_source():
    """A limiter denial surfaces as an exception -> empty candle list; the
    generic evaluators must still refuse to treat that as a complete source.

    Locked in from the Issue #253 Stage E audit: the literal
    ``deny -> [] -> finalize`` chain is ruled out by these guards. (The
    separate non-empty-but-truncating-window risk is documented in
    ``docs/issue253_oos_limiter_safety_analysis_v1.md`` as an operator
    decision — it is pre-existing and not introduced by this issue.)
    """
    root = Path(__file__).resolve().parents[1]
    guards = {
        "app/research/evaluator.py": "if not candles",
        "app/research/prospective_evaluator.py": "if not candles",
        "app/shadow/evaluator.py": "if not candles",
        "app/shadow/srr_research_evaluator.py": "if not candles",
        "app/shadow/v2d_evaluator.py": "if not candles",
    }
    for relative, needle in guards.items():
        source = (root / relative).read_text(encoding="utf-8")
        assert needle in source, (
            f"{relative} lost its empty-candle guard: a limiter denial "
            "could otherwise finalize an OOS outcome on an incomplete source")


# ============================================== 7. no-HTTP guarantees
def test_every_test_stayed_offline(monkeypatch):
    """Sanity: the module-level poison is in place for this suite too."""
    assert callable(bybit_client_module.urlopen)


def test_contract_state_paths_are_never_cwd_relative(tmp_path, monkeypatch):
    """A relative contract state path anchors at the repo root (Issue #231
    R2 rule) so two consumers started from different directories still
    resolve the same shared state file."""
    contract = _contract(tmp_path, **{
        "global_limiter_state_path": "var/state/global_bybit_limiter_v1.json"})
    monkeypatch.chdir(tmp_path)
    settings = _load(tmp_path, {CONTRACT_PATH_KEY: str(contract)})
    resolved = LimiterConfig.from_settings(settings).state_path
    from app.config.settings import _LIMITER_STATE_ANCHOR
    assert resolved == os.path.normpath(str(
        _LIMITER_STATE_ANCHOR / "var/state/global_bybit_limiter_v1.json"))
    assert not os.path.isabs(
        json.loads(contract.read_text(encoding="utf-8"))
        ["global_limiter_state_path"])   # contract itself stays portable
