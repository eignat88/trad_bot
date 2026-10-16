"""Read-only deterministic candidate universe snapshot for Issue #154.

Never imports the isolated OOS runner, never writes DB, never enables capture.
Candidate is NOT operator-approved until exact file is reviewed and committed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.config import Settings
from app.exchange.bybit_client import BybitClient

LIMIT = 50
MIN_TURNOVER_USDT_24H = 10_000_000


def main() -> None:
    instruments = BybitClient(Settings()).get_liquid_instruments(
        top_n=LIMIT,
        min_turnover_24h=MIN_TURNOVER_USDT_24H,
        quote_coin="USDT",
    )
    symbols = [row["symbol"] for row in instruments]
    if len(symbols) != LIMIT or len(symbols) != len(set(symbols)):
        raise SystemExit("CANDIDATE_UNIVERSE_NOT_COMPLETE")
    if any(not s.endswith("USDT") for s in symbols):
        raise SystemExit("CANDIDATE_UNIVERSE_NOT_USDT")
    canonical = ",".join(symbols)
    print(json.dumps({
        "status": "CANDIDATE_NOT_APPROVED",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "selection": "Bybit active linear perpetual USDT, 24h turnover >= 10M, top 50",
        "ordering": "descending turnover_24h, then symbol ascending",
        "symbol_count": len(symbols),
        "symbols": symbols,
        "symbol_csv": canonical,
        "sha256_symbol_csv": hashlib.sha256(canonical.encode()).hexdigest(),
        "ranked_snapshot": instruments,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
