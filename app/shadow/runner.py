"""Shadow scanner runner for real-time signal collection."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.exchange.bybit_client import BybitClient
from app.scanners.atr_wick_rejection_short import AtrWickRejectionShortScanner, SCANNER_NAME
from app.scanners.context_builder import build_market_context
from app.shadow.repository import ShadowSignalRepository

logger = logging.getLogger(__name__)


class ShadowScannerRunner:
    """Runner for real-time shadow signal collection.

    This runner operates independently of the main scanner and paper trading.
    It collects signals for experimental analysis without affecting trading.
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

    def scan_symbol(self, symbol: str) -> int:
        """Scan a single symbol for shadow signals.

        Returns number of signals saved.
        """
        try:
            ctx = build_market_context(self.client, symbol, self.settings)
            signals = self.scanner.scan(ctx)

            saved = 0
            for candidate in signals:
                # Convert SetupCandidate back to WickRejectionSignal for storage
                # Extract signal data from candidate features
                features = candidate.features
                signal = self._candidate_to_signal(candidate, features)
                if signal and self.repo.save_signal(signal):
                    saved += 1

            return saved
        except Exception:
            logger.exception("Shadow scan failed for %s", symbol)
            return 0

    def _candidate_to_signal(
        self, candidate: Any, features: dict
    ) -> Any:
        """Convert a SetupCandidate back to WickRejectionSignal for storage."""
        from app.scanners.atr_wick_rejection_short import WickRejectionSignal

        return WickRejectionSignal(
            symbol=candidate.symbol,
            signal_time=candidate.detected_at,
            signal_price=candidate.reference_price,
            open=features.get("open", 0),
            high=features.get("high", 0),
            low=features.get("low", 0),
            close=features.get("close", 0),
            volume=features.get("volume", 0),
            atr=features.get("atr", 0),
            atr_pct=features.get("atr_pct", 0),
            wick_size=features.get("wick_size", 0),
            wick_atr=features.get("wick_atr", 0),
            upper_wick_pct=features.get("upper_wick_pct", 0),
            close_location=features.get("close_location", 0),
            rsi=features.get("rsi", 0),
            stoch_rsi=features.get("stoch_rsi"),
            bb_upper=features.get("bb_upper", 0),
            bb_mid=features.get("bb_mid", 0),
            bb_lower=features.get("bb_lower", 0),
            bb_width=features.get("bb_width", 0),
            distance_to_upper_bb=features.get("distance_to_upper_bb", 0),
            ema_fast=features.get("ema_fast", 0),
            ema_medium=features.get("ema_medium", 0),
            ema_slow=features.get("ema_slow", 0),
            ema_slope=features.get("ema_slope", 0),
            volume_ratio=features.get("volume_ratio", 0),
            signal_version=features.get("signal_version", "1.0.0"),
        )

    def scan_universe(self, symbols: list[str]) -> dict[str, int]:
        """Scan all symbols in the universe.

        Returns dict of {symbol: signals_saved}.
        """
        results: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=self.settings.scanner_workers) as executor:
            future_to_symbol = {
                executor.submit(self.scan_symbol, symbol): symbol
                for symbol in symbols
            }
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    results[symbol] = future.result()
                except Exception:
                    logger.exception("Shadow scan failed for %s", symbol)
                    results[symbol] = 0

        total = sum(results.values())
        logger.info(
            "Shadow scan complete: %d signals across %d symbols",
            total, len(symbols),
        )
        return results

    def run_cycle(self) -> dict[str, Any]:
        """Run one complete scan cycle.

        Returns summary of the scan.
        """
        start_time = datetime.now(timezone.utc)

        # Get universe of symbols
        symbols = self._get_universe_symbols()

        # Scan all symbols
        results = self.scan_universe(symbols)

        # Calculate summary
        total_signals = sum(results.values())
        symbols_with_signals = sum(1 for v in results.values() if v > 0)

        summary = {
            "start_time": start_time.isoformat(),
            "end_time": datetime.now(timezone.utc).isoformat(),
            "symbols_scanned": len(symbols),
            "symbols_with_signals": symbols_with_signals,
            "total_signals": total_signals,
            "results": results,
        }

        logger.info(
            "Shadow scan cycle complete: %d symbols scanned, %d signals found",
            len(symbols), total_signals,
        )

        return summary

    def _get_universe_symbols(self) -> list[str]:
        """Get list of symbols to scan from universe configuration."""
        # Use configured symbols if available
        if self.settings.symbols:
            return list(self.settings.symbols)

        # Otherwise, get top symbols from exchange
        try:
            # For now, use a default list of popular symbols
            default_symbols = [
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
                "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT",
                "UNIUSDT", "ATOMUSDT", "NEARUSDT", "FTMUSDT", "ALGOUSDT",
                "HBARUSDT", "VETUSDT", "ICPUSDT", "FILUSDT", "AAVEUSDT",
            ]
            return default_symbols[:self.settings.scanner_universe.top_n]
        except Exception:
            logger.exception("Failed to get universe symbols")
            return ["BTCUSDT", "ETHUSDT"]