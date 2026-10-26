# HTF Phase-2 — Issue #197 FROZEN Protocol & Timestamp Authority V1

**Parent:** #181. **Working issue:** #197 (Protocol Freeze & Activation Prep).
**Predecessors:** #186 (six operator approvals), #195 (migrations 074/075),
#196 (observer infrastructure), PR #199 (draft prep document).
**Mode:** development + isolated tests only.
**Verdict:** `FROZEN_PROTOCOL_IMPLEMENTATION_REVIEW_READY / PHASE_2_NO_GO`.
This document is **not** a freeze GO, not an activation and not a run
authorization. It supersedes the DESIGN-only sections of the PR #199 draft
where they conflict with the timestamp-authority mechanism below.

---

## 1. Audit findings (current architecture at `c17b2a6`)

### 1.1 Where the protocol freeze lives

`app/research/htf_baseline_short_phase2_protocol_v1.py`:

* `PROTOCOL_STATUS = "PROTOCOL_NOT_FROZEN"` is serialized into
  `PROTOCOL_V1["status"]`, therefore **the status is part of the fingerprint**;
* `APPROVED_FREEZE_TS: str | None = None` is a module-level constant OUTSIDE
  the canonical payload — a timestamp change does not move the fingerprint,
  but the constant is still a Git-tracked value;
* `operator_decisions_pending()` walks `PROTOCOL_V1` for
  `OPERATOR_APPROVAL_REQUIRED` markers; at `c17b2a6` it returns `[]`
  (all six fields `OPERATOR_APPROVED`).

### 1.2 Fingerprint mechanics

`protocol_fingerprint()` = SHA-256 over `json.dumps(PROTOCOL_V1,
sort_keys=True, separators=(",", ":"), ensure_ascii=True)`. Independently
reproduced during this audit on the pristine worktree:

| Payload | SHA-256 |
|---|---|
| reviewed candidate (`status = PROTOCOL_NOT_FROZEN`) | `39accac9ddddfd1495202cd708d7dc95b6c6b93ac79d819ff3d5a02beec60de1` (matches the published candidate) |
| in-memory `status = FROZEN`, nothing else changed | `22fa0c08eed92df7bb6e94576987305343a71083f3129f3ce4acd07c287cb3f0` (matches the proposed frozen value) |

### 1.3 The three freeze authorities and how the runner compares them

`require_runnable()` (`observer_runner_v1.py`) compares:

1. protocol authority — `approved_freeze_ts()` (or an injected test value);
2. database authority — `research.htf_baseline_short_phase2_experiment`
   (`freeze_ts`, `protocol_fingerprint`) written once by
   `research.htf_baseline_short_phase2_activate()`;
3. runtime authority — the operator activation JSON (`--activation-config`).

`Phase2FreezeBoundary.create()` requires all three timestamps to be present
and **exactly equal** (millisecond level) and every fingerprint to equal the
fingerprint of the running code. Missing/mismatched sources raise and the
CLI exits `3` (EXIT_NOT_RUNNABLE) before any observation exists.

### 1.4 PostgreSQL activation

`research.htf_baseline_short_phase2_activate(experiment_id, freeze_ts,
fingerprint)` (migration 074, hardened by 075):

* window `now()-60s <= freeze_ts <= now()+5m` (`P2_FREEZE_TS_IN_PAST`,
  `P2_FREEZE_TS_MUST_NOT_BE_FAR_FUTURE`);
* `freeze_registry(freeze_ts PRIMARY KEY)` makes any re-used timestamp
  structurally impossible (`P2_FREEZE_TS_ALREADY_CLAIMED`);
* `freeze_ts` / `protocol_fingerprint` are set-once (`P2_FREEZE_TS_SET_ONCE`,
  `P2_PROTOCOL_FINGERPRINT_SET_ONCE`), status transitions are guarded, the
  registration must start `PENDING / NULL / observer_enabled=false`;
