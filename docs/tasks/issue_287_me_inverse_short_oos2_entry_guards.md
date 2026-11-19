# Agent implementation task — Issue #287

Source of truth: https://github.com/eignat88/trad_bot/issues/287
References: #276 (existing OOS-2 frozen strategy), #285 (analogous V3 entry guards).
Dashboard: http://91.99.60.150:3000/d/me-inverse-short-oos-2/trad-bot-me-inverse-short-oos2

## Assignment
Implement `ME_INVERSE_SHORT_OOS2_ENTRY_GUARDS_AND_CAPACITY_V1` in this PR branch. The issue text is authoritative; **do not substitute V3 logic or turn independent OOS-2 capacity into shared V2/V3 capacity**. This branch initially contains only this development brief and **is not an implemented feature**.

**Operating mode:** code/tests/docs only. No production DB writes, migrations execution, deployment, service restarts, activation, freeze changes, drain, backfill, trading actions, PR merge, or live orders. Do not close #287 until tested and approved. Keep the PR as Draft and report the factual verdict.

## Mandatory baseline and design audit before editing
1. Record current git HEAD and inspect actual main OOS-2 code paths for event capture, activation/fingerprint, admission, positions, leg fills, funding/outbox/recovery, config, SQL migrations, systemd and Grafana. Identify exact files/schemas; do not assume V2 paths are OOS-2 paths.
2. Inspect existing tests/CI failures to establish baseline. If read-only VPS access is available, gather current deployment state and active OOS-2 OPEN positions/partial legs; clearly mark any unavailable facts **NOT VERIFIED**.
3. Inspect whether current OOS-2 has frozen activation and live OPEN positions; document how to preserve exits while implementing separate versioned admission protocol. Any changed admission semantics must have a new experiment/protocol version, new fingerprint and fresh prospective-only freeze at separately approved cutover, not mutating old protocol.
4. Write an audit note in the PR containing file map, schema names, potential conflicts, and migration/cutover risks.

## Required guards
- Capacity: maximum **12 OPEN logical positions in OOS-2 only**, counting partially closed TP1/TP2 positions; 13th fresh event follows existing pending→TTL semantics. V2/V3 must not count toward this limit.
- Symbol exclusivity: maximum **1 OPEN logical OOS-2 position per symbol** after safe legacy handover; partial TP is still OPEN. No cross-contour symbol block with V2/V3.
- Cooldown: **12h from last committed OOS-2 entered_at**, including CLOSED trades, not from close time; refused events/partial closes do not reset timer. Only a NEW eligible event at/after DB 12h boundary may enter; no replay of terminal blocked signal.
- Terminal refusal reasons: `SYMBOL_ALREADY_OPEN`, `SYMBOL_COOLDOWN_12H`; persist event_key, lineage, disposition and reason atomically, without position or entry fee. Keep `ENTRY_CAP` pending until TTL according to current contract.
- DB transaction admission must be race-safe among multiple workers, distinct event keys and ambiguous commits. Use deterministic lock ordering across event/activation/portfolio/expiration/close; recheck status/fingerprint/freeze/TTL/symbol/cap/cooldown **after acquiring locks using DB decision time**. Handle committed position adoption before classifying insert conflict; fail closed for new entries on state failures while exits proceed.
- Add schema-level uniqueness for new OOS-2 OPEN symbols if safely possible, never fail migration on historical duplicates; add targeted indexes and an idempotent additive migration. Preserve existing tables/rows and completed partial TP legs.

## Do not change
Source: independent `MOMENTUM_EXHAUSTION_REVERSE_LONG_V2` LONG candidate → inverse SHORT.
TP ladder: +5% favorable short closes **50% original qty**; +7% closes **75% of remaining after TP1 = 37.5% original**; +10% closes all remaining **12.5% original**.
SL: adverse short +10% price movement, stop `entry*1.10` for outstanding quantity; STOP_FIRST on ambiguous candles.
Max hold: 7200 minutes; signal TTL: 120 minutes.
Do not alter detector/scoring/gates, fees/slippage/funding/sizing, per-leg exactly-once, stop/time-out/close/recovery, historical balances, frozen fingerprints, V1/V2/V3 or production scanner/paper.

## Work packages
1. Audit & design note, versioned OOS-2 fork or equivalent isolated protocol/activation without modifying frozen runtime.
2. Explicit validated config defaults (`max_open_positions=12`, `single_active_symbol=true`, `symbol_entry_cooldown_hours=12`) included in **new** fingerprint; validate YAML/env precedence and unknown keys.
3. Durable PostgreSQL guard/admission, event dispositions, migration/indexes/lock discipline, in-memory test-store parity.
4. Continue partial TP/exits/funding and close outbox in EXIT_ONLY/DRAINING with no admissions. Legacy duplicate symbol rows survive until ordinary exits; no forced closures.
5. OOS-2 Grafana panels: logical OPEN X/12, by-symbol OPEN, cooldown/last committed entry, cap pending/TTL, rejection reason counts, TP stage and outstanding qty, activation/fingerprint/freeze, outbox/funding/recovery/health. No mixing with V2/V3 or counting partial legs as separate trades.
6. Read-only preflight and separate **approval-gated** cutover/rollback runbooks. Initial activation disabled, no backfill.

## Required tests (real PostgreSQL in addition to unit tests)
- Two concurrent distinct event keys same symbol (including ~9ms difference), different DB sessions -> exactly one committed OOS-2 entry and fee; no duplicate logical OPEN.
- 12 different symbols allowed; 13th pending until slot available or TTL; TP1/TP2 do not free capacity.
- Existing OPEN and partially closed position block same symbol. After full close, entry <12h terminal refused; >=12h **new eligible** event allowed; refusal does not reset timer.
- DB-time lock-wait TTL boundaries, inactive/mismatched protocol, pre-freeze, missing source/funding/price fail closed for entries, while exits still protected.
- Rollback, ambiguous/lost COMMIT acknowledgement, retries, duplicate events, worker restart, deadlock prevention, no expired-with-position, no double fee/partial-leg/funding.
- TP1→TP2→TP3 and TP1→STOP, STOP_FIRST, timeout, remaining quantity precision, realized PnL retention, funding on outstanding only.
- Legacy duplicate OPEN symbol coexistence and DRAINING; SQL migration repeatability; override precedence.
- Full unchanged regression tests for OOS-2 and V1/V2/V3.

## Agent delivery protocol
- Commit implementation to `feat/287-me-inverse-short-oos2-entry-guards`, push to origin; update the existing Draft PR linked to #287. **Do not create a competing PR.**
- PR summary must contain: files and migrations changed; design/locking proof; raw test commands/counts/results; known failures; baseline differences; OOS-2 protocol/fingerprint isolation; DB and VPS modifications (expected NONE); approval-gated deployment/rollback procedure.
- Attach evidence or write `NOT VERIFIED` for anything not checked; never claim PostgreSQL integration tests passed based only on mocks.
- Final verdict: `READY_FOR_PR_REVIEW` only if required tests and preflight are factually verified; otherwise `FIX_REQUIRED` with exact blockers.
- Leave Draft, do not merge/deploy/activate/drain. Closing #287 requires separate review.
