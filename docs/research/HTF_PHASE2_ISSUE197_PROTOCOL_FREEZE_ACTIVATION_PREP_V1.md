# HTF Phase-2 — Issue #197 Protocol Freeze & Activation Preparation V1

**Parent:** #181. **Dependencies:** #186 (six operator approvals), #195 (migration 074/075), #196 (observer infrastructure).  
**Mode:** preparation / audit only. **Verdict:** `PREPARED_FOR_REVIEW / PHASE_2_NO_GO`. This document is **not** a freeze, GO, or run authorization.

## 1. Frozen candidate and identity

Experiment ID: `HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1` (SHORT, observe-only). Source: `app/research/htf_baseline_short_phase2_protocol_v1.py`.

| Field | Approved value (#186) |
|---|---|
| `geometry.proximity_ms` | `300000` |
| `geometry.price_bps` | `20` |
| `lifecycle.level_retirement_policy` | `LEVEL_ELIGIBILITY_LOSS_IS_NON_TERMINAL` |
| `maturity_gates.minimum_confirmations` | `30` |
| `maturity_gates.minimum_symbols` | `5` |
| `maturity_gates.minimum_active_days` | `14` |

Approved **candidate** fingerprint (not yet independently re-evaluated on deployed VPS at #197 execution):  
`39accac9ddddfd1495202cd708d7dc95b6c6b93ac79d819ff3d5a02beec60de1`.

The 0/10/20/30/40 bps sensitivity is a **predeclared diagnostic**, not an invitation to select a retrospective PnL optimum. Ambiguous prior anchors remain fail-closed. Never reuse Point-B's `2026-10-06T14:00:00Z` or an experiment ID.

## 2. Critical protocol/fingerprint mechanics

As currently implemented, `PROTOCOL_V1["status"] = PROTOCOL_STATUS` enters the canonical serialized JSON used by `protocol_fingerprint()`. Changing `PROTOCOL_STATUS` from `PROTOCOL_NOT_FROZEN` to `FROZEN` therefore changes the fingerprint. **Never use the candidate hash for an activated frozen protocol without recomputing the final hash.**

`APPROVED_FREEZE_TS` is a separate module constant, not a key of `PROTOCOL_V1`: a timestamp change should not alter that hash with the present serialization, but this must be proven by a deterministic test before approval. Treat any fingerprint drift as a hard stop.

Keep the three runtime authorities distinct:
1. Protocol constant `APPROVED_FREEZE_TS` in the **reviewed/deployed** frozen protocol commit.
2. DB `research.htf_baseline_short_phase2_experiment.freeze_ts` and `protocol_fingerprint` recorded via `research.htf_baseline_short_phase2_activate(...)`.
3. Private `/opt/trad_bot/etc/htf_phase2_activation.json` with identical experiment ID, freeze and fingerprint.

The DB freeze registry is a durable uniqueness guard, not a fourth freely editable timestamp. No writing to it outside the approved activation function.

## 3. Read-only preflight commands (VPS)

Run each command on `trad-bot-01`. Keep output, UTC clock, Git SHA, and a separate operator identity in the change log.

```bash
cd /opt/trad_bot
date -u '+%Y-%m-%dT%H:%M:%SZ'
git status --porcelain=v1
git rev-parse HEAD
git ls-remote origin refs/heads/main

PYTHONDONTWRITEBYTECODE=1 /opt/trad_bot/.venv/bin/python - <<'PY'
from app.research.htf_baseline_short_phase2_protocol_v1 import (
    PROTOCOL_STATUS, APPROVED_FREEZE_TS, protocol_fingerprint,
    operator_decisions_pending,
)
print("status", PROTOCOL_STATUS)
print("freeze_ts", APPROVED_FREEZE_TS)
print("pending", operator_decisions_pending())
print("fingerprint", protocol_fingerprint())
PY

systemctl is-enabled trad-bot-htf-baseline-short-observer || true
systemctl is-active trad-bot-htf-baseline-short-observer || true
test ! -e /opt/trad_bot/etc/htf_phase2_observer_enabled && echo FLAG_ABSENT
systemctl is-active trad-bot-scanner trad-bot-paper trad-bot-research-evaluator
```

DB registration read only, explicitly on production 5432:

```bash
sudo -u postgres psql -p 5432 -d trad_bot -X -v ON_ERROR_STOP=1 -P pager=off <<'SQL'
SELECT current_database(), current_setting('port'), now();
SELECT experiment_id, status, freeze_ts, protocol_fingerprint, observer_enabled
FROM research.htf_baseline_short_phase2_experiment
WHERE experiment_id = 'HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1';
SELECT count(*) AS observation_n
FROM research.htf_baseline_short_phase2_observation
WHERE experiment_id = 'HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1';
SELECT count(*) AS claimed_freezes
FROM research.htf_baseline_short_phase2_freeze_registry;
SQL
```

Expected *before* any activation: one experiment row `PENDING`, null freeze/fingerprint, `observer_enabled=false`, zero Phase-2 observations, systemd disabled/inactive, activation flag absent; nonzero freeze-registry count should be separately explained, never erased. Verify private env/password ownership and mode by `stat` only, **never print secrets**.

## 4. Future freeze and activation sequence — DESIGN, NOT EXECUTABLE AUTHORIZATION

**Gate F1 — freeze code change / PR (separate review):**
- Introduce explicit frozen protocol status in a versioned commit and prove six approved values unchanged, `operator_decisions_pending()==[]`, event identities and Point-B isolation unchanged.
- Recompute and pin **final frozen fingerprint** after the status change. Full Phase-2 regression, protocol serialization stability, checkpoint/restart, fail-closed tests and CI must pass.
- Identify who is authorized to set the *timestamp-bearing* protocol constant; never deploy a timestamp older than the DB acceptance boundary.

**Gate F2 — timestamp delivery design:**
- The current DB function accepts a freeze no earlier than server `now() - 60s` and no later than `now() + 5m`. A timestamp embedded in Git/Python requires code review, commit, deployment and DB activation within a tight time window. **Do not assume this is operationally feasible.**
- Before any production timestamp write, independently exercise a timed dry-run on the isolated PostgreSQL test cluster. Validate skew on VPS vs DB and Bybit server time and a complete atomically reviewed operator procedure.
- If deploying timestamp-bearing code in this window cannot be guaranteed, STOP and propose a separately reviewed amendment to timestamp authority/activation mechanism. Never silently relax guards, use a stale timestamp, reset state, or call DB activation first and assume protocol authority will eventually catch up.
- When a timestamp is finally authorized, record one *fresh* UTC value, exact code SHA, final protocol hash and operator approval, and verify exact three-source agreement; any mismatch is `NO_GO`.

**Gate F3 — DB activation (future separate GO):**
- Reconfirm #195 verified privileges/backups, #196 credentials and systemd inactive. Obtain explicit approval for DB activation and write of the private activation JSON.
- Use `research.htf_baseline_short_phase2_activate` **only once**, with the agreed fresh UTC timestamp and final hash. Check DB state afterward; do not treat a partially completed sequence as safe to retry with another timestamp/experiment.
- Do not set `observer_enabled=true`, create the systemd gate file, or start the unit as part of #197.

**Gate F4 — first startup (owned by #198):**
- Separate operator `GO_FOR_OBSERVE_ONLY_STARTUP` required. Recheck three sources, strict `signal_time > freeze_ts` and `first_touch_time > freeze_ts`, warmup exclusion, natural event uniqueness, Bybit coverage and active existing services.
- Only then authorize enablement **of the DB observer flag**, gate-file creation and `systemctl start`; **never** `systemctl enable` or route paper/live.

## 5. Partial failure / stop policy

- Failure before any DB activation: keep protocol not runnable; no activation flag/service start. Revert *unactivated* preparation PR if needed under normal review.
- Failure after DB activation but before observer startup: **do not rewind immutable freeze**, change experiment ID, overwrite DB freeze, or relabel old history as prospective. Keep unit inactive/flag absent, retain audit evidence; operators decide whether to recover the same exact frozen boundary or cancel and version a new experiment.
- Failure during startup: stop **only** Phase-2 observer, remove **only** its gate file and (after separate authorization) set `observer_enabled=false`. Preserve rows/checkpoint/registry. Other services must remain untouched.
- Any protocol/DB/activation JSON hash or timestamp disagreement = hard `NO_GO` with zero OOS writes.

## 6. Acceptance and current gaps

- [x] All six approved candidate values documented in #186.
- [x] Migration #195 and infrastructure #196 reported complete, independently verifiable by read-only preflight.
- [x] Three-authority freeze contract and rollback/stop paths documented.
- [ ] Current VPS preflight / fingerprint command output captured for #197.
- [ ] FROZEN-status semantic diff and recomputed final hash covered by dedicated tests/PR.
- [ ] Time-bounded timestamp delivery mechanism proven feasible in isolation and explicitly approved.
- [ ] Final independent review + operator decision before production freeze.
- [ ] #198 startup GO and natural post-freeze observations (out of #197 scope).

**Current decision: PREPARATION_IN_PROGRESS / NO_GO.** Do not close #181, alter `APPROVED_FREEZE_TS`, write DB freeze or start observer merely because this document was merged.
