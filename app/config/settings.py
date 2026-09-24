from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScannerUniverseSettings:
    """Controls how scanner symbols are selected."""

    mode: str = "static"
    quote_coin: str = "USDT"
    category: str = "linear"
    top_n: int = 50
    min_turnover_24h: float = 10_000_000.0
    min_volume_24h: float = 0.0


@dataclass(frozen=True)
class DCASettings:
    """DCA (Dollar Cost Averaging) Breakeven configuration.

    Applied to all active scanner/direction combinations when enabled.
    Operates at the position management level — not inside scanners.
    """
    enabled: bool = False
    level_atr: float = 0.75
    initial_entry_pct: float = 0.50
    dca_entry_pct: float = 0.50
    exit_mode: str = "breakeven"
    stop_loss_atr: float = 1.5
    stop_reference: str = "initial_entry"
    max_dca_count: int = 1


@dataclass(frozen=True)
class ExecutionPolicyConfig:
    """Scanner-direction-specific execution policy configuration."""
    policy: str = "DEFAULT"
    enabled: bool = False
    hold_minutes: int = 240
    dca_enabled: bool = False
    trailing_enabled: bool = False
    breakeven_enabled: bool = False
    tp_enabled: bool = False
    expiry_enabled: bool = False


@dataclass(frozen=True)
class ExperimentalScannerConfig:
    """Configuration for an experimental shadow/counterfactual scanner.

    Shadow scanners derive trades from an existing scanner's signals but
    test an alternative hypothesis (e.g. reversed direction, different
    stop/target geometry) without affecting the paper balance or live trading.
    """
    enabled: bool = False
    mode: str = "shadow"
    source_scanner: str = ""
    source_direction: str = "SHORT"
    trade_direction: str = "LONG"
    stop_loss_pct: float = 2.5
    take_profit_pct: float = 3.0
    dca_enabled: bool = False
    trailing_enabled: bool = False
    breakeven_enabled: bool = False


