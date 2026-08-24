from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    category: str = "linear"
    timeframe: str = "5"
    trading_mode: str = "paper"
    live_trading_enabled: bool = False
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    initial_balance: float = 10_000.0
    risk_per_trade: float = 0.005
    max_open_positions: int = 3
    max_daily_loss: float = 0.03
    max_consecutive_losses: int = 4
    max_symbol_exposure: float = 0.20
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
    settings = Settings(**values)
    if settings.category != "linear":
        raise ValueError("Price/OI strategy requires category=linear")
    if settings.trading_mode not in {"paper", "live"}:
        raise ValueError("TRADING_MODE must be paper or live")
    return settings
