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
    scanner_workers: int = 5
    scan_interval: int = 300
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
    blocked_scanner_directions: tuple[tuple[str, str], ...] = (
        ("VOLATILITY_COMPRESSION", "SHORT"),
        ("BREAKOUT_RETEST", "SHORT"),
        ("BREAKOUT_RETEST", "LONG"),
        ("MOMENTUM_EXHAUSTION", "LONG"),
    )
    # Live gate thresholds measured from persisted forward paper trading.
    paper_min_forward_days: int = 14
    paper_min_closed_trades: int = 30
    paper_max_drawdown: float = 0.10
    paper_funding_interval_hours: int = 8
    paper_emergency_stop_file: str = "data/PAPER_TRADING_STOP"
    # Paper trading scan interval in seconds (default 300 = 5 minutes).
    paper_scan_interval: int = 300
    # Multiplier for setup TTL — doubles the default entry-timeout bars.
    # 2.0 means a 5m setup lives 2 hours instead of 1 hour.
    setup_ttl_multiplier: float = 2.0
    # When enabled, reject entries where direction conflicts with the
    # market regime (e.g. LONG in TREND_DOWN, SHORT in TREND_UP).
    regime_filter_enabled: bool = True


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


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
    settings = Settings(**values)
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
    if settings.paper_funding_interval_hours <= 0:
        raise ValueError("paper_funding_interval_hours must be positive")
    if (settings.scanner_workers <= 0 or settings.scan_interval <= 0
            or settings.signal_conflict_window <= 0):
        raise ValueError("scanner workers and interval must be positive")
    if settings.paper_scan_interval <= 0:
        raise ValueError("paper_scan_interval must be positive")
    if settings.setup_ttl_multiplier <= 0:
        raise ValueError("setup_ttl_multiplier must be positive")
    return settings