@dataclass(frozen=True)
class Settings:
    # --- PostgreSQL database configuration ---
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "trad_bot"
    db_user: str = "postgres"
    db_password: str = ""
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    category: str = "linear"
    timeframe: str = "5"
    trading_mode: str = "paper"
    live_trading_enabled: bool = False
    # Internal kill switch: remains false until protected SL/TP, reduce-only,
    # confirmation, reconciliation and restart recovery are implemented.
    live_safety_ready: bool = False
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_timeout: float = 15.0
    bybit_max_attempts: int = 3
    bybit_retry_backoff: float = 1.0
    bybit_rate_limit_rps: float = 10.0
    bybit_rate_limit_max_retries: int = 5
    bybit_rate_limit_retry_backoff: float = 0.5
    scanner_workers: int = 5
    scan_interval: int = 300
    # OOS evaluator interval in scanner cycles (default 12 = every 60 minutes
    # with 5-minute scan interval). Set to 0 to disable in-scanner evaluation.
    oos_evaluator_cycle_interval: int = 12
    signal_conflict_window: int = 600
    telegram_token: str = ""
    telegram_chat_id: str = ""
    initial_balance: float = 10_000.0
    risk_per_trade: float = 0.005
    max_open_positions: int = 3
    max_daily_loss: float = 0.03
    max_consecutive_losses: int = 4
    max_symbol_exposure: float = 0.20
    max_portfolio_gross_exposure: float = 0.60
    max_portfolio_net_exposure: float = 0.40
    maker_fee: float = 0.0002
    taker_fee: float = 0.00055
    slippage_percent: float = 0.0005
    atr_period: int = 14
    rsi_period: int = 14
    ma_period: int = 20
    volume_period: int = 20
    atr_stop_multiple: float = 1.5
    reward_risk: float = 2.0
    fomo_price_threshold: float = 8.0
    fomo_oi_threshold: float = 15.0
    fomo_atr_multiple: float = 2.0
    funding_warning: float = 0.03
    funding_block: float = 0.05
    flat_price_threshold: float = 0.15
    trend_start: dict[str, float] = field(default_factory=lambda: {
        "price_change_min": 0.5, "price_change_max": 3.0,
        "oi_change_min": 5.0, "volume_ratio_min": 1.5, "funding_max": 0.03,
    })
    compression: dict[str, float] = field(default_factory=lambda: {
        "price_range_max": 1.0, "oi_change_min": 10.0,
        "volume_ratio_min": 1.0, "breakout_volume_min": 1.2,
    })
    capitulation: dict[str, float] = field(default_factory=lambda: {
        "price_change_max": -8.0, "oi_change_max": -15.0,
        "volume_ratio_min": 2.0, "oi_stabilization_min": -2.0,
    })
    data_file: str = "data/trades.jsonl"
    rejection_file: str = "data/rejections.jsonl"
    market_data_file: str = "data/market_snapshots.jsonl"
    scanner_universe: ScannerUniverseSettings = field(default_factory=ScannerUniverseSettings)
    # Expectancy filter: reject scanner/direction combos with negative historical R.
    expectancy_filter_enabled: bool = False
    expectancy_min_avg_r: float = 0.0
    expectancy_min_samples: int = 30
    expectancy_min_profit_factor: float = 1.20
    expectancy_min_net_pnl: float = 0.0
    # Explicitly paused scanner/direction combinations, regardless of sample size.
    # This safety blocklist follows the 2026-09-01 paper-trading analysis.
    # Blocked combinations remain observable through scanner outcomes, but are
    # never persisted as tradeable setups or opened by paper_runner.
    blocked_scanner_directions: tuple[tuple[str, str], ...] = (
        ("VOLATILITY_COMPRESSION", "LONG"),
        ("VOLATILITY_COMPRESSION", "SHORT"),
        ("SUPPORT_RESISTANCE_REACTION", "LONG"),
        ("SUPPORT_RESISTANCE_REACTION", "SHORT"),
        ("LIQUIDITY_REVERSAL", "SHORT"),
        ("BREAKOUT_RETEST", "LONG"),
        ("BREAKOUT_RETEST", "SHORT"),
        ("MOMENTUM_EXHAUSTION", "LONG"),
        ("MOMENTUM_EXHAUSTION", "SHORT"),  # Block original ME SHORT for experiment
        ("TREND_PULLBACK_V2", "SHORT"),
        ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG"),  # Blocked for OOS validation
        ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "SHORT"),  # Reverse long only trades LONG
        ("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "SHORT"),  # Reverse long V2 only trades LONG
        ("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1", "SHORT"),  # OOS scanner only trades LONG
    )
    # Optional scanner/direction regime allow-lists. Unspecified scanners use
    # the generic direction-conflict filter; an empty tuple blocks a direction.
    scanner_regime_whitelist: dict[str, dict[str, tuple[str, ...]]] = field(
        default_factory=lambda: {
            "TREND_PULLBACK_V2": {"LONG": ("TREND_UP",)},
        }
    )
    # Live gate thresholds measured from persisted forward paper trading.
    paper_min_forward_days: int = 14
    paper_min_closed_trades: int = 100
    paper_min_avg_r: float = 0.0
    paper_min_profit_factor: float = 1.0
    paper_max_drawdown: float = 0.10
    # Legacy reporting threshold retained for backwards-compatible settings.
    # Severe STOP_LOSS_GAP gating uses the execution-gap thresholds below.
    paper_max_loss_r_per_trade: float = 1.2
    # Halt only for an anomalous market move through the stop or for execution
    # materially worse than the normal all-in stop fill.
    paper_severe_stop_gap_r: float = 0.20
    paper_severe_execution_extra_r: float = 0.15
    # Controls enforcement after a severe STOP_LOSS_GAP.  Observation remains
    # persisted in every mode; only enforce activates the durable entry gate.
    paper_safety_gate_mode: str = "enforce"
    paper_funding_interval_hours: int = 8
    paper_emergency_stop_file: str = "data/PAPER_TRADING_STOP"
    # Paper trading scan interval in seconds (default 300 = 5 minutes).
    paper_scan_interval: int = 300
    # Multiplier for setup TTL — doubles the default entry-timeout bars.
    # 2.0 means a 5m setup lives 2 hours instead of 1 hour.
    setup_ttl_multiplier: float = 2.0
    # Minimum effective risk ratio for paper entries.  After all exposure
    # caps are applied, if actual risk_usdt / requested_risk falls below
    # this threshold the trade is rejected (position too small to be useful).
    # 0.50 means the trade must retain at least 50 % of the originally
    # requested risk amount.  Set to 0.0 to disable the gate (legacy behavior).
    min_effective_risk_ratio: float = 0.50
    # When enabled, reject entries where direction conflicts with the
    # market regime (e.g. LONG in TREND_DOWN, SHORT in TREND_UP).
    regime_filter_enabled: bool = True
    # Paper consecutive-loss cooldown: after max_consecutive_losses is
    # reached, block new entries for this many minutes before allowing
    # fresh entries again.  Set to 0 to keep the old hard-stop behavior.
    paper_consecutive_loss_cooldown_minutes: int = 5
    # Position monitor interval in seconds (default 10).
    # Controls how often open positions are checked for SL/TP/trailing
    # in the background thread.  Independent of paper_scan_interval.
    position_monitor_interval: int = 10
    # DCA Breakeven configuration
    dca: DCASettings = field(default_factory=DCASettings)
    # Scanner-direction-specific execution policies.
    # Key structure: { scanner_name: { direction: policy_config } }
    # Example:
    #   "MOMENTUM_EXHAUSTION": {
    #       "SHORT": {
    #           "policy": "FIXED_HORIZON_V1",
    #           "hold_minutes": 240,
    #           "dca_enabled": false,
    #           "trailing_enabled": false,
    #           "breakeven_enabled": false,
    #           "tp_enabled": false,
    #           "expiry_enabled": false
    #       }
    #   }
    # Missing scanner/direction = DEFAULT existing behavior.
    execution_policies: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # Parsed execution policy configs (scanner_name → direction → ExecutionPolicyConfig)
    execution_policy_configs: dict[str, dict[str, ExecutionPolicyConfig]] = field(default_factory=dict, repr=False)
    # Experimental scanners configuration (shadow/counterfactual strategies).
    # Key structure: { scanner_name: ExperimentalScannerConfig }
    experimental_scanners: dict[str, ExperimentalScannerConfig] = field(default_factory=dict, repr=False)
    analytics_schedule_time: str = "06:00"
    analytics_timezone: str = "Europe/Sofia"
    analytics_post_exit_hours: int = 4
    analytics_candle_retention_days: int = 180
    analytics_candle_workers: int = 2
    analytics_api_retry_count: int = 3
    analytics_stage_timeout_seconds: int = 3600
    analytics_enabled: bool = False


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_TRUE_VALUES: frozenset[str] = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES: frozenset[str] = frozenset({"false", "0", "no", "off"})

