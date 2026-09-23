"""Backfill runner for historical shadow signal collection."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner
from app.shadow.repository import ShadowSignalRepository

logger = logging.getLogger(__name__)


class ShadowBackfillRunner:
    """Backfill historical shadow signals from existing 5m candles.

    This runner processes historical candle data to collect signals
    that occurred before the shadow scanner was started.
    """

    def __init__(
        self,
        settings: Settings,
        client: BybitClient,
        repo: ShadowSignalRepository,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repo = repo
        self.scanner = AtrWickRejectionShortScanner()

    def backfill_symbol(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime | None = None,
    ) -> int:
        """Backfill shadow signals for a symbol over a time range.

        Args:
            symbol: Trading pair symbol
            start_time: Start of backfill period
            end_time: End of backfill period (default: now)

        Returns:
            Number of signals saved
        """
        if end_time is None:
            end_time = datetime.now(timezone.utc)

        try:
            # Get historical 5m candles
            candles = self._get_historical_candles(symbol, start_time, end_time)
            if not candles:
                logger.warning("No candles found for %s backfill", symbol)
                return 0

            # Process candles in sequence
            saved = 0
            for i in range(50, len(candles)):  # Need 50 candles for indicators
                window = candles[i-49:i+1]
                ctx = self._create_context(symbol, window, candles[i].timestamp)

                # Detect signal
                signal = self.scanner.detect_signal(ctx)
                if signal:
                    # Save to database
                    if self.repo.save_signal(signal):
                        saved += 1

            logger.info(
                "Backfill complete for %s: %d signals from %d candles",
                symbol, saved, len(candles),
            )
            return saved

        except Exception:
            logger.exception("Backfill failed for %s", symbol)
            return 0

    def backfill_universe(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        symbols: list[str] | None = None,
    ) -> dict[str, int]:
        """Backfill shadow signals for all symbols in universe.

        Args:
            start_time: Start of backfill period
            end_time: End of backfill period (default: now)
            symbols: Optional list of symbols (default: universe)

        Returns:
            Dict of {symbol: signals_saved}
        """
        if symbols is None:
            symbols = self._get_universe_symbols()

        results: dict[str, int] = {}
        for symbol in symbols:
            results[symbol] = self.backfill_symbol(symbol, start_time, end_time)

        total = sum(results.values())
        logger.info(
            "Universe backfill complete: %d signals across %d symbols",
            total, len(symbols),
        )
        return results

    def _get_historical_candles(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        """Get historical 5m candles for backfill."""
        try:
            # Calculate number of candles needed
            time_diff = (end_time - start_time).total_seconds()
            num_candles = int(time_diff / 300) + 100  # 5m = 300 seconds

            # Get candles from exchange
            candles = self.client.get_klines(symbol, "5", min(num_candles, 1000))

            # Filter to time range
            start_ts = int(start_time.timestamp() * 1000)
            end_ts = int(end_time.timestamp() * 1000)

            return [
                c for c in candles
                if start_ts <= c.timestamp <= end_ts
            ]

        except Exception:
            logger.exception("Failed to get historical candles for %s", symbol)
            return []

    def _create_context(
        self,
        symbol: str,
        candles: list[Candle],
        timestamp: int,
    ) -> Any:
        """Create a MarketContext from historical candles."""
        from app.scanners.context_builder import _build_indicators, _classify_market_regime
        from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels
        from datetime import datetime, timezone

        # Build indicators from the candle window
        indicators = _build_indicators(candles)

        # Classify market regime
        market_regime = _classify_market_regime(indicators, candles[-1].close)

        # Create context
        return MarketContext(
            symbol=symbol,
            candles_5m=tuple(candles),
            candles_15m=tuple(candles),  # Use same candles for simplicity
            candles_1h=tuple(candles),
            candles_4h=tuple(candles),
            indicators=indicators,
            market_regime=market_regime,
            levels=MarketLevels(),
            evaluated_at=datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc),
        )

    def _get_universe_symbols(self) -> list[str]:
        """Get list of symbols from universe configuration."""
        if self.settings.symbols:
            return list(self.settings.symbols)

        # Default universe
        return [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
            "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT",
            "UNIUSDT", "ATOMUSDT", "NEARUSDT", "FTMUSDT", "ALGOUSDT",
            "HBARUSDT", "VETUSDT", "ICPUSDT", "FILUSDT", "AAVEUSDT",
        ]