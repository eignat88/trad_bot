# ME_R V3 crypto-only fixed OOS universe (Issue #154 / PR #159)
Status: OPERATOR CRYPTO-ONLY SCOPE APPROVED; EXACT MEMBERSHIP REQUIRES REVIEW.
Source snapshot: 2026-10-16T05:05:45.917305Z (candidate top-50 Bybit linear perpetuals).
Method: retain the existing relative rank of candidate instruments with crypto-related underlying; remove visibly equity/ETF/metal-related contracts. Do not backfill or refill to 50 dynamically.
Selected count: 34.
SHA256 of UTF-8 single-line CSV without newline: 19c5807e510e75ff1e19bdca8bdad469e05a9c2198266ab27821570425a28bf8

```text
BTCUSDT,ETHUSDT,SOLUSDT,ZECUSDT,NEARUSDT,XRPUSDT,HYPEUSDT,STRKUSDT,ENAUSDT,ZEREBROUSDT,DOGEUSDT,SUIUSDT,AKEUSDT,ADAUSDT,PUMPFUNUSDT,1000PEPEUSDT,WLDUSDT,ZROUSDT,ONDOUSDT,INJUSDT,UNIUSDT,WALUSDT,AVAXUSDT,LINKUSDT,ARBUSDT,TIAUSDT,PYTHUSDT,TAOUSDT,PUFFERUSDT,SIRENUSDT,AAVEUSDT,ZKUSDT,QNTUSDT,CRVUSDT
```

Excluded 16 candidate contracts (subject to manual category verification):
SOXLUSDT,KORUUSDT,SKHYNIXUSDT,XAUUSDT,SNDKUSDT,CLUSDT,SPCXUSDT,SKHYUSDT,MUUSDT,SOXSUSDT,MSTRUSDT,BTWUSDT,XAGUSDT,XPLUSDT,NVDAUSDT,XAUTUSDT

Not a direct Bybit instrument-metadata classification: ticker-based selection is provisional, especially ambiguous token names. No deployment/capture until exact list explicitly approved and instrument underlying classification verified.

## Validation checkpoint — 2026-10-16 05:12 UTC

VPS detached checkout `b38f100a`: regression `215 passed, 3 skipped`.
Operator natural Bybit read-only `--market-preflight` on exact manifest via `ME_R_V3_OOS_SYMBOLS`: 34/34 symbols PASS, no errors. All source closes `2026-10-16T05:10:00Z`; 199 completed 5m, 199 completed 15m and 199 completed 1h per symbol. BTCUSDT fetch completion `05:12:09.049091Z` (129.049s from close); CRVUSDT completion `05:12:31.664265Z` (151.664s from close). Sequential API completion span 22.615174s, max delay below 300s. One natural sampling cycle only; network retries, long-run continuity, and full evaluator runtime remain separate gates.

Crypto-only scope approved, exact ticker membership classification has not been verified from exchange underlying metadata for ambiguous names. No merge, deployment or activation authorization implied.
