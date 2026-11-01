"""ISSUE231 R2 — deterministic two-factor configuration contract for
GLOBAL_BYBIT_RATE_LIMITER_V1 (regression suite).

Contract under test (maps 1:1 to the R2 requirements):

1. STRICT BOOLEAN (R1 preserved) — ``global_bybit_rate_limiter_enabled``
   and ``global_bybit_rate_limiter_allow_env`` accept only literal
   JSON ``true``/``false``; strings, numbers, null and containers raise an
   explicit ``ValueError`` at all interpretation sites.
2. DEFAULT-OFF — without any limiter key the flag stays ``False``
   (``Settings`` default and ``load_settings()``).
3. TWO-FACTOR ENV ACTIVATION — the ENV variable
   ``GLOBAL_BYBIT_RATE_LIMITER_ENABLED`` may activate the limiter ONLY
   when the config file also sets ``global_bybit_rate_limiter_allow_env:
   true``. Without that permission the ENV variable is ignored for
   activation (warning logged, startup unaffected) — it can never switch
   the limiter on by itself.
4. PRIORITY ORDER (documented, tested) — permitted ENV > config.yaml >
   default. Conflicts resolve predictably: a permitted ENV value always
   beats the YAML flag; config-file TYPE errors stay fatal even when the
   ENV value would win; without permission YAML governs.
5. VALIDATED PARAMETERS — every ``global_limiter_*`` key consumed by
   ``LimiterConfig.from_settings`` (state path, G, B, TTLs, breaker,
   registry, retries, probe lease, lock timeout) rejects wrong types
   (strings, booleans, null, containers, NaN/Infinity) and out-of-range
   values with an explicit ``ValueError`` inside ``load_settings()``.
6. STATE PATH SAFETY — an enabled limiter in live (production) trading
   mode requires an explicitly ABSOLUTE ``global_limiter_state_path``;
   relative dev paths anchor to the repository root, never to the process
   WorkingDirectory; the shared directory/file must be writable by the
   current service user (multi-user contract).
7. NO UNEXPECTED HTTP — every invalid configuration fails during
   settings load or limiter build, BEFORE any BybitClient request path:
   the transport (``urlopen``) is poisoned in this module and the injected
   sessions assert loudly if touched.

No token-bucket, breaker, fairness, retry or accounting logic is changed
by the configuration contract; those remain covered by the Phase 3/4
suites (``test_global_bybit_rate_limiter_*``) and the R1 suite
(``test_issue231_strict_limiter_flag_v1``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.exchange.bybit_client as bybit_client_module
from app.config import settings as settings_module
from app.config.settings import Settings, load_settings
from app.exchange.bybit_client import BybitClient
from app.exchange.global_rate_limiter import (
    GlobalBybitRateLimiter,
    LimiterConfig,
)

FLAG = "global_bybit_rate_limiter_enabled"
ALLOW_ENV = "global_bybit_rate_limiter_allow_env"
ENV_FLAG = "GLOBAL_BYBIT_RATE_LIMITER_ENABLED"
REPO_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ fakes
class RecordingSession:
    """Any transport call is an immediate test failure."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, *args, **kwargs):
        self.calls.append("GET")
        raise AssertionError("unexpected HTTP GET from the config contract")

    def post(self, *args, **kwargs):
        self.calls.append("POST")
        raise AssertionError("unexpected HTTP POST from the config contract")


@pytest.fixture(autouse=True)
def isolated_limiter_env(monkeypatch):
    """The activation ENV must never leak in from the shell or .env."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.delenv("GLOBAL_BYBIT_RATE_LIMITER_ALLOW_ENV", raising=False)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Poison the transport: an unexpected real request fails immediately."""
    def _poisoned(*args, **kwargs):
        raise AssertionError("unexpected HTTP attempted (urlopen poisoned)")
    monkeypatch.setattr(bybit_client_module, "urlopen", _poisoned)


