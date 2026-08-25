from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from app.scanners.models import SetupCandidate, SetupState


@dataclass(frozen=True)
class AggregationResult:
    """Resolution of all recent raw signals for one instrument."""

    symbol: str
    status: SetupState
    long_score: float
    short_score: float
    signals: tuple[SetupCandidate, ...]

    @property
    def trade_candidates(self) -> tuple[SetupCandidate, ...]:
        if self.status != SetupState.READY_TO_TRADE:
            return ()
        return tuple(
            signal for signal in self.signals
            if signal.state == SetupState.READY_TO_TRADE
        )


class SignalAggregator:
    """Resolve cross-scanner direction conflicts in a rolling time window.

    Scanners remain independent producers of DETECTED signals.  A symbol is
    eligible for trading only when every signal still in the window agrees on
    direction.  Any LONG/SHORT mixture blocks the complete symbol.
    """

    def __init__(self, conflict_window_seconds: int = 600) -> None:
        if conflict_window_seconds <= 0:
            raise ValueError("conflict_window_seconds must be positive")
        self.window = timedelta(seconds=conflict_window_seconds)
        self._signals: dict[str, SetupCandidate] = {}

    def resolve(
        self,
        candidates: list[SetupCandidate],
        now: datetime | None = None,
    ) -> list[AggregationResult]:
        evaluated_at = now or datetime.now(timezone.utc)
        cutoff = evaluated_at - self.window
        self._signals = {
            key: signal for key, signal in self._signals.items()
            if signal.detected_at >= cutoff
        }
        for candidate in candidates:
            # setup_id is stable when repository upserts a signal; fingerprint
            # deliberately keeps distinct scanners and directions independent.
            self._signals[candidate.fingerprint] = replace(
                candidate, state=SetupState.DETECTED
            )

        symbols = {candidate.symbol for candidate in candidates}
        results: list[AggregationResult] = []
        for symbol in sorted(symbols):
            recent = [
                signal for signal in self._signals.values()
                if signal.symbol == symbol
            ]
            has_long = any(signal.direction == "LONG" for signal in recent)
            has_short = any(signal.direction == "SHORT" for signal in recent)
            status = (
                SetupState.CONFLICT
                if has_long and has_short
                else SetupState.READY_TO_TRADE
            )
            resolved = tuple(replace(signal, state=status) for signal in recent)
            for signal in resolved:
                self._signals[signal.fingerprint] = signal
            results.append(AggregationResult(
                symbol=symbol,
                status=status,
                long_score=sum(s.score for s in resolved if s.direction == "LONG"),
                short_score=sum(s.score for s in resolved if s.direction == "SHORT"),
                signals=resolved,
            ))
        return results