# Tracks where DCA enabled flag was sourced from (last load_settings call).
_last_dca_source: str = "config"


def get_dca_source() -> str:
    """Return the source of the DCA enabled flag from the last load_settings call."""
    return _last_dca_source


def _load_execution_policies(settings: Settings, raw: dict) -> None:
    """Parse execution_policies from config + env overrides into typed configs.

    Priority: ENV > config.yaml > application default.
    Currently supports env override for MOMENTUM_EXHAUSTION SHORT enabled flag:
        ME_SHORT_FIXED_240M_ENABLED
    """
    raw_policies = raw.get("execution_policies", {})
    if not raw_policies or not isinstance(raw_policies, dict):
        raw_policies = {}

    # Environment override for ME SHORT FIXED_HORIZON_V1 enabled flag
    me_short_env = os.getenv("ME_SHORT_FIXED_240M_ENABLED")

    parsed: dict[str, dict[str, ExecutionPolicyConfig]] = {}
    for scanner_name, directions in raw_policies.items():
        if not isinstance(directions, dict):
            continue
        parsed[scanner_name] = {}
        for direction, policy_raw in directions.items():
            if not isinstance(policy_raw, dict):
                continue
            enabled = policy_raw.get("enabled", False)
            config_source = "CONFIG"

            # Apply env override for MOMENTUM_EXHAUSTION SHORT
            if scanner_name == "MOMENTUM_EXHAUSTION" and direction == "SHORT":
                if me_short_env is not None:
                    enabled = _parse_bool_env(me_short_env, "ME_SHORT_FIXED_240M_ENABLED")
                    config_source = "ENV"

            policy_config = ExecutionPolicyConfig(
                policy=policy_raw.get("policy", "DEFAULT"),
                enabled=enabled,
                hold_minutes=policy_raw.get("hold_minutes", 240),
                dca_enabled=policy_raw.get("dca_enabled", False),
                trailing_enabled=policy_raw.get("trailing_enabled", False),
                breakeven_enabled=policy_raw.get("breakeven_enabled", False),
                tp_enabled=policy_raw.get("tp_enabled", False),
                expiry_enabled=policy_raw.get("expiry_enabled", False),
            )
            if policy_config.hold_minutes <= 0:
                raise ValueError(
                    f"execution_policies.{scanner_name}.{direction}.hold_minutes "
                    f"must be positive, got {policy_config.hold_minutes}"
                )
            parsed[scanner_name][direction] = policy_config
            # Store config source for startup logging
            if not hasattr(settings, "_execution_policy_sources"):
                object.__setattr__(settings, "_execution_policy_sources", {})
            settings._execution_policy_sources[(scanner_name, direction)] = config_source

    # Use object.__setattr__ because Settings is a frozen dataclass
    object.__setattr__(settings, "execution_policy_configs", parsed)