def _load(tmp_path, payload):
    """load_settings() against an isolated JSON config and no .env file."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return load_settings(config_path, env_file=tmp_path / ".env.absent")


# ============================================================ 1. default-off
def test_default_off_without_any_limiter_key(tmp_path):
    settings = _load(tmp_path, {"trading_mode": "paper"})
    assert settings.global_bybit_rate_limiter_enabled is False
    assert settings.global_bybit_rate_limiter_allow_env is False
    # default-off is also the dataclass default (requirement 2)
    assert Settings().global_bybit_rate_limiter_enabled is False
    assert Settings().global_bybit_rate_limiter_allow_env is False


def test_settings_defaults_mirror_core_defaults():
    """No drift between Settings defaults and LimiterConfig defaults."""
    assert LimiterConfig.from_settings(Settings()) == LimiterConfig()
    assert LimiterConfig().enabled is False          # default-off contract


def test_default_off_client_zero_io_zero_http(tmp_path):
    settings = _load(tmp_path, {})
    session = RecordingSession()
    client = BybitClient(settings, session=session)
    assert client._limiter_enabled() is False
    assert client._shared_limiter() is None          # zero I/O, no build
    assert client._shared_limiter_obj is None
    assert session.calls == []


# ==================================================== 2. two-factor ENV gate
def test_env_activation_allowed_when_config_grants_permission(tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "true")
    settings = _load(tmp_path, {ALLOW_ENV: True})
    assert settings.global_bybit_rate_limiter_allow_env is True
    assert settings.global_bybit_rate_limiter_enabled is True


@pytest.mark.parametrize("config", [
    {},                                   # no permission, no YAML key
    {FLAG: True},                         # YAML on, but ENV unauthorized
    {ALLOW_ENV: False},                   # explicit refusal
    {FLAG: True, ALLOW_ENV: False},       # permission denied wins the gate
], ids=["empty", "yaml_on", "allow_false", "yaml_on_allow_false"])
def test_env_without_permission_never_activates(tmp_path, monkeypatch, config):
    monkeypatch.setenv(ENV_FLAG, "true")
    settings = _load(tmp_path, config)
    # the limiter follows config.yaml/default — the ENV value is ignored
    assert settings.global_bybit_rate_limiter_enabled is config.get(FLAG, False)


def test_permission_is_config_file_only(tmp_path, monkeypatch):
    """A same-named ENV var must never grant the permission itself."""
    monkeypatch.setenv("GLOBAL_BYBIT_RATE_LIMITER_ALLOW_ENV", "true")
    monkeypatch.setenv(ENV_FLAG, "true")
    settings = _load(tmp_path, {})
    assert settings.global_bybit_rate_limiter_allow_env is False
    assert settings.global_bybit_rate_limiter_enabled is False


def test_ignored_env_junk_is_not_fatal_without_permission(tmp_path,
                                                          monkeypatch,
                                                          caplog):
    """Without permission the ENV var is not a config source at all:
    junk values neither activate the limiter nor break startup, and the
    ignored intent is surfaced as a warning (documented behavior)."""
    monkeypatch.setenv(ENV_FLAG, "definitely-not-a-boolean")
    settings = _load(tmp_path, {})
    assert settings.global_bybit_rate_limiter_enabled is False
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= 30]                 # logging.WARNING
    assert any(ENV_FLAG in w for w in warnings), warnings


@pytest.mark.parametrize("junk", ["definitely-not-a-boolean", "", "2", "ture"],
                         ids=["word", "empty", "number", "typo"])
def test_env_junk_with_permission_fails_closed(tmp_path, monkeypatch, junk):
    monkeypatch.setenv(ENV_FLAG, junk)
    with pytest.raises(ValueError, match=ENV_FLAG):
        _load(tmp_path, {ALLOW_ENV: True})


@pytest.mark.parametrize("value", ["true", "1", 1, 0, None, [], {}],
                         ids=["str_true", "str_1", "int_1", "int_0",
                              "none", "list", "dict"])
def test_allow_env_requires_literal_bool(tmp_path, value):
    with pytest.raises(ValueError, match=ALLOW_ENV):
        _load(tmp_path, {ALLOW_ENV: value})


# ============================================ 3. precedence ENV > YAML > default
@pytest.mark.parametrize("yaml_value, env_value, expected", [
    (True,  "false", False),   # permitted ENV beats YAML-on  (conflict)
    (False, "true",  True),    # permitted ENV beats YAML-off (conflict)
    (True,  "true",  True),
    (False, "false", False),
], ids=["env_off_beats_yaml_on", "env_on_beats_yaml_off",
        "both_on", "both_off"])
def test_permitted_env_takes_precedence_over_yaml(tmp_path, monkeypatch,
                                                  yaml_value, env_value,
                                                  expected):
    monkeypatch.setenv(ENV_FLAG, env_value)
    settings = _load(tmp_path, {FLAG: yaml_value, ALLOW_ENV: True})
    assert settings.global_bybit_rate_limiter_enabled is expected


@pytest.mark.parametrize("yaml_value", [True, False], ids=["on", "off"])
def test_yaml_governs_when_env_absent(tmp_path, yaml_value):
    settings = _load(tmp_path, {FLAG: yaml_value, ALLOW_ENV: True})
    assert settings.global_bybit_rate_limiter_enabled is yaml_value


def test_yaml_type_error_fatal_even_when_env_wins(tmp_path, monkeypatch):
    """Config-file TYPE errors always surface — precedence only decides
    between VALID values."""
    monkeypatch.setenv(ENV_FLAG, "true")
    with pytest.raises(ValueError, match=FLAG):
        _load(tmp_path, {FLAG: "true", ALLOW_ENV: True})


# ================================================ 4. R1 strictness preserved
INVALID_YAML_VALUES = [
    "true", "false", "True", "1", "0", "yes", "off",   # strings
    1, 0, 1.0, 0.0,                                     # numbers
    None,                                               # null
    [], {},                                             # containers
]
INVALID_YAML_IDS = [f"{type(v).__name__}_{v}" for v in INVALID_YAML_VALUES]


@pytest.mark.parametrize("value", INVALID_YAML_VALUES, ids=INVALID_YAML_IDS)
def test_yaml_flag_still_strict(tmp_path, value):
    """Issue 231 R1 contract is preserved for the YAML/Python-bool path."""
    with pytest.raises(ValueError, match=FLAG):
        _load(tmp_path, {FLAG: value})


def test_strict_parser_still_rejects_non_bools():
    from app.config.settings import parse_strict_bool_flag
    with pytest.raises(ValueError, match=FLAG):
        parse_strict_bool_flag("true", FLAG)
    with pytest.raises(ValueError, match=FLAG):
        parse_strict_bool_flag(1, FLAG)


# ===================================== 5. wrong types for global_limiter_* keys
WRONG_TYPE_CASES = [
    ("global_limiter_g_rps", "8"),
    ("global_limiter_g_rps", True),
    ("global_limiter_burst", "12"),
    ("global_limiter_burst", None),
    ("global_limiter_state_path", 123),
    ("global_limiter_state_path", None),
    ("global_limiter_demand_ttl_s", "120"),
    ("global_limiter_max_wait_s", True),
    ("global_limiter_breaker_cooldowns_s", "5,30,120"),
    ("global_limiter_breaker_cooldowns_s", 5),
    ("global_limiter_recovery_success_target", "20"),
    ("global_limiter_recovery_success_target", 20.5),
    ("global_limiter_registry_capacity", 3.5),
    ("global_limiter_record_retry_attempts", True),
    ("global_limiter_record_retry_backoff_s", "0.05"),
    ("global_limiter_lock_timeout_s", "5"),
    ("global_limiter_probe_lease_s", False),
    ("global_limiter_pending_liveness_ttl_s", []),
    ("global_limiter_max_lifetime_s", {}),
]


@pytest.mark.parametrize("key, value", WRONG_TYPE_CASES)
def test_wrong_types_rejected_at_load(tmp_path, key, value):
    with pytest.raises(ValueError, match=key):
        _load(tmp_path, {key: value})


def test_from_settings_rejects_wrong_type_directly():
    """Defense in depth: the build-time entry point is strict as well."""
    with pytest.raises(ValueError, match="global_limiter_g_rps"):
        LimiterConfig.from_settings(
            Settings(global_limiter_g_rps="fast"))


# ==================================================== 6. ranges and limits
BAD_RANGE_CASES = [
    ("global_limiter_g_rps", 0.0),
    ("global_limiter_g_rps", -1.0),
    ("global_limiter_g_rps", float("nan")),
    ("global_limiter_g_rps", float("inf")),
    ("global_limiter_burst", 0.0),
    ("global_limiter_burst", 0.5),
    ("global_limiter_burst", float("nan")),
    ("global_limiter_demand_ttl_s", 0.0),
    ("global_limiter_demand_ttl_s", -5.0),
    ("global_limiter_pending_liveness_ttl_s", 0.0),
    ("global_limiter_max_lifetime_s", 0.0),
    ("global_limiter_max_wait_s", 0.0),
    ("global_limiter_lock_timeout_s", -1.0),
    ("global_limiter_probe_lease_s", 0.0),
    ("global_limiter_record_retry_attempts", 0),
    ("global_limiter_record_retry_backoff_s", -0.01),
    ("global_limiter_recovery_success_target", 0),
]


@pytest.mark.parametrize("key, value", BAD_RANGE_CASES)
def test_out_of_range_rejected_at_load(tmp_path, key, value):
    with pytest.raises(ValueError):
        _load(tmp_path, {key: value})


def test_empty_breaker_cooldowns_rejected(tmp_path):
    with pytest.raises(ValueError, match="global_limiter_breaker_cooldowns_s"):
        _load(tmp_path, {"global_limiter_breaker_cooldowns_s": []})


def test_negative_breaker_cooldown_rejected(tmp_path):
    with pytest.raises(ValueError):
        _load(tmp_path,
              {"global_limiter_breaker_cooldowns_s": [5.0, -1.0, 120.0]})


def test_registry_capacity_must_cover_g_times_lifetime(tmp_path):
    """G=100 req/s needs ceil(2*100*60)=12000 registry slots > 8192."""
    with pytest.raises(ValueError, match="registry_capacity"):
        _load(tmp_path, {"global_limiter_g_rps": 100.0})


@pytest.mark.parametrize("bad_ttl", [0.0, -5.0], ids=["zero", "negative"])
def test_direct_limiter_config_rejects_non_positive_demand_ttl(bad_ttl):
    """Direct LimiterConfig construction path (defense in depth).

    load_settings() routes every range check through
    LimiterConfig.__post_init__(); this covers the other entry point — a
    hand-built LimiterConfig — which must reject the same non-positive
    pending-demand TTL instead of letting _refresh() prune every
    registered demand on its next pass.
    """
    with pytest.raises(ValueError, match="demand_ttl_s"):
        LimiterConfig(demand_ttl_s=bad_ttl)


def test_valid_custom_parameters_roundtrip(tmp_path):
    """A fully specified, in-range configuration loads and maps 1:1 onto
    the LimiterConfig fields consumed by the core."""
    payload = {
        FLAG: True,
        "global_limiter_state_path": str(tmp_path / "shared_state.json"),
        "global_limiter_g_rps": 4,
        "global_limiter_burst": 6.5,
        "global_limiter_demand_ttl_s": 90,
        "global_limiter_breaker_cooldowns_s": [1, 2, 3],
        "global_limiter_recovery_success_target": 7,
        "global_limiter_max_wait_s": 12.5,
    }
    settings = _load(tmp_path, payload)
    cfg = LimiterConfig.from_settings(settings)
    assert cfg.enabled is True
    assert cfg.g_rps == 4.0
    assert cfg.burst == 6.5
    assert cfg.demand_ttl_s == 90.0
    assert cfg.breaker_cooldowns_s == (1.0, 2.0, 3.0)
    assert cfg.recovery_success_target == 7
    assert cfg.max_wait_s == 12.5
    assert Path(cfg.state_path) == (tmp_path / "shared_state.json").resolve()


# ============================================================ 7. state path
def test_relative_state_path_anchors_to_repo_root_not_cwd(tmp_path,
                                                          monkeypatch):
    settings = _load(tmp_path,
                     {"global_limiter_state_path": "var/state/r2_probe.json"})
    before = LimiterConfig.from_settings(settings).state_path
    monkeypatch.chdir(tmp_path)          # random WorkingDirectory
    after = LimiterConfig.from_settings(settings).state_path
    assert before == after                          # cwd-independent
    assert Path(after).is_absolute()
    assert Path(after) == REPO_ROOT / "var" / "state" / "r2_probe.json"


def test_production_requires_absolute_state_path(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        _load(tmp_path, {FLAG: True, "trading_mode": "live"})


def test_production_with_absolute_state_path_ok(tmp_path):
    absolute_state = str((tmp_path / "prod_state.json").resolve())
    settings = _load(tmp_path, {
        FLAG: True,
        "trading_mode": "live",
        "global_limiter_state_path": absolute_state,
    })
    assert settings.global_bybit_rate_limiter_enabled is True
    cfg = LimiterConfig.from_settings(settings)
    assert Path(cfg.state_path).is_absolute()
    assert cfg.state_path == absolute_state


def test_from_settings_live_relative_rejected_directly():
    """Defense in depth for direct Settings(...) construction."""
    with pytest.raises(ValueError, match="absolute"):
        LimiterConfig.from_settings(
            Settings(trading_mode="live",
                     **{FLAG: True}))


def test_empty_state_path_rejected(tmp_path):
    with pytest.raises(ValueError, match="global_limiter_state_path"):
        _load(tmp_path, {"global_limiter_state_path": ""})


def test_unwritable_shared_state_dir_rejected_when_enabled(tmp_path,
                                                           monkeypatch):
    """Multi-user access contract: an existing shared directory that this
    service user cannot write fails at load time, not at runtime."""
    def _deny(*args, **kwargs):
        return False
    monkeypatch.setattr(settings_module.os, "access", _deny)
    absolute_state = str((tmp_path / "shared_state.json").resolve())
    with pytest.raises(ValueError, match="writable"):
        _load(tmp_path, {FLAG: True,
                         "global_limiter_state_path": absolute_state})


def test_disabled_limiter_never_probes_path_access(tmp_path, monkeypatch):
    """Default-off keeps the zero-I/O contract: no access probe at all,
    even for a live (production) config with a relative default path."""
    def _deny(*args, **kwargs):
        return False
    monkeypatch.setattr(settings_module.os, "access", _deny)
    settings = _load(tmp_path, {"trading_mode": "live"})
    assert settings.global_bybit_rate_limiter_enabled is False


def test_enabled_missing_parent_dir_defers_creation(tmp_path):
    """A not-yet-existing shared directory is accepted: it is created on
    first acquire (operators pre-create it group-writable for multi-user
    deployments — documented in docs/issue231_r2_...)."""
    settings = _load(tmp_path, {
        FLAG: True,
        "global_limiter_state_path":
            "var/state/nonexistent_r2_subdir/state.json",
    })
    assert settings.global_bybit_rate_limiter_enabled is True
    cfg = LimiterConfig.from_settings(settings)
    assert not Path(cfg.state_path).exists()        # no I/O at load time


# ============================================== 8. no unexpected HTTP
def test_invalid_config_fails_before_any_client_or_http(tmp_path):
    with pytest.raises(ValueError, match="g_rps"):
        _load(tmp_path, {"global_limiter_g_rps": -1.0})
    # nothing was constructed: no BybitClient, no session, no state file —
    # the poisoned urlopen would have failed the test if anything had run.


def test_env_activation_denied_produces_disabled_client(tmp_path,
                                                        monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "true")        # permission NOT granted
    settings = _load(tmp_path, {})
    session = RecordingSession()
    client = BybitClient(settings, session=session)
    assert client._limiter_enabled() is False
    assert client._shared_limiter() is None
    assert client._shared_limiter_obj is None
    assert session.calls == []


def test_permitted_env_activation_builds_limiter_without_io_or_http(
        tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "true")
    settings = _load(tmp_path, {
        ALLOW_ENV: True,
        "global_limiter_state_path": "var/state/r2_activation_probe.json",
    })
    assert settings.global_bybit_rate_limiter_enabled is True
    session = RecordingSession()
    client = BybitClient(settings, session=session)
    assert client._limiter_enabled() is True
    limiter = client._shared_limiter()             # construction: NO I/O
    assert isinstance(limiter, GlobalBybitRateLimiter)
    assert Path(limiter.config.state_path).is_absolute()
    assert not Path(limiter.config.state_path).exists()   # untouched
    assert session.calls == []


def test_enabled_client_fails_closed_on_invalid_params(tmp_path):
    settings = Settings(**{FLAG: True, "global_limiter_g_rps": -1.0})
    client = BybitClient(settings, session=RecordingSession())
    with pytest.raises(ValueError, match="g_rps"):
        client._shared_limiter()                   # build-time validation
    assert client._shared_limiter_obj is None      # never constructed
