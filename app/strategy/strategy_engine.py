from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.models import MarketSnapshot, Setup, Side, StrategyDecision, TradeSignal
from app.strategy.regimes import classify_regime


@dataclass
class _Armed:
    upper: float
    lower: float
    timestamp: int


class StrategyEngine:
    """The single stateful signal implementation shared by runtime and backtest."""
    def __init__(self, settings: Settings):
        self.settings = settings
        self.compressions: dict[str, _Armed] = {}
        self.capitulations: dict[str, _Armed] = {}

    def _signal(self, s: MarketSnapshot, setup: Setup, side: Side, reason: str) -> TradeSignal:
        distance = self.settings.atr_stop_multiple * s.atr
        if side == Side.LONG:
            stop = min(s.close - distance, s.local_low)
            take_profit = s.close + (s.close - stop) * self.settings.reward_risk
        else:
            stop = max(s.close + distance, s.local_high)
            take_profit = s.close - (stop - s.close) * self.settings.reward_risk
        return TradeSignal(s.symbol, setup, side, s.close, stop, take_profit, 0.75, reason)

    def _is_fomo(self, s: MarketSnapshot) -> bool:
        return ((s.price_change_1h >= self.settings.fomo_price_threshold and
                 s.oi_change_1h >= self.settings.fomo_oi_threshold) or
                (s.high - s.low >= self.settings.fomo_atr_multiple * s.atr))

    def evaluate(self, s: MarketSnapshot) -> StrategyDecision:
        regime = classify_regime(s, self.settings.flat_price_threshold,
                                 self.settings.fomo_price_threshold, self.settings.fomo_oi_threshold).value

        # Confirmation of previously armed setups comes before detecting new ones.
        armed = self.compressions.get(s.symbol)
        if armed and s.timestamp > armed.timestamp and s.volume_ratio >= self.settings.compression["breakout_volume_min"]:
            if s.close > armed.upper:
                del self.compressions[s.symbol]
                signal = self._signal(s, Setup.OI_COMPRESSION, Side.LONG, "upper range breakout confirmed by volume")
                if self._is_fomo(s):
                    return StrategyDecision(None, Setup.OI_COMPRESSION, "FOMO_FILTER", regime)
                return StrategyDecision(signal=signal, state=regime)
            if s.close < armed.lower:
                del self.compressions[s.symbol]
                return StrategyDecision(self._signal(s, Setup.OI_COMPRESSION, Side.SHORT,
                                                      "lower range breakout confirmed by volume"), state=regime)

        capitulation = self.capitulations.get(s.symbol)
        if (capitulation and s.timestamp > capitulation.timestamp and s.low >= capitulation.lower and
                s.oi_change_5m >= self.settings.capitulation["oi_stabilization_min"] and
                s.close > s.open and s.close > s.previous_high):
            del self.capitulations[s.symbol]
            return StrategyDecision(self._signal(s, Setup.CAPITULATION, Side.LONG,
                                                  "low held, OI stabilized and bullish high broke"), state=regime)

        c = self.settings.capitulation
        if (s.price_change_1h <= c["price_change_max"] and s.oi_change_1h <= c["oi_change_max"] and
                s.volume_ratio >= c["volume_ratio_min"]):
            self.capitulations[s.symbol] = _Armed(s.local_high, s.low, s.timestamp)
            return StrategyDecision(None, Setup.CAPITULATION, "CAPITULATION_DETECTED_AWAIT_CONFIRMATION", regime)

        c = self.settings.compression
        if (abs(s.price_change_30m) <= c["price_range_max"] and s.oi_change_30m >= c["oi_change_min"] and
                s.volume_ratio >= c["volume_ratio_min"]):
            self.compressions[s.symbol] = _Armed(s.local_high, s.local_low, s.timestamp)
            return StrategyDecision(None, Setup.OI_COMPRESSION, "SETUP_ARMED_AWAIT_BREAKOUT", regime)

        t = self.settings.trend_start
        trend_candidate = (s.price_change_15m > t["price_change_min"] and
                           s.price_change_15m < t["price_change_max"] and
                           s.oi_change_15m > t["oi_change_min"] and
                           s.volume_ratio > t["volume_ratio_min"] and s.close > s.local_high)
        if trend_candidate:
            if self._is_fomo(s):
                return StrategyDecision(None, Setup.TREND_START, "FOMO_FILTER", regime)
            if s.funding_rate >= min(t["funding_max"], self.settings.funding_block):
                return StrategyDecision(None, Setup.TREND_START, "FUNDING_TOO_HIGH", regime)
            reason = (f"Price15m={s.price_change_15m:+.2f}% OI15m={s.oi_change_15m:+.2f}% "
                      f"VolumeRatio={s.volume_ratio:.2f} Funding={s.funding_rate:+.4f}%")
            return StrategyDecision(self._signal(s, Setup.TREND_START, Side.LONG, reason), state=regime)
        return StrategyDecision(state=regime)
