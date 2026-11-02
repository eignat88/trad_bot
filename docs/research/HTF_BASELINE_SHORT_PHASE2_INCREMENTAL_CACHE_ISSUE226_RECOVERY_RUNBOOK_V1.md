# HTF Phase-2 Incremental Candle Cache — Issue #226 Recovery Runbook (V1)

Scope: `app/research/htf_baseline_short_phase2_incremental_cache_v1.py`
(`HTF_P2_INCREMENTAL_CACHE_V1`). Research worktree only — no production,
VPS, paper/live, or scanner state is touched by this document.

The cache is **fail-closed by design**: every unsafe state raises
`CacheIntegrityError` (subclass `CacheLockError` for lock contention) and
leaves the persisted cache file **byte-identical**. There is NO automatic
overwrite of an unsafe cache. Recovery is always an explicit operator action.

---

## 1. `recovery limit exceeded; manual recovery required`

**Trigger.** Pending closed 5m bars exceed `max_recovery_bars`
(default `max_cache_bars - min_history_bars`). The sequential recovery pass is
bounded (`page_budget = ceil(pending / max_incremental_bars)`, capped by
`max_recovery_bars`); beyond the bound the load refuses to run.

**Symptoms.** `CacheIntegrityError('recovery limit exceeded; manual recovery
required')`; cache file unchanged (SHA-256 valid); observer symbol blocked.

**Operator procedure.**
1. Confirm the failure reason and the pending-bar count in the logs.
2. Stop or pause the writer for the affected symbol (the inter-process lock
   makes concurrent writers harmless, but recovery is cleaner serialized).
3. Delete the per-symbol cache file `<cache_dir>/<SYMBOL>.json` — this is the
   documented **manual recovery** action. The next load is a COLD rebuild
   (`mode=COLD`) with full `min_history_bars` coverage.
4. Resume the writer. Verify `last_load_metrics['mode'] == 'COLD'` and the
   returned window length `>= min_history_bars`.
5. Record the incident: symbol, pending count, wall time of the gap, and the
   action taken.

**Never** truncate or hand-edit the cache file to “pass” the check.

## 2. Cache corruption (`cache checksum mismatch` / schema drift)

**Trigger.** SHA-256 checksum of the stored `bars` payload does not match the
embedded `sha256` field, or the JSON/schema fields are missing or invalid.

**Symptoms.** `CacheIntegrityError('cache checksum mismatch')` (or a schema
error); the file is left in place.

**Operator procedure.**
1. Do NOT let any tool overwrite the file automatically — the corrupt file is
   evidence. Copy it aside (`<SYMBOL>.json.corrupt-<timestamp>`) for inspection.
2. Inspect for torn writes, disk errors, or external tampering. An intact
   atomic-write marker (`*.tmp` residue absent) plus checksum mismatch
   suggests external interference or bit rot — investigate the host.
3. Recovery: delete the cache file → next load is COLD (see §1.3–1.4).
4. Record the incident with the preserved corrupt copy.

## 3. Impossible causal cursor

**Triggers.**
* `resume cursor beyond the closed window` — `resume_open_ms` is newer than
  the exchange-closed tip (non-causal resume request or clock skew);
* `resume cursor causal lookback evicted by recovery` — the verified window
  starts AFTER `resume_open_ms - (min_history_bars - 1) * BAR_MS`, i.e. the
  deeper context the resume cursor needs was trimmed by `max_cache_bars`;
* `cache ahead of exchange ASOF` — the cache contains bars newer than the
  caller's evaluation ASOF (stale caller clock or out-of-order writers).

**Operator procedure.**
1. Treat as fail-closed: no cache change has been made.
2. For a stale caller: fix the caller clock/ASOF and rerun. The cache is
   correct; the request was not.
3. For an evicted lookback: either restore deeper history through the reviewed
   historical replay (separate mechanism, issue #223/#224 scope) or perform a
   manual COLD rebuild (delete the cache file) and resume from a cursor whose
   `min_history_bars` lookback fits `max_cache_bars`. Consider raising
   `max_cache_bars` after review (`max_recovery_bars <= max_cache_bars -
   min_history_bars` is enforced at construction).
4. Record which trigger fired and the resolution.

## 4. Lock contention (`cache lock busy` / `cannot open cache lock`)

**Trigger.** Another live writer holds the per-symbol lock longer than
`lock_wait_s` (default `0.0` = immediate fail-closed; hard bound 30 s —
never an infinite wait).

**Semantics.**
* The OS advisory lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows)
  is the single source of truth and dies with its process: a crashed writer
  can NEVER leave a stale lock. No sweep is needed; the next acquire succeeds.
* The lock file `<cache_dir>/.<SYMBOL>.cache.lock` carries advisory
  `pid=... ts=...` metadata only. Its mere existence never blocks anyone.

**Operator procedure.** Identify the holder process (metadata is advisory;
confirm with the OS process table). If the holder is alive and progressing,
wait or raise `lock_wait_s` up to the 30 s bound. If the holder is gone, retry
— the OS lock is already released.

## 5. Write/IO errors (`cache write failed`, `cannot open cache lock`)

Atomic save = temp file + `os.fchmod(0600)` + `fsync` + `os.replace`. A failure
anywhere in that sequence leaves the previous cache file byte-identical and
raises. Fix the underlying IO problem (disk full, permissions, antivirus lock)
and rerun; no cache surgery is needed.

---

## Sandbox quirks (local DSH test runner only — NOT normal CI)

These are properties of this constrained local execution environment and must
be kept separate from normal CI results:

1. **0700 directory poisoning.** Directories created via
   `os.mkdir(path, 0o700)` (incl. `tempfile.mkdtemp`) become inaccessible to
   this sandbox (`PermissionError`/`WinError 5` on later file operations).
   The cache module's `cache_dir.mkdir(mode=0o700)` is CORRECT production
   behavior; affected tests pre-create the directory with default ACLs.
   * Shim (kept for the pre-existing preserved tests only):
     `.pytest_tmp/dsh_shim/sitecustomize.py` patches `os.mkdir` to add
     `0o077`. It is gitignored, used only via `PYTHONPATH`, and never
     required by the new Issue #226 tests (`tests/test_htf_baseline_short_phase2_issue226_*` run
     shim-free against `tests/conftest.py` `tmp_path`).
2. **Piped stdio.** Child processes cannot open named pipes in confined
   modes; concurrency tests spawn workers with `stdout/stderr=DEVNULL` and
   communicate through result files.

## Verification after any recovery

Run the joint regression (shim-free unless marked):

```
python -m pytest tests/test_htf_baseline_short_phase2_issue226_bounded_recovery_v1.py \
    tests/test_htf_baseline_short_phase2_issue226_concurrency_lock_v1.py \
    tests/test_htf_baseline_short_phase2_issue226_api_budget_v1.py -q
python -m pytest tests/test_htf_baseline_short_phase2_issue226_causal_parity_v1.py -q     # ~2.5 min
$env:PYTHONPATH = '<shim>'; python -m pytest tests/test_htf_baseline_short_phase2_incremental_cache_v1.py -q   # shim
$env:PYTHONPATH = '<repo root>;<shim>'; python tests/test_htf_baseline_short_phase2_issue226_integration_smoke.py  # shim
```
