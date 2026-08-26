from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.scanners.breakout_retest import BreakoutRetestScanner
from app.scanners.deduplication import DeduplicationEngine
from app.scanners.liquidity_reversal import LiquidityReversalScanner
from app.scanners.liquidity_sweep_choch import LiquiditySweepCHOCHScanner
from app.scanners.models import MarketContext, SetupCandidate
from app.scanners.momentum_exhaustion import MomentumExhaustionScanner
from app.scanners.expectancy_filter import ExpectancyFilter, filter_candidates
from app.scanners.risk_geometry import validate_risk_geometry
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

    def scan_all(
        self,
        ctx: MarketContext,
        expectancy_filter: ExpectancyFilter | None = None,
        min_avg_r: float = 0.0,
    ) -> list[SetupCandidate]:
        candidates, _ = self.scan_all_with_stats(ctx, expectancy_filter, min_avg_r)
        return candidates

    def scan_all_with_stats(
        self,
        ctx: MarketContext,
        expectancy_filter: ExpectancyFilter | None = None,
        min_avg_r: float = 0.0,
    ) -> tuple[list[SetupCandidate], dict[str, dict[str, int | float]]]:
        """Run every configured scanner and return per-scanner observability data."""
        self.scan_count += 1
        self.last_scan_time = datetime.now(timezone.utc)
        all_candidates: list[SetupCandidate] = []
        stats: dict[str, dict[str, int | float]] = {}

        for name, scanner in self.scanners.items():
            started = time.perf_counter()
            try:
                candidates = scanner.scan(ctx)
                all_candidates.extend(candidates)
                stats[name] = {
                    "candidates_found": len(candidates),
                    "errors_count": 0,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            except Exception:
                logger.exception("scanner %s failed on %s", name, ctx.symbol)
                stats[name] = {
                    "candidates_found": 0,
                    "errors_count": 1,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                }

        scored = [score_candidate(c) for c in all_candidates]
        scored = [self._attach_context(c, ctx) for c in scored]
        scored.sort(key=lambda c: c.score, reverse=True)
        unique = self.dedup.filter_new(scored)

        valid: list[SetupCandidate] = []
        invalid_geometry_by_scanner: dict[str, int] = {}
        for c in unique:
            risk_ok, reason = validate_risk_geometry(c)
            if not risk_ok:
                invalid_geometry_by_scanner[c.scanner_name] = (
                    invalid_geometry_by_scanner.get(c.scanner_name, 0) + 1
                )
                logger.warning(
                    "scanner rejected invalid risk geometry: symbol=%s scanner=%s direction=%s reason=%s entry=%s-%s stop=%s target_1=%s",
                    c.symbol, c.scanner_name, c.direction, reason,
                    c.entry_zone_low, c.entry_zone_high,
                    c.invalidation_price, c.target_1,
                )
                continue
            if c.score >= 20:
                valid.append(c)

        # Expectancy filter: drop scanner/direction combos with negative historical R
        expectancy_rejected = 0
        if expectancy_filter is not None:
            valid, expectancy_rejected = filter_candidates(
                valid, expectancy_filter, min_avg_r=min_avg_r,
            )

        if valid:
            logger.info(
                "scanner scan=%d symbol=%s candidates=%d unique=%d",
                self.scan_count, ctx.symbol, len(all_candidates), len(valid),
            )
        saved_by_scanner: dict[str, int] = {}
        for candidate in valid:
            saved_by_scanner[candidate.scanner_name] = (
                saved_by_scanner.get(candidate.scanner_name, 0) + 1
            )
        for name in stats:
            stats[name]["setups_saved"] = saved_by_scanner.get(name, 0)
        return valid, stats

    @classmethod
    def _attach_context(cls, c: SetupCandidate, ctx: MarketContext) -> SetupCandidate:
        candidate = cls._attach_signal_candle(c, ctx)
        if candidate.market_regime is None:
            candidate = replace(candidate, market_regime=ctx.market_regime)
        return candidate

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