* `FOR UPDATE` on the experiment row serializes concurrent activations;
* observation inserts enforce strict `signal_time > freeze_ts` AND
  `first_touch_time > freeze_ts`, reject `warmup_origin`/`resumed_pre_freeze`
  at BOTH the SQL function and the raw-INSERT trigger, and rows are immutable;
* migration 075 turns the write/activation functions into a `SECURITY DEFINER`
  wall and grants `activate()` **only** to `trad_bot_htf_p2_admin` — the
  observer role cannot activate.

### 1.5 Identified problems

* **P-TS-1 (core, this issue):** an `APPROVED_FREEZE_TS` embedded in the
  Python module must survive review → merge → CI → VPS deployment inside the
  60s-past/5m-future DB window. That is operationally infeasible: either the
  activation fails (`P2_FREEZE_TS_IN_PAST`) or the timestamp gets refreshed
  outside its own review (unsound), or operators feel pressure to backdate.
  This is exactly the failure mode the freeze boundary exists to prevent.
* **P-TS-2:** the "protocol authority" carried no *binding* between the chosen
  timestamp and the reviewed fingerprint — agreement was runtime-only.
* **P-TS-3:** `require_runnable` did not check `status = RUNNING` explicitly
  (it relied on `freeze_ts IS NULL` while PENDING) and had no single
  agreement path over all four applicable values (record/DB/activation/code).
* **P-TS-4:** no artifact existed that could be approved ONCE and verified
  LATER byte-for-byte (the activation JSON is written at activation time and
  is mutable until the runner reads it).
