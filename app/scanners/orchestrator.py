from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.scanners.breakout_retest import BreakoutRetestScanner
from app.scanners.deduplication import DeduplicationEngine
from app.scanners.liquidity_reversal import LiquidityReversalScanner
from app.scanners.liquidity_sweep_choch import LiquiditySweepCHOCHScanner
from app.scanners.models import MarketContext, SetupCandidate
from app.scanners.momentum_exhaustion import MomentumExhaustionScanner
from app.scanners.scoring import score_candidate
from app.scanners.support_resistance import SupportResistanceScanner
from app.scanners.trend_pullback import TrendPullbackScanner
from app.scanners.volatility_compression import VolatilityCompressionScanner

if TYPE_CHECKING:
    from app.db.repository import ScannerRepository

logger = logging.getLogger(__name__)

EXPIRATION_MAP = {
    "5m": 12,
    "15m": 8,
    "1h": 6,
    "4h": 4,
}


class ScannerOrchestrator:
    def __init__(
        self,
        enabled_scanners: list[str] | None = None,
        repository: ScannerRepository | None = None,
    ) -> None:
        self.repository = repository
        all_scanners = {
            "LIQUIDITY_SWEEP_CHOCH_OB": LiquiditySweepCHOCHScanner(),
            "BREAKOUT_RETEST": BreakoutRetestScanner(),
            "LIQUIDITY_REVERSAL": LiquidityReversalScanner(),
            "TREND_PULLBACK": TrendPullbackScanner(),
            "VOLATILITY_COMPRESSION": VolatilityCompressionScanner(),
            "SUPPORT_RESISTANCE_REACTION": SupportResistanceScanner(),
            "MOMENTUM_EXHAUSTION": MomentumExhaustionScanner(),
        }

        if enabled_scanners is not None:
            self.scanners = {
                name: s for name, s in all_scanners.items()
                if name in enabled_scanners
            }
        else:
            self.scanners = all_scanners

        self.dedup = DeduplicationEngine()
        self.scan_count = 0
        self.last_scan_time: datetime | None = None

    def scan_all(self, ctx: MarketContext) -> list[SetupCandidate]:
        self.scan_count += 1
        self.last_scan_time = datetime.now(timezone.utc)
        all_candidates: list[SetupCandidate] = []

        for name, scanner in self.scanners.items():
            try:
                candidates = scanner.scan(ctx)
                all_candidates.extend(candidates)
            except Exception:
                logger.exception("scanner %s failed on %s", name, ctx.symbol)

        scored = [score_candidate(c) for c in all_candidates]
        scored = [self._attach_signal_candle(c, ctx) for c in scored]
        scored.sort(key=lambda c: c.score, reverse=True)
        unique = self.dedup.filter_new(scored)

        valid: list[SetupCandidate] = []
        for c in unique:
            if c.score >= 20:
                valid.append(c)

        if valid:
            logger.info(
                "scanner scan=%d symbol=%s candidates=%d unique=%d",
                self.scan_count, ctx.symbol, len(all_candidates), len(valid),
            )
        return valid

    @staticmethod
    def _attach_signal_candle(c: SetupCandidate, ctx: MarketContext) -> SetupCandidate:
        if c.signal_candle_open_time:
            return c
        candles_by_timeframe = {
            "5m": ctx.candles_5m,
            "15m": ctx.candles_15m,
            "1h": ctx.candles_1h,
            "4h": ctx.candles_4h,
        }
        candles = candles_by_timeframe.get(c.entry_timeframe, ())
        if not candles:
            return c
        return replace(c, signal_candle_open_time=candles[-1].timestamp)

    def check_expiration(
        self,
        candidates: list[SetupCandidate],
        current_time: datetime | None = None,
    ) -> list[SetupCandidate]:
        now = current_time or datetime.now(timezone.utc)
        expired: list[SetupCandidate] = []
        for c in candidates:
            max_candles = EXPIRATION_MAP.get(c.setup_timeframe, 8)
            tf_minutes = int(c.setup_timeframe.replace("m", "").replace("h", ""))
            if "h" in c.setup_timeframe:
                tf_minutes *= 60
            max_age = max_candles * tf_minutes * 60
            age = (now - c.detected_at).total_seconds()
            if age > max_age:
                expired.append(c)
        return expired

    def get_stats(self) -> dict:
        return {
            "scan_count": self.scan_count,
            "last_scan_time": self.last_scan_time.isoformat() if self.last_scan_time else None,
            "active_scanners": list(self.scanners.keys()),
            "dedup_cache_size": len(self.dedup._seen),
        }

    def save_setup(self, candidate: SetupCandidate) -> None:
        if self.repository:
            self.repository.save_setup(candidate)