def _load_experimental_scanners(settings: Settings, raw: dict) -> None:
    """Parse experimental_scanners from config into typed ExperimentalScannerConfig.

    Experimental scanners run as shadow/counterfactual strategies that derive
    trades from existing scanner signals without affecting the paper balance.
    """
    raw_experimental = raw.get("experimental_scanners", {})
    if not raw_experimental or not isinstance(raw_experimental, dict):
        raw_experimental = {}

    parsed: dict[str, ExperimentalScannerConfig] = {}
    for scanner_name, config_raw in raw_experimental.items():
        if not isinstance(config_raw, dict):
            continue

        parsed[scanner_name] = ExperimentalScannerConfig(
            enabled=config_raw.get("enabled", False),
            mode=config_raw.get("mode", "shadow"),
            source_scanner=config_raw.get("source_scanner", ""),
            source_direction=config_raw.get("source_direction", "SHORT"),
            trade_direction=config_raw.get("trade_direction", "LONG"),
            stop_loss_pct=config_raw.get("stop_loss_pct", 2.5),
            take_profit_pct=config_raw.get("take_profit_pct", 3.0),
            dca_enabled=config_raw.get("dca_enabled", False),
            trailing_enabled=config_raw.get("trailing_enabled", False),
            breakeven_enabled=config_raw.get("breakeven_enabled", False),
        )

    # Use object.__setattr__ because Settings is a frozen dataclass
    object.__setattr__(settings, "experimental_scanners", parsed)


def _parse_bool_env(value: str, env_name: str) -> bool:
    """Parse a boolean environment variable.  Fail-fast on invalid values.

    Accepted truthy: true, 1, yes, on (case-insensitive).
    Accepted falsy:  false, 0, no, off (case-insensitive).
    Anything else raises ValueError to prevent silent misconfiguration.
    """
    normalised = value.strip().lower()
    if normalised in _TRUE_VALUES:
        return True
    if normalised in _FALSE_VALUES:
        return False
    raise ValueError(
        f"Invalid {env_name}={value!r}. "
        f"Expected one of: true/1/yes/on or false/0/no/off (case-insensitive)."
    )


