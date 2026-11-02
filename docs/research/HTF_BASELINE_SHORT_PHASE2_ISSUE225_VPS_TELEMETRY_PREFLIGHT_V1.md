# Issue #225 — Read-only VPS API Telemetry Preflight (V1)

Purpose: produce the **measured** Bybit API budget evidence that #225 requires
(`GLOBAL_API_BUDGET_PENDING` → `API_BUDGET_CLEARED`/`NOT_CLEARED`) before any
V2 discussion. This document is an operator-executed, **read-only** preflight
plan. It does not start services, does not modify any config, and does not
touch production state.

> **HOLD gates: `DO NOT MERGE` / `API_BUDGET_UNVERIFIED` / `V2_NO_GO`.**
> Nothing here authorizes V2 freeze, activation, deploy or observer start.

## 0. Read-only guardrails (mandatory)

* Allowed: `journalctl` reads, `systemctl status`/`list-units` (read-only),
  log greps, at most a handful of bounded public market-data GETs to capture
  rate-limit headers.
* Forbidden: `systemctl start/stop/restart/enable`, config or unit edits,
  deployments, DB writes, restarting the observer/scanner/paper, downloading
  unbounded history, sustained load tests, any write to `/etc`, `/opt`, VPS
  service directories.
* Sampling must be minimal: the exchange warns that excessive frequency leads
  to a 403 "access too frequent" **~10-minute IP ban** (see §4).

## 1. Baseline totals per service (fixed window)

Take a fixed window, e.g. `T0-24h .. T0` (plus the same metrics for the peak
hour). Services: scanner, paper, research evaluator, HTF Phase-2 BTC/ETH
observer (V1). Substitute the actual systemd unit names:

```
journalctl --since "<T0-24h>" --until "<T0>" -u <unit> --no-pager \
  | grep -c "BYBIT_API_CALL"
journalctl --since "<T0-24h>" --until "<T0>" -u <unit> --no-pager \
  | grep -Eic "RATE_LIMIT|10006|too many visits|access too frequent|429"
journalctl --since "<T0-24h>" --until "<T0>" -u <unit> --no-pager \
  | grep -Eic "retry|retrying"
```

Record per service: total calls, rate-limit responses, retry warnings. Sum for
the whole-IP baseline. Reference point from the 2026-10-29 60-minute snapshot
(#225 comment): scanner 3027 / evaluator 2730 / HTF observer 388 / paper 0 =
**6145 calls, 3 rate-limit responses, 3 retry warnings**.

## 2. Peak request flow

From the same logs, bucket timestamps per minute (1-minute histogram):

```
journalctl --since "<T0-1h>" --until "<T0>" -u <unit> --no-pager \
  | grep "BYBIT_API_CALL" | awk '{print $1" "$2}' | cut -c1-16 | sort | uniq -c
```

Record: p50/p95/max calls-per-minute per service and in total (all services
share the same egress IP). This is the figure V2 load must fit under.

## 3. Per-IP / per-endpoint headroom (bounded sample)

* Documented limits (external source, verify at execution time):
  * HTTP IP limit: **600 requests per 5-second window per IP** for
    `api.bybit.com` (Bybit V5 rate-limit docs);
    violation → 403 "access too frequent" → automatic ~10-minute ban.
  * Per-endpoint API limits (per second per UID) are reported in response
    headers: `X-Bapi-Limit`, `X-Bapi-Limit-Status`,
    `X-Bapi-Limit-Reset-Timestamp`.
* Capture the headers from at most 2–3 single public GETs (e.g. one kline
  request per endpoint family actually used) and record `X-Bapi-Limit-Status`
  as the **observed** per-endpoint headroom. Do NOT probe repeatedly.
* Compute: observed peak rpm (§2) vs the 600/5s IP ceiling, and per-endpoint
  status vs the running cadence.

## 4. V2 additional-load forecast (SYNTHETIC — evidence-limited)

From #226 deterministic tests (fakes, not the live exchange):

| Scenario | Synthetic measurement | Origin |
|---|---|---|
| steady 5m cycle, 5 symbols | 2 req/symbol (1 time + 1 page) ≈ **120 req/h** | `tests/test_htf_baseline_short_phase2_issue226_api_budget_v1.py` |
| cold enrollment | 16 req/symbol (1 time + 15 pages) | same |
| restart (cache reuse) | 1 req/symbol | same |
| recovery burst (250 bars) | 3 req/symbol, ≤ 79/load bound | same |
| uncached comparison | ~1200 req/h (900 kline + 300 time) | #225 estimate |

**`120 req/h` is NOT proof of a safe total budget.** It is a deterministic
fakes-only measurement of the cache layer in isolation. The total budget must
be cleared by the measured baseline + headroom above, not by this number.

## 5. Hypotheses / measured / unknowns (explicit)

**Measured (deterministic, synthetic fakes):** per-phase request counts of the
cache layer (§4 table); fail-closed bounds (79 requests/load, 16 gap-recovery
requests, 3 transport attempts/page); SHA-256 integrity and lock semantics.

**Hypotheses to confirm on the VPS:**
1. One log `BYBIT_API_CALL` line corresponds to one HTTP request (the #225
   snapshot noted `page_requests=0` vs `pages_fetched=15` — instrumentation
   may undercount; validate the counter semantics before trusting totals).
2. Per-IP limit remains 600 req/5s at execution time (docs may change).
3. V1 observer cadence and the other services' load stay constant during the
   measurement window.
4. No hidden API consumers (cron jobs, manual ops, monitoring probes) beyond
   the four known services.

**Unknowns:**
1. Actual per-IP headroom under *concurrent* service load (limits are shared).
2. Behaviour of combined bursts (V1 observer recovery + V2 cold enrollment
   coinciding) against the 429/10006 path.
3. Rate-limit counters are per-UID per-second for API limits while the IP
   limit is shared — interaction under multi-service use is unmeasured.
4. Exchange-side limit changes, VIP tiers, or per-endpoint throttling policy.
5. Timeout-driven transport retries (not visible as rate-limit events).

## 6. Verdict template

`API_BUDGET_PREFLIGHT_PASS | FAIL | INCONCLUSIVE` with: window, per-service
totals, rpm histogram, observed `X-Bapi-Limit*` values, computed headroom vs
600/5s, the §4 forecast labelled synthetic, and the §5 unknowns list. Until
this is filled in from real measurements, the standing status stays
**`API_BUDGET_UNVERIFIED`** and V2 stays **NO_GO**.
