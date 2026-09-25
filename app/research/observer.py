"""Research Observer — captures scanner candidates for research.

Positioned IMMEDIATELY after scanner.scan(ctx), BEFORE any production
filtering (scoring, dedup, geometry, gates, expectancy, regime).

Design:
  - fail-open: every method catches all exceptions
  - never uses production transaction
  - never blocks scanner cycle
  - append-only: INSERT, no UPDATE in observe()
  - status resolution happens later via update_observation_status()
"""
from __future__ import annotations

import logging
from typing import Any

from app.research.models import ResearchObservation
from app.research.repository import ResearchRepository
from app.scanners.models import SetupCandidate

logger = logging.getLogger(__name__)


class ResearchObserver:
    """Captures ALL scanner candidates for research.

    Integration point: call observe() for every candidate from
    scanner.scan(ctx), BEFORE scoring/dedup/gates.

    This is a fail-open component: errors are logged and counted
    but never propagate to the caller.
    """

    def __init__(
        self,
        repository: ResearchRepository,
        experiments: dict[str, dict[str, Any]],
    ) -> None:
        """Parameters
        ----------
        repository : ResearchRepository
            Generic research DB layer (may share or be separate from production conn).
        experiments : dict
            Registry of experiments keyed by scanner_name.
            Each value must contain:
              - experiment_id: str
              - parameter_set_id: str
              - parameters: dict (frozen parameter snapshot)
              - htf_timeframe: str (default "1h")
              - setup_timeframe: str (default "15m")
              - entry_timeframe: str (default "5m")
        """
        self._repo = repository
        self._experiments = experiments
        self._stats: dict[str, int] = {}

    def observe(self, candidate: SetupCandidate) -> None:
        """Record a candidate as a research observation.

        Called for EVERY candidate from scanner.scan(ctx), BEFORE
        any filtering.  Fail-open: catches all exceptions.
        """
        exp = self._experiments.get(candidate.scanner_name)
        if exp is None:
            return  # scanner not registered for research

        try:
            obs = ResearchObservation(
                experiment_id=exp["experiment_id"],
                scanner_name=candidate.scanner_name,
                scanner_version=candidate.scanner_version,
                parameter_set_id=exp["parameter_set_id"],
                symbol=candidate.symbol,
                direction=candidate.direction,
                signal_time=candidate.detected_at,
                signal_candle_open_time=candidate.signal_candle_open_time,
                reference_price=candidate.reference_price,
                entry_zone_low=candidate.entry_zone_low,
                entry_zone_high=candidate.entry_zone_high,
                invalidation_price=candidate.invalidation_price,
                target_1=candidate.target_1,
                target_2=candidate.target_2,
                score=candidate.score,
                status="DETECTED",
                rejection_stage=None,
                rejection_reason=None,
                features=dict(candidate.features) if candidate.features else {},
                parameters=exp["parameters"],
                market_regime=candidate.market_regime,
                htf_timeframe=exp.get("htf_timeframe", "1h"),
                setup_timeframe=exp.get("setup_timeframe", "15m"),
                entry_timeframe=exp.get("entry_timeframe", "5m"),
                setup_id=str(candidate.setup_id) if candidate.setup_id else None,
            )

            obs_id = self._repo.save_observation(obs)
            if obs_id is not None:
                self._stats["inserted"] = self._stats.get("inserted", 0) + 1
                logger.debug(
                    "research observation: %s %s %s → obs_id=%d",
                    obs.experiment_id, obs.symbol, obs.direction, obs_id,
                )
            else:
                self._stats["duplicates"] = self._stats.get("duplicates", 0) + 1
        except Exception:
            # Fail-open: never propagate to scanner cycle
            self._stats["errors"] = self._stats.get("errors", 0) + 1
            logger.exception(
                "research observation failed: %s %s",
                candidate.scanner_name, candidate.symbol,
            )

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)