def load_settings(path: str | Path = "config.yaml", env_file: str | Path = ".env") -> Settings:
    _load_dotenv(Path(env_file))
    raw: dict[str, Any] = {}
    config_path = Path(path)
    if config_path.exists():
        # JSON is a strict YAML 1.2 subset, keeping the configuration portable
        # without requiring a parser merely to validate/start the application.
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    env = {
        "db_host": os.getenv("DB_HOST", raw.get("db_host", "localhost")),
        "db_port": int(os.getenv("DB_PORT", str(raw.get("db_port", 5432)))),
        "db_name": os.getenv("DB_NAME", raw.get("db_name", "trad_bot")),
        "db_user": os.getenv("DB_USER", raw.get("db_user", "postgres")),
        "db_password": os.getenv("DB_PASSWORD", raw.get("db_password", "")),
        "trading_mode": os.getenv("TRADING_MODE", raw.get("trading_mode", "paper")).lower(),
        "live_trading_enabled": os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true",
        "bybit_api_key": os.getenv("BYBIT_API_KEY", ""),
        "bybit_api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "telegram_token": os.getenv("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "max_open_positions": int(
            os.getenv(
                "MAX_OPEN_POSITIONS",
                str(raw.get("max_open_positions", 3)),
            )
        ),
        "paper_safety_gate_mode": str(os.getenv(
            "PAPER_SAFETY_GATE_MODE", raw.get("paper_safety_gate_mode", "enforce")
        )).strip().lower(),
    }
    allowed = Settings.__dataclass_fields__.keys()
    values = {k: v for k, v in raw.items() if k in allowed}
    values.update(env)
    if "symbols" in values:
        values["symbols"] = tuple(values["symbols"])
    if "blocked_scanner_directions" in values:
        try:
            values["blocked_scanner_directions"] = tuple(
                (str(scanner_name).upper(), str(direction).upper())
                for scanner_name, direction in values["blocked_scanner_directions"]
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "blocked_scanner_directions must contain [scanner_name, direction] pairs"
            ) from exc
    if "scanner_universe" in values and isinstance(values["scanner_universe"], dict):
        values["scanner_universe"] = ScannerUniverseSettings(**values["scanner_universe"])
    if "scanner_regime_whitelist" in values:
        try:
            values["scanner_regime_whitelist"] = {
                str(scanner_name).upper(): {
                    str(direction).upper(): tuple(
                        str(regime).upper() for regime in regimes
                    )
                    for direction, regimes in directions.items()
                }
                for scanner_name, directions in values["scanner_regime_whitelist"].items()
            }
        except (AttributeError, TypeError) as exc:
            raise ValueError(
                "scanner_regime_whitelist must map scanner names to direction/regime lists"
            ) from exc
    # DCA settings: load from nested "dca" key in config
    dca_raw = raw.get("dca", {})
    dca_source = "config"
    if dca_raw and isinstance(dca_raw, dict):
        dca_allowed = DCASettings.__dataclass_fields__.keys()
        dca_values = {k: v for k, v in dca_raw.items() if k in dca_allowed}
        # ENV override: DCA_ENABLED takes priority over config.yaml
        dca_enabled_env = os.getenv("DCA_ENABLED")
        if dca_enabled_env is not None:
            dca_values["enabled"] = _parse_bool_env(dca_enabled_env, "DCA_ENABLED")
            dca_source = "env"
        values["dca"] = DCASettings(**dca_values)
    else:
        # No DCA config in config.yaml — check env anyway
        dca_enabled_env = os.getenv("DCA_ENABLED")
        if dca_enabled_env is not None:
            dca_values = {"enabled": _parse_bool_env(dca_enabled_env, "DCA_ENABLED")}
            dca_source = "env"
            values["dca"] = DCASettings(**dca_values)
    settings = Settings(**values)
    # Expose the DCA config source via module-level variable.
    # Used by paper_runner for startup logging.
    global _last_dca_source
    _last_dca_source = dca_source
    # Load execution policies from config
    _load_execution_policies(settings, raw)
    # Load experimental scanners from config
    _load_experimental_scanners(settings, raw)
    if settings.category != "linear":
        raise ValueError("Price/OI strategy requires category=linear")
    if settings.trading_mode not in {"paper", "live"}:
        raise ValueError("TRADING_MODE must be paper or live")
    universe = settings.scanner_universe
    if universe.mode not in {"static", "dynamic"}:
        raise ValueError("scanner_universe.mode must be static or dynamic")
    if universe.category != "linear":
        raise ValueError("scanner_universe.category must be linear")
    if universe.top_n <= 0:
        raise ValueError("scanner_universe.top_n must be positive")
    if universe.min_turnover_24h < 0 or universe.min_volume_24h < 0:
        raise ValueError("scanner universe liquidity thresholds cannot be negative")
    if settings.bybit_timeout <= 0 or settings.bybit_max_attempts <= 0:
        raise ValueError("Bybit timeout and max attempts must be positive")
    if not 1 <= settings.db_port <= 65535:
        raise ValueError("db_port must be between 1 and 65535")
    if not settings.db_name:
        raise ValueError("db_name must not be empty")
    if settings.bybit_retry_backoff < 0:
        raise ValueError("bybit_retry_backoff cannot be negative")
    if settings.bybit_rate_limit_rps <= 0:
        raise ValueError("bybit_rate_limit_rps must be positive")
    if settings.bybit_rate_limit_max_retries < 0:
        raise ValueError("bybit_rate_limit_max_retries cannot be negative")
    if settings.bybit_rate_limit_retry_backoff < 0:
        raise ValueError("bybit_rate_limit_retry_backoff cannot be negative")
    if settings.initial_balance <= 0:
        raise ValueError("initial_balance must be positive")
    if not 0 < settings.risk_per_trade <= 1:
        raise ValueError("risk_per_trade must be in (0, 1]")
    if settings.max_open_positions <= 0 or settings.max_consecutive_losses <= 0:
        raise ValueError("paper position and loss-streak limits must be positive")
    if not 0 < settings.max_daily_loss < 1:
        raise ValueError("max_daily_loss must be in (0, 1)")
    if not 0 < settings.max_symbol_exposure <= 1:
        raise ValueError("max_symbol_exposure must be in (0, 1]")
    if not 0 < settings.max_portfolio_gross_exposure <= 1:
        raise ValueError("max_portfolio_gross_exposure must be in (0, 1]")
    if not 0 <= settings.max_portfolio_net_exposure <= settings.max_portfolio_gross_exposure:
        raise ValueError("max_portfolio_net_exposure must not exceed gross exposure")
    if any(value < 0 for value in (settings.maker_fee, settings.taker_fee, settings.slippage_percent)):
        raise ValueError("paper fees and slippage cannot be negative")
    if settings.atr_stop_multiple <= 0:
        raise ValueError("atr_stop_multiple must be positive")
    if settings.paper_min_forward_days <= 0 or settings.paper_min_closed_trades <= 0:
        raise ValueError("paper forward-test thresholds must be positive")
    if not 0 < settings.paper_max_drawdown < 1:
        raise ValueError("paper_max_drawdown must be in (0, 1)")
    if settings.paper_min_avg_r < 0:
        raise ValueError("paper_min_avg_r cannot be negative")
    if settings.paper_min_profit_factor <= 0:
        raise ValueError("paper_min_profit_factor must be positive")
    if settings.paper_max_loss_r_per_trade < 1:
        raise ValueError("paper_max_loss_r_per_trade must be at least 1")
    if settings.paper_severe_stop_gap_r < 0:
        raise ValueError("paper_severe_stop_gap_r cannot be negative")
    if settings.paper_severe_execution_extra_r < 0:
        raise ValueError("paper_severe_execution_extra_r cannot be negative")
    if settings.paper_safety_gate_mode not in {"enforce", "observe", "disabled"}:
        raise ValueError(
            "PAPER_SAFETY_GATE_MODE must be enforce, observe, or disabled"
        )
    if settings.paper_funding_interval_hours <= 0:
        raise ValueError("paper_funding_interval_hours must be positive")
    if (settings.scanner_workers <= 0 or settings.scan_interval <= 0
            or settings.signal_conflict_window <= 0):
        raise ValueError("scanner workers and interval must be positive")
    if settings.paper_scan_interval <= 0:
        raise ValueError("paper_scan_interval must be positive")
    if settings.setup_ttl_multiplier <= 0:
        raise ValueError("setup_ttl_multiplier must be positive")
    if settings.paper_consecutive_loss_cooldown_minutes < 0:
        raise ValueError("paper_consecutive_loss_cooldown_minutes cannot be negative")
    # DCA validation
    dca = settings.dca
    if not 0 < dca.initial_entry_pct <= 1:
        raise ValueError("dca.initial_entry_pct must be in (0, 1]")
    if not 0 < dca.dca_entry_pct <= 1:
        raise ValueError("dca.dca_entry_pct must be in (0, 1]")
    if abs(dca.initial_entry_pct + dca.dca_entry_pct - 1.0) > 0.001:
        raise ValueError("dca.initial_entry_pct + dca.dca_entry_pct must equal 1.0")
    if dca.level_atr <= 0:
        raise ValueError("dca.level_atr must be positive")
    if dca.stop_loss_atr <= 0:
        raise ValueError("dca.stop_loss_atr must be positive")
    if dca.exit_mode not in {"breakeven"}:
        raise ValueError("dca.exit_mode must be 'breakeven'")
    if dca.stop_reference not in {"initial_entry"}:
        raise ValueError("dca.stop_reference must be 'initial_entry'")
    if dca.max_dca_count < 0:
        raise ValueError("dca.max_dca_count must be non-negative")
    return settings
