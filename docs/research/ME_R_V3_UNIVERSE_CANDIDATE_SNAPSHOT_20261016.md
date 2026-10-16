# ME_R V3 OOS candidate universe — snapshot 2026-10-16

**State: CANDIDATE ONLY — NOT APPROVED, NOT ALLOWED FOR RUNTIME CAPTURE.**

Source: operator-provided stdout of `tools/research/me_r_v3_universe_candidate_issue154.py`, executed on VPS at `2026-10-16T05:05:45.917305+00:00`.
Selection: Bybit active linear perpetual, quote USDT, 24h turnover >=10M USDT; top 50 ordered descending 24h turnover, ascending symbol to resolve ties.

Count: **50** (50 distinct).
SHA256 of the exact comma-joined CSV (UTF-8, no terminal newline):
`72b61dca61133367fe597c4d0deeb72d3accd1f21e2f38d7584f140f5ca46b9d`

```text
BTCUSDT,ETHUSDT,SOLUSDT,ZECUSDT,SOXLUSDT,NEARUSDT,XRPUSDT,HYPEUSDT,STRKUSDT,KORUUSDT,SKHYNIXUSDT,XAUUSDT,SNDKUSDT,ENAUSDT,ZEREBROUSDT,DOGEUSDT,CLUSDT,SPCXUSDT,SUIUSDT,AKEUSDT,ADAUSDT,PUMPFUNUSDT,1000PEPEUSDT,WLDUSDT,SKHYUSDT,ZROUSDT,MUUSDT,SOXSUSDT,ONDOUSDT,INJUSDT,UNIUSDT,WALUSDT,MSTRUSDT,AVAXUSDT,LINKUSDT,BTWUSDT,XAGUSDT,ARBUSDT,XPLUSDT,TIAUSDT,PYTHUSDT,TAOUSDT,PUFFERUSDT,SIRENUSDT,AAVEUSDT,ZKUSDT,QNTUSDT,NVDAUSDT,CRVUSDT,XAUTUSDT
```

**Review blocker:** this API category contains nontraditional underlyings and tickers potentially referencing equities/ETFs/metals (e.g. SOXLUSDT, SOXSUSDT, NVDAUSDT, SKHYNIXUSDT, XAUUSDT, XAGUSDT, XAUTUSDT). Their inclusion must be assessed against the intended V1 source scanner market assumptions and continuity of 5m/15m/1h data. This is not proof they are crypto instruments. No silent exclusion or substitution: changing the selection rule requires an explicit updated manifest and approval.

**Required before approval:**
1. Decide eligible underlying universe (crypto-only vs all Bybit linear perpetuals) and document exclusions/market assumptions.
2. Run `--market-preflight` on all approved symbols; record individual failures and wall-clock duration. A result from only BTCUSDT/ETHUSDT cannot justify 50-symbol operation.
3. Ensure timer offsets/cycle length do not systematically breach maximum 300s source delay or cause missing OOS observations.
4. Approve exact ordered symbol list and hash explicitly; configure runtime only after separate deployment/activation authorizations.

Original state remains `CANDIDATE_NOT_APPROVED`. Freeze timestamp 2026-10-16T12:00:00Z and activation cutoff 2026-10-19T12:00:00Z do **not** authorize a service start.

## Full 50-symbol natural market preflight — 2026-10-16

Operator-provided VPS stdout after checking out commit `6e6eeafc`.
Command: `--market-preflight` on all 50 symbols from this snapshot, running
as dedicated OS user `trad_bot_me_r_v3_oos`, no DB connection or capture.

- Results returned for **all 50/50** symbols; zero reported failures.
- All source-close timestamps: `2026-10-16T05:05:00Z`.
- All: 199 validated closed 5m, 199 closed 15m, 199 closed 1h candles.
- Earliest fetch completion BTCUSDT: `05:07:37.543720Z`, 157.544s lag.
- Latest fetch completion XAUTUSDT: `05:08:08.580044Z`, 188.580s lag.
- Observed first-to-last API completion span: 31.036324s.
- Max 300s lag gate: PASS for this single observed sampling cycle.

**Scope limitation:** this is one read-only observed sequence. It does not
measure 50-symbol live evaluator completion, tail retry/backoff latency,
API failures or repeated-cycle reliability. Market/underlying eligibility
has not been independently approved. The snapshot remains
`CANDIDATE_NOT_APPROVED`; no merger, migration to the clean OOS database,
service start, timer activation or historical ingestion is authorized.