* **Pre-existing, unrelated to this issue:** on current `main`,
  `tests/test_htf_baseline_short_phase2_isolation_v1.py::test_frozen_point_b_stack_byte_identical_to_base`
  fails because PR #193 legitimately modified `app/research/prospective_observer.py`
  after the test's hard-coded `BASE_SHA` (`4ef60d1e`). This failure exists on
  `origin/main` itself and is reported, not masked, by this task.
  **Resolved as a documented historical exception (PR #200 FIX-5):** `BASE_SHA`
  stays `4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba` and the other seven frozen
  artifacts remain strictly byte-identical; for `prospective_observer.py` the
  isolation test tolerates EXCLUSIVELY the single one-line replacement
  introduced by commit `41536cc5bc967331846d045eaae18a0702da8b89` ("scoped ME
  V3 draining lifecycle", PR #193 — ME V3 capture is RUNNING-only while
  Point-B keeps the original `_active_status()` branch semantics). The exact
  source and resulting fragments, the delta of `41536cc5` itself and the
  expected content hash are all verified with that provenance; ANY other change
  to the protected file (including a revert) fails the test, and negative
  tests keep Point-B-dependent logic changes detectable. See the module
  docstring of `tests/test_htf_baseline_short_phase2_isolation_v1.py`.

### 1.6 Non-atomic / partial-activation states found

1. `activate()` commits the registry insert + experiment update atomically,
   but the *operator sequence* spans three artifacts (record, DB, activation
   JSON): a crash between DB claim and JSON write leaves the DB `RUNNING`
   with no runnable observer (fail-closed) but needs a defined repair;
2. two concurrent operator attempts could race before `FOR UPDATE` serializes
   them — one wins, the loser must NOT retry with a new timestamp;
3. a minted record that is never claimed is inert (no DB effect) but must not
   be claimable after its freshness window.

---

## 2. Design decision — timestamp authority V1 (freeze record)

**Decision: the FROZEN protocol module carries NO timestamp; the fresh UTC
freeze timestamp is chosen at the separate operator GO and materialized as an
immutable, self-hashed FREEZE RECORD that binds
`(experiment_id, protocol_fingerprint, freeze_ts, claimed_at, approval ref)`.**

Rationale against the alternatives:

| Alternative | Why rejected |
|---|---|
| keep `APPROVED_FREEZE_TS` in Git (status quo) | cannot fit review/merge/CI/deploy latency into the 60s/5m window; stale-timestamp pressure (P-TS-1) |
| relax the DB window (e.g. accept far-future timestamps) | weakens the freeze boundary for every future experiment; explicitly forbidden by the task and by `freeze_boundary.mismatch_or_missing_freeze = REJECT_FAIL_CLOSED` |
| auto-generate the timestamp in the runner at startup | the runner would own its own freeze; prohibited (a runtime must never choose the boundary that governs its own observations) |
| reuse the DB `freeze_registry` row as the only authority | collapses three authorities into one mutable-at-claim source; loses independent detection of a bad claim |
| **chosen: operator-approved freeze record** | timestamp is chosen at GO, near activation; the reviewed protocol pins the FINGERPRINT; the record content-hash is reviewable in the change log without any further Git commit; every authority stays independently verifiable |

### 2.1 The three authorities after this change

1. **Protocol authority** — the FROZEN protocol payload
   (`PROTOCOL_STATUS = "FROZEN"`, fingerprint pinned in
   `FROZEN_PROTOCOL_FINGERPRINT`) **plus** the operator freeze record that
   binds the fresh timestamp to that fingerprint
   (`app/research/htf_baseline_short_phase2_timestamp_authority_v1.py`,
   record schema `HTF_PHASE2_FREEZE_RECORD_V1`).
2. **Database authority** — `research.htf_baseline_short_phase2_experiment`
   (`freeze_ts`, `protocol_fingerprint`) written once by `activate()`; the
   `freeze_registry` PK additionally forbids re-use.
3. **Runtime authority** — `/opt/trad_bot/etc/htf_phase2_activation.json`
   (operator-deployed; unchanged format).

The runner (`--use-registration`) now requires BOTH the activation config and
`--freeze-record`, verifies the record self-hash and canonical timestamps,
and admits a boundary only when record / database / activation / running code
agree **exactly** (timestamps at millisecond level, fingerprints 64-hex
equal). Any missing source, tamper, drift or ordering violation exits `3`
with zero observations.

### 2.2 Why this is not a security downgrade

* the DB acceptance window is enforced TWICE: in the mint/claim tool against
  the PostgreSQL server clock, and inside `activate()` itself;
* a record is claimable only inside `[claimed_at-60s, claimed_at+300s]` and
  `[now-60s, now+300s]`; backdating, stale and far-future timestamps die in
  pure logic before the DB is touched;
* `record_sha256` makes any post-approval edit detectable; the operator
  records the hash (and the canonical JSON) in the change log;
* the mint/claim tool hard-rejects legacy boundaries — including the Point-B
  freeze `2026-10-06T14:00:00Z` — and the DB registry rejects any re-used
  timestamp structurally;
* one record = one claim (UUID `record_id`); a second claim is only the
  documented interrupted-activation repair against the SAME record;
* the claim tool refuses to run unless the deployed code matches the pinned
  `FROZEN_PROTOCOL_FINGERPRINT`;
* nothing in the chain enables the observer: `observer_enabled` stays `false`,
  the systemd gate file is absent, and observe-only startup remains issue
  #198 with its own `GO_FOR_OBSERVE_ONLY_STARTUP`.

---

## 3. Frozen protocol delta (what changed semantically)

`PROTOCOL_STATUS: "PROTOCOL_NOT_FROZEN" -> "FROZEN"` — **the only change
inside `PROTOCOL_V1`**. Proven by regression: restoring that single field
reproduces the reviewed candidate fingerprint
`39accac9ddddfd1495202cd708d7dc95b6c6b93ac79d819ff3d5a02beec60de1`,
while the FROZEN payload fingerprints
`22fa0c08eed92df7bb6e94576987305343a71083f3129f3ce4acd07c287cb3f0`
(pinned in `FROZEN_PROTOCOL_FINGERPRINT`, verified by tests).

Unchanged (regression-tested): all six approved values (`300000`, `20`,
`LEVEL_ELIGIBILITY_LOSS_IS_NON_TERMINAL`, `30`, `5`, `14`), causal contracts,
level/episode/event identity, `AMBIGUOUS_PRIOR_ANCHORS` fail-closed, the
0/10/20/30/40 bps sensitivity declaration, Point-B isolation, paper/live
routing, `retrospective_pnl_tuning = PROHIBITED`,
`phase2_start = PROHIBITED_WITHOUT_OPERATOR_APPROVAL`.
`operator_decisions_pending() == []`. `APPROVED_FREEZE_TS` remains `None` —
the freeze timestamp never enters Git.

---

## 4. Activation sequencing (exact order) and STOP conditions

> DESIGN — the sequence is executed only under a future, separately issued
> `PHASE_2_GO` / `GO_FOR_OBSERVE_ONLY_STARTUP` (issue #198). Nothing in
> issue #197 executes it on production.

1. **STOP check** — preflight (PR #199 §3 read-only commands) confirms:
   `PROTOCOL_STATUS=FROZEN`, `operator_decisions_pending()==[]`,
   pinned fingerprint matches the deployed commit, DB registration `PENDING`,
   `freeze_ts/protocol_fingerprint NULL`, `observer_enabled=false`, zero
   Phase-2 observations, systemd unit disabled/inactive, gate file absent.
   Any deviation → **STOP**.
2. **Mint (operator GO)** — as `trad_bot_htf_p2_admin`, using the DB clock:

   ```bash
   python tools/research/htf_baseline_short_phase2_freeze_record_v1.py mint \
     --operator-approval-ref "<GO reference + operator identity>" \
     --issued-by "<operator>" --record-id "$(uuidgen)" \
     --out /opt/trad_bot/etc/htf_phase2_freeze_record.json
   ```

   Record `record_sha256` + canonical JSON in the change log. STOP on any
   `REFUSED` output (pinned-fingerprint drift, window violation, forbidden
   legacy timestamp).
3. **Claim (same GO, immediately after)** —

   ```bash
   python tools/research/htf_baseline_short_phase2_freeze_record_v1.py claim \
     --record /opt/trad_bot/etc/htf_phase2_freeze_record.json \
     --activation-out /opt/trad_bot/etc/htf_phase2_activation.json
   ```

   This performs the ONE `research.htf_baseline_short_phase2_activate(...)`
   call, then deploys the runtime activation JSON and prints
   `THREE_AUTHORITY_AGREEMENT=OK`. STOP on `REFUSED` / `PARTIAL ACTIVATION`.

   The initial claim enforces the PostgreSQL acceptance window
   `now()-60s .. now()+5min` in full (tool side and inside `activate()`).
   The one exception is the documented **delayed repair** (PR #200 FIX-1):
   `claim --repair-activation-json` against a database that ALREADY carries a
   committed, exactly-matching, registry-evidenced claim — that path performs
   no activation, only re-derives the runtime activation JSON from the
   identical freeze record, and it is the only path where the freshness
   window is not re-applied.
4. **Verify (read-only)** — `... verify --record ... --activation ...` plus
   the §4 post-activation SQL checks; keep the output.
5. **Issue #198 only** — separate `GO_FOR_OBSERVE_ONLY_STARTUP`: set
   `observer_enabled=true`, create `/opt/trad_bot/etc/htf_phase2_observer_enabled`,
   `systemctl start trad-bot-htf-baseline-short-observer` (never `enable`).

**STOP conditions (any one is a hard stop, zero OOS writes):** pinned
fingerprint drift; `operator_decisions_pending() != []`; any `REFUSED` from
mint/claim/verify; any three-authority disagreement; DB registration not
`PENDING` before claim; freeze registry already non-empty without a matching
record; systemd unit or gate file present before step 5; any pre-freeze row in
the observation table.

### 4.1 Partial failure / recovery matrix

| Failure point | State | Recovery (never re-mint a timestamp) |
|---|---|---|
| before mint | nothing changed | normal PR/review flow |
| mint done, claim not run | inert record only | claim later **only if** still inside the acceptance window; otherwise discard record and re-mint under the same GO decision (old record cannot be claimed — freshness rules) |
| claim: DB committed, activation JSON write failed | DB `RUNNING`, no runtime config → observer cannot start (fail-closed) | `claim --repair-activation-json` with the SAME record; never a new `freeze_ts`; if the record is lost, STOP and escalate. This **delayed repair** also works after the `now()-60s .. now()+5min` window has expired (PR #200 FIX-1): it requires the unchanged DB record (same experiment id, `freeze_ts`, protocol fingerprint) **and** the matching immutable `freeze_registry` row **and** an exact `record_id` + `record_sha256` match against the claim receipt sealed at the original claim (PR #200 FIX-4, §4.3); any mismatch — or a missing claim receipt — fails closed before the activation JSON is written |
| claim refused (concurrent attempt won) | exactly one freeze claimed | STOP; inspect registry + record; never mint a new timestamp to "fix" it |
| disagreement found at verify/startup | mixed artifacts | hard NO_GO; keep observer stopped; preserve evidence; operators decide: recover the exact same boundary or `CANCEL` and version a new experiment |
| after issue #198 startup error | observer running/partial | stop ONLY the observer unit, remove ONLY its gate file, set `observer_enabled=false` (always allowed); never rewind the freeze, never edit observations |

### 4.2 Freeze-record SHA-256 evidence model (PR #200 FIX-3)

**What the hash is — and what it is not.** `record_sha256` is the SHA-256 of
the record's canonical JSON payload (all fields except `record_sha256` itself,
`sort_keys=True`, separators `(",", ":")`, `ensure_ascii=True`). It is
**tamper evidence only**: it detects any later edit of the file. It is NOT a
digital signature, NOT a proof of who created the record, and NOT independent
proof of authenticity — anyone who can rewrite the file can also recompute the
hash. Authenticity comes from the out-of-band operator GO record
(`operator_approval_ref`, `issued_by`, the human change log), never from the
JSON self-hash.

**How the operator records the evidence (at mint, in the same GO):**

1. capture the mint output — the canonical JSON and the printed
   `record_sha256=...` line;
2. paste BOTH into the change log / issue GO comment, together with the
   approving operator identity, the GO reference and a UTC timestamp;
3. optionally GPG-sign that change-log entry (or the record file) — this is
   the only step in the flow that adds actual cryptographic authenticity.

**How an independent reviewer verifies it (without trusting the tool or the
operator workstation):** copy ONLY the JSON file to any machine and recompute
the digest with stock Python (no project code involved):

```bash
python -c "import json,hashlib,sys; r=json.load(open(sys.argv[1], encoding='utf-8')); r.pop('record_sha256', None); print(hashlib.sha256(json.dumps(r, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')).hexdigest())" /opt/trad_bot/etc/htf_phase2_freeze_record.json
```

* the output must equal the `record_sha256` recorded in the change log at
  mint time — compare them byte-for-byte;
* cross-check with coreutils on a different host if desired:
  `jq -cS 'del(.record_sha256)' htf_phase2_freeze_record.json | tr -d '\n' | sha256sum`
  (equivalent for ASCII-only fields; the Python recipe above is
  authoritative);
* then confirm the semantic fields against the GO decision by eye:
  `experiment_id`, `protocol_fingerprint` (= pinned
  `FROZEN_PROTOCOL_FINGERPRINT`), `freeze_ts`, `claimed_at`,
  `record_id`, `operator_approval_ref`, `issued_by`;
* `verify --record ... --activation ...` re-checks the three runtime
  authorities afterwards, but it is a convenience — the digest comparison
  above is the independent artifact check and must be performed outside the
  tool.

Any mismatch at any step = the record cannot be trusted = **STOP** (no claim,
no repair, no activation JSON).

### 4.3 Claim-receipt record identity (PR #200 FIX-4)

**Defect found by the independent VPS run.** A second `mint` executed within
the same wall-clock second produces a NEW freeze record with the SAME
`freeze_ts` and protocol fingerprint — and therefore a fully matching DB
boundary — but a different `record_id` and `record_sha256`. The delayed-repair
path compared only (experiment id, `freeze_ts`, fingerprint), so it accepted
this second record and re-derived the activation JSON from it. The record's
self-hash could not help: it verifies only that a record is internally intact
(tamper evidence), never that the record is the one the original GO claimed.

**Trusted source of identity — the claim receipt** (schema
`HTF_PHASE2_CLAIM_RECEIPT_V1`, file
`<activation-out>.claim_receipt.json`, e.g.
`/opt/trad_bot/etc/htf_phase2_activation.json.claim_receipt.json`):

* the receipt binds `experiment_id`, `protocol_fingerprint`, `freeze_ts`,
  `claimed_at`, **`record_id`** and **`record_sha256`** (plus its own
  `receipt_sha256` self-hash) and is a deterministic pure function of the
  freeze record;
* it is sealed **exactly once** by the initial claim, **strictly BEFORE the
  `activate()` transaction commits** — so every committed claim provably
  carries the identity of exactly one record, and an interrupted seal leaves
  the database un-claimed (fail-closed both ways);
* it is written through a dedicated atomic primitive (`O_EXCL`, fsync, 0600,
  symlink-safe) and is **never overwritten**: only a byte-identical re-seal of
  the SAME record (idempotent retry of an interrupted claim) is accepted; any
  other content refuses the claim before the database is touched;
* the repair path re-derives the activation JSON ONLY when the presented
  record's `record_id` AND `record_sha256` (and every bound field) equal the
  receipt EXACTLY; a same-second twin record is refused even when the DB
  boundary matches completely.

**Fail closed (required).** A missing, unreadable or mismatched receipt means
the original identity cannot be established → the repair refuses BEFORE any
write and the operator must STOP and escalate. The record self-hash is never
substituted for provenance. This does not weaken the initial claim path (the
acceptance window, the single `activate()` call and the registry guards are
unchanged) and never permits re-activation.

**Why not the database.** `freeze_registry` is keyed by `freeze_ts` and holds
no record identity; it is trigger-immutable. Recording `record_id` /
`record_sha256` there would require a schema/function change to migrations
074/075, which is forbidden without separate approval. The claim receipt is
therefore the pre-commit write that stands in for that binding.

**Residual trust (documented, not hidden).** The receipt is a local artifact
protected by the same controls as the record and the activation JSON (0600,
operator-owned). An attacker who can rewrite those files can also forge a
receipt — which is exactly why §4.2 requires recording `record_sha256` in the
change log (and optionally GPG-signing it): a reviewer comparing the receipt,
the record and the change-log entry detects the forgery. DELETING the receipt
only denies repair (fail closed); it never admits a different record.

**Regression.** `tests/test_htf_baseline_short_phase2_freeze_record_postgres_v1.py::test_two_mints_same_second_delayed_repair_binds_original_record_identity`
(Linux/PostgreSQL IT) deterministically mints two records with one shared
`--freeze-ts`, claims the first, and proves that delayed repair refuses the
twin and succeeds ONLY with the original record. Unit coverage (no DB):
`tests/test_htf_baseline_short_phase2_freeze_record_v1.py` (twin refusal,
fail-closed without receipt, one-time seal) and
`tests/test_htf_baseline_short_phase2_timestamp_authority_v1.py` (receipt
purity/identity binding).

### 4.4 Atomic no-clobber immutable publication (PR #200 FIX-6)

**Defect (Codex Review P2).** `_atomic_write_bytes()` published immutable
artifacts with `os.replace(tmp, target)` after a `target.exists()` pre-check.
That is a TOCTOU race: a competing writer publishing between the check and
the replace is silently CLOBBERED — the "never overwritten" rule held only
for sequential use. The race affects every immutable artifact written by the
tool: the freeze record (`cmd_mint`), the claim receipt
(`_seal_claim_receipt`), and the initial activation JSON.

**Fix — atomic no-clobber publication.** For `overwrite=False` the temp file
(after `write` + `fsync`, mode 0600, `O_EXCL` creation) is published with
`os.link(tmp, target)`, never `os.replace`:

* `os.link` fails with `FileExistsError` when the destination exists
  **atomically at link time** (Windows `CreateHardLinkW` →
  `ERROR_ALREADY_EXISTS`; POSIX `link(2)` → `EEXIST`), so exactly one of any
  number of concurrent publications wins and the winner's bytes are the
  final content — a published artifact can never be replaced;
* rename-based no-clobber is deliberately NOT used: `os.rename` refuses an
  existing destination on Windows but clobbers it on POSIX, so its semantics
  cannot be relied on cross-platform. `os.link` is no-clobber on both; the
  platform property is *proven* by
  `test_os_link_refuses_existing_target_on_this_platform` on each OS the
  suite runs on (Windows locally, Linux on VPS), not assumed;
* `FileExistsError` → fail-closed `ToolRefused`; any other `OSError`
  (e.g. a filesystem without hard links) → fail-closed `ToolRefused` too —
  the tool never degrades to a clobberable write;
* the temp name is removed on EVERY path (success, interrupted write,
  publication conflict), preserving the FIX-2 interrupted-write behaviour
  and the `O_EXCL` squatting refusal.

`overwrite=True` keeps its exclusive meaning — the authorized
`--repair-activation-json` path — and is the ONLY remaining `os.replace`
user inside the primitive.

**Identity consequences.** The freeze record and the claim receipt become
true write-once artifacts under concurrency: if two operators claim two
different records in the same window, exactly one receipt is sealed and
`verify_claim_identity` can only ever bind that winner's `record_id` +
`record_sha256`; the losing seal attempt is refused and can never rewrite
the identity (and vice versa for the freeze record).

**Reintroduction guard.** `test_immutable_publication_never_calls_os_replace`
monkeypatches `os.replace` to explode and proves immutable publication and
receipt sealing still succeed while the repair path still raises through it;
`test_immutable_publication_source_has_single_replace_on_repair_path` is an
AST guard requiring `os.link()` and exactly one `os.replace` call — inside
the `overwrite=True` branch only.

**Race regressions.** `tests/test_htf_baseline_short_phase2_freeze_record_v1.py`
runs real multi-process races (spawn + Barrier, different payloads):
`test_concurrent_immutable_publications_exactly_one_wins` (4 competing
writers: exactly one OK, final content = winner's, no temp leftovers) and
`test_concurrent_claim_receipt_seals_bind_one_identity` (two different
records: exactly one seal, loser can never bind or re-seal), plus the
deterministic `test_publication_conflict_cleans_temp_and_preserves_winner`
(simulated competitor) and
`test_temp_file_cleaned_up_on_precheck_refusal` /
`test_temp_file_cleaned_up_on_symlink_refusal`.

## 5. Verification performed in this task (isolated, no production access)

* pure-logic + stubbed-gate regression:
  `tests/test_htf_baseline_short_phase2_timestamp_authority_v1.py`;
* frozen-protocol regression (updated in place):
  `tests/test_htf_baseline_short_phase2_protocol_v1.py`;
* PostgreSQL integration on a **disposable initdb cluster** (own data
  directory `D:/py_pro/trad_bot/.issue197_pg_isolation/data`, port `55433`,
  loopback-only — never `trad_bot:5432`, never the production server):
  `tests/test_htf_baseline_short_phase2_freeze_record_postgres_v1.py` plus the
  pre-existing Phase-2 DB suites;
* scenario matrix (§ E of the issue) is covered scenario-by-scenario; every
  SKIPPED test is listed in the task report with its reason.

## 6. Explicit non-actions (issue #197)

No production operation was performed: no merge to `main`, no `/opt/trad_bot`
changes, no production registration/freeze/ACL edits, no `APPROVED_FREEZE_TS`
set, no activation flag, `observer_enabled` untouched, no systemd unit
started/enabled, scanner/paper/live/evaluator untouched, Point-B frozen
protocol untouched, zero OOS observations created, zero real positions.
