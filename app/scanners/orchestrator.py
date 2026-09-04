from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.scanners.breakout_retest import BreakoutRetestScanner
from app.scanners.deduplication import DeduplicationEngine
from app.scanners.direction_gate import ScannerDirectionGatePolicy
from app.scanners.liquidity_reversal import LiquidityReversalScanner
from app.scanners.liquidity_sweep_choch import LiquiditySweepCHOCHScanner
from app.scanners.models import MarketContext, SetupCandidate
from app.scanners.momentum_exhaustion import MomentumExhaustionScanner
from app.scanners.momentum_exhaustion_r import MomentumExhaustionRScanner
from app.scanners.expectancy_filter import ExpectancyFilter, filter_candidates
from app.scanners.risk_geometry import validate_risk_geometry
from app.scanners.scoring import score_candidate
from app.scanners.support_resistance import SupportResistanceScanner
from app.scanners.trend_pullback_v2 import TrendPullbackScannerV2
from app.scanners.volatility_compression import VolatilityCompressionScanner

if TYPE_CHECKING:
    from app.db.repository import ScannerRepository

logger = logging.getLogger(__name__)

# Base expiration bars; actual = base * setup_ttl_multiplier (from settings).
EXPIRATION_BASE_MAP = {
    "5m": 12,
    "15m": 8,
    "1h": 6,
    "4h": 4,
}

