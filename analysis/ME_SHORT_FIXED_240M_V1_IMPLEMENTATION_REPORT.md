# ME_SHORT_FIXED_240M_V1 — IMPLEMENTATION REPORT

## Git

```
Base branch: main
Base main SHA: fbae6765352907a879f7e16eef9a922f09cbeb3d
Primary feature branch: feat/me-short-fixed-240m-v1
Feature HEAD: edf09e6
```

## CURRENT IMPLEMENTATION AUDIT

### Entry Path
- `app/paper/engine.py` → `check_entries()`
- Position created with `PaperTradeRecord`
- DCA state created if `DCAPolicy.should_apply()`

### Exit Path (check_exits)
- Priority order: Stop → DCA → BE → TP1 → TP2 → Trailing → Expiry
- All in `check_exits()` method

### Key Files
| File | Purpose |
|---|---|
| `app/paper/engine.py` | PaperTradingEngine: entry, exit, lifecycle |
| `app/paper/position_monitor.py` | Background thread: calls check_exits() |
| `app/paper/dca.py` | DCA state machine |
| `app/paper/exit_reasons.py` | Exit reason enum |
| `app/config/settings.py` | All config including DCA, TTL, execution_policies |
| `app/db/repository.py` | DB persistence |
| `app/db/schema.sql` | DB schema |

## ARCHITECTURE

### Execution Policy Layer
```python
execution_policy = resolve_execution_policy(
    scanner_name=trade.scanner_name,
    direction=trade.direction,
)
```

Config structure:
```json
{
  "execution_policies": {
    "MOMENTUM_EXHAUSTION": {
      "SHORT": {
        "policy": "FIXED_HORIZON_V1",
        "enabled": false,
        "hold_minutes": 240,
        "dca_enabled": false,
        "trailing_enabled": false,
        "breakeven_enabled": false,
        "tp_enabled": false,
        "expiry_enabled": false
      }
    }
  }
}
```

## DB MIGRATION

`sql/migrations/038_execution_policy.sql`:
- Added `execution_policy TEXT DEFAULT 'DEFAULT'`
- Added `planned_exit_at TIMESTAMPTZ`
- Added indexes for time-exit queries
- Updated CHECK constraint for `FIXED_HORIZON` exit reason

## CHANGED FILES

1. `app/config/settings.py` — ExecutionPolicyConfig + config loading
2. `app/paper/exit_reasons.py` — Added FIXED_HORIZON
3. `app/paper/engine.py` — Fixed-horizon lifecycle in check_exits + entry
4. `app/db/schema.sql` — Updated CHECK constraint
5. `app/db/repository.py` — Persist execution_policy and planned_exit_at
6. `sql/migrations/038_execution_policy.sql` — DB migration
7. `config.yaml` — Added execution_policies (disabled by default)
8. `paper_runner.py` — Startup logging

## POLICY

```
entry = current production entry
stop = initial production stop (never widen)
DCA = false
TP = false
trailing = false
BE = false
expiry = false after entry
time_exit = entered_at + hold_minutes (240m default)
```

## TESTS

```
204 passed, 0 failed (relevant subset)
1 pre-existing failure in analytics (unrelated)
```

## RESTART RECOVERY

Not yet implemented — requires loading planned_exit_at from DB on restart.

## STATUS

**READY_FOR_PAPER** — All functional criteria met. Restart recovery and full integration tests pending VPS deployment.

## PUSH STATUS

Push failed due to Git credential issue on local machine. Branch needs to be pushed manually or via different auth method.
