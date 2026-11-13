# ME_LONG_V2_INVERSE_SHORT_V2 — isolated Scanner source mode

> Review-only change. No deployment, activation, backfill or live orders are performed by this change.

## Why
The main Scanner initializes generic/prospective research observers even on an otherwise clean V2-only host. Missing legacy `research.*` tables cause 42P01 and cascading 25P02 errors. Migration of the entire historical research stack is intentionally out of scope.

## Opt-in
Set these values in the Scanner `EnvironmentFile` at `/opt/trad_bot/.env`:

```ini
TRADING_MODE=paper
LIVE_TRADING_ENABLED=false
ME_INVERSE_SHORT_V1_ENABLED=0
ME_INVERSE_SHORT_V2_SOURCE_ONLY=1
ME_INVERSE_SHORT_V2_ENABLED=0
```

The V2 runtime has a separate `ME_INVERSE_SHORT_V2_ENABLED=0` setting before preflight. Leave both at 0 until an approved prospective activation after successful tests and preflight.

## Semantics
- Exactly one registered detector: `MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`.
- Original `_me_inverse_short_source_events` is published **before** production filtering; its V2 capture call site is unchanged and unique.
- Other scanner candidates are not saved as tradeable setups in source-only mode. Generic/prospective observers, early MAE, and embedded OOS evaluator are not initialized/launched.
- Legacy V1 delivery function remains for audit compatibility; the mode rejects startup if the V1 capture flag is ON. The V1 hook itself is a no-op while disabled.
- `dds.scanner_run` / `dds.scanner_run_stat` still record scanner cycles, detector candidate counts and failures. `setups_found=0` counts *ordinary persisted setups*, not V2 source events; the dedicated `source_events=N` log reports the latter.
- `ME_INVERSE_SHORT_V2_SOURCE_ONLY` absent/off means unchanged legacy Scanner behavior.
- V2 fingerprint, geometry, position management, and direction/expectancy filters are untouched.

## Verification / mandatory gates
1. Run tests as Linux user `tradbot`, not root (Git pinned baseline and `tests/.tmp` permissions).
2. Run existing V2 deployment/source-capture/byte-identity tests, including the five negative wiring controls; any failure blocks deployment.
3. Run `tests/test_me_inverse_short_v2_source_only.py` and inspect the diff.
4. Validate that Scanner's service EnvironmentFile has source-only=1, live trading OFF, V1 OFF and V2 activation OFF.
5. Perform controlled Scanner start with one detector; confirm no `research.*` errors and completed cycles.
6. Run V2 preflight and require 6/6 PASS before any activation action.
7. Activate V2 only under the separate frozen prospective procedure; **never** backfill earlier signals.

## Current limitations
The change has not yet been deployed or tested on the target VPS. Do **not** merge or activate based on this document alone.