# Legacy alias for callers that don't pass a multiplier.
EXPIRATION_MAP = dict(EXPIRATION_BASE_MAP)


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
            "TREND_PULLBACK_V2": TrendPullbackScannerV2(),
            "VOLATILITY_COMPRESSION": VolatilityCompressionScanner(),
            "SUPPORT_RESISTANCE_REACTION": SupportResistanceScanner(),
            "MOMENTUM_EXHAUSTION": MomentumExhaustionScanner(),
            "MOMENTUM_EXHAUSTION_R": MomentumExhaustionRScanner(),
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
        min_samples: int = 10,
        blocked_combinations: frozenset[tuple[str, str]] = frozenset(),
        gate_policy: ScannerDirectionGatePolicy | None = None,
        regime_filter: bool = False,
        scanner_regime_whitelist: dict[str, dict[str, tuple[str, ...]]] | None = None,
        trading_mode: str = "paper",
    ) -> list[SetupCandidate]:
        candidates, _ = self.scan_all_with_stats(
            ctx, expectancy_filter, min_avg_r, min_samples, blocked_combinations,
            gate_policy=gate_policy,
            regime_filter=regime_filter,
            scanner_regime_whitelist=scanner_regime_whitelist,
            trading_mode=trading_mode,
        )
        return candidates

    def scan_all_with_stats(
        self,
        ctx: MarketContext,
        expectancy_filter: ExpectancyFilter | None = None,
        min_avg_r: float = 0.0,
        min_samples: int = 10,
        blocked_combinations: frozenset[tuple[str, str]] = frozenset(),
        gate_policy: ScannerDirectionGatePolicy | None = None,
        regime_filter: bool = False,
        scanner_regime_whitelist: dict[str, dict[str, tuple[str, ...]]] | None = None,
        trading_mode: str = "paper",
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

        # Some scanners (currently TREND_PULLBACK_V2) emit a calibrated local
        # score with explainable reasons. Preserve it instead of silently
        # replacing it with the generic confirmation score.
        scored = [
            c if c.score > 0 and c.reasons else score_candidate(c)
            for c in all_candidates
        ]
        scored = [self._attach_context(c, ctx) for c in scored]
        scored = self._calibrate_scores(scored)
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
            if c.score >= 30:
                valid.append(c)

        # Direction gates are independent of expectancy.  Candidates were still
        # generated/scored above, so blocked strategies remain observable.
        if gate_policy is not None:
            gate_accepted: list[SetupCandidate] = []
            for candidate in valid:
                decision = gate_policy.evaluate(
                    candidate.scanner_name, candidate.direction, candidate.market_regime or ctx.market_regime
                )
                if decision.allowed:
                    gate_accepted.append(candidate)
                else:
                    logger.info(
                        "direction gate rejected: symbol=%s scanner=%s direction=%s "
                        "reason_code=%s gate_status=%s gate_reason=%s allowed_regimes=%s market_regime=%s",
                        candidate.symbol, candidate.scanner_name, candidate.direction,
                        decision.reason_code, decision.status, decision.reason,
                        decision.allowed_regimes, candidate.market_regime or ctx.market_regime,
                    )
            valid = gate_accepted

        # Expectancy filter: drop scanner/direction combos with negative historical R.
        # Static manual blocks are handled by the gate policy above.
        expectancy_rejected = 0
        if expectancy_filter is not None:
            valid, expectancy_rejected = filter_candidates(
                valid,
                expectancy_filter,
                min_avg_r=min_avg_r,
                min_samples=min_samples,
                blocked_combinations=blocked_combinations,
                trading_mode=trading_mode,
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

        # Market regime filter: apply scanner-specific allow-lists first, then
        # the generic direction-conflict policy for scanners without a rule.
        if regime_filter:
            valid = self._apply_regime_filter(
                ctx, valid, scanner_regime_whitelist or {},
            )

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
        ttl_multiplier: float = 1.0,
    ) -> list[SetupCandidate]:
        now = current_time or datetime.now(timezone.utc)
        expired: list[SetupCandidate] = []
        for c in candidates:
            base_candles = EXPIRATION_BASE_MAP.get(c.setup_timeframe, 8)
            max_candles = base_candles * ttl_multiplier
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

    @staticmethod
    def _calibrate_scores(candidates: list[SetupCandidate]) -> list[SetupCandidate]:
        """Normalize per-scanner scores to [10, 90] to remove scanner-identity bias.

        After the quality-metric feature overhaul, different scanners may still
        cluster in different score ranges.  This post-scoring calibration maps
        each scanner's score range into a common [10, 90] band so that a 60
        from BREAKOUT_RETEST means roughly the same quality as a 60 from
        LIQUIDITY_SWEEP_CHOCH_OB.
        """
        if not candidates:
            return candidates

        # Group by scanner
        by_scanner: dict[str, list[SetupCandidate]] = {}
        for c in candidates:
            by_scanner.setdefault(c.scanner_name, []).append(c)

        calibrated: list[SetupCandidate] = []
        for scanner_name, scanner_candidates in by_scanner.items():
            scores = [c.score for c in scanner_candidates]
            if len(scores) < 2:
                calibrated.extend(scanner_candidates)
                continue

            min_score = min(scores)
            max_score = max(scores)
            score_range = max_score - min_score

            if score_range > 0:
                for c in scanner_candidates:
                    normalized = ((c.score - min_score) / score_range) * 80 + 10  # [10, 90]
                    calibrated.append(replace(c, score=round(normalized, 2)))
            else:
                # All scores identical — map to midpoint
                mid = 50.0
                calibrated.extend(replace(c, score=mid) for c in scanner_candidates)

        return calibrated

    @staticmethod
    def _apply_regime_filter(
        ctx: MarketContext,
        candidates: list[SetupCandidate],
        scanner_regime_whitelist: dict[str, dict[str, tuple[str, ...]]] | None = None,
    ) -> list[SetupCandidate]:
        """Apply scanner-specific regime allow-lists and generic trend conflict rules."""
        regime = ctx.market_regime
        if not regime:
            return candidates
        whitelist = scanner_regime_whitelist or {}
        filtered = []
        for c in candidates:
            allowed_by_scanner = whitelist.get(c.scanner_name, {}).get(c.direction)
            if allowed_by_scanner is not None:
                if regime not in allowed_by_scanner:
                    logger.debug(
                        "regime filter: dropping %s %s %s in %s (scanner allow-list)",
                        c.symbol, c.scanner_name, c.direction, regime,
                    )
                    continue
            elif (c.direction == "LONG" and regime == "TREND_DOWN") or (
                c.direction == "SHORT" and regime == "TREND_UP"
            ):
                logger.debug(
                    "regime filter: dropping %s %s in %s",
                    c.symbol, c.direction, regime,
                )
                continue
            filtered.append(c)
        return filtered
