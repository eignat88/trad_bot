# H1-E — Phase C: VPS Compatibility Matrix (Issue #324)

Scope: isolated prospective H1-E observer vs the `trad-bot-01` runtime state of 27.11.2026.
Research only — nothing here is applied, deployed or activated.

## Runtime state (as supplied; not re-verifiable from this host)

| Item | State | H1-E impact |
|---|---|---|
| HEAD / branch | `b1b6221` (PR #310) / `main` | H1-E ships as a SEPARATE PR from current `main`; no dependence on this SHA beyond #296/#305 |
| PostgreSQL primary | reachable | reader + ledger store target it (read + H1-E-schema-only writes) |
| #305 source | `RUNNING`, freeze `2026-11-24 13:31:53.365952+00:00` | READ-ONLY source of events |
| source events | 120 captured / 1 duplicate / 0 errors; 0 identity/metadata mismatches; 0 pre-freeze | cursor starts fresh (prospective); NO backfill of these 120 |
| coverage audit | 25.11.2026 matched 1/1 | informational; 24–25 Nov window remains `UPSTREAM_SOURCE_COVERAGE_UNPROVEN` |
| H1-E PG schema | ABSENT | expected; appears only after an owner DDL GO |
| untracked `.env.pre_*` files | present | NEVER delete/overwrite; H1-E adds its own env file only |

## Dependency matrix

| Dependency | Required | State | Notes |
|---|---|---|---|
| #296 (observer role / systemd conventions) | role/grant + recovery conventions | CLOSED (PR merged) | closure ≠ runtime gate: `deploy/sql/296_observer_role.sql` pattern is reused in `324_h1e_observer_role.sql`; role must exist on the VPS before the worker connects (owner GO) |
| #305 (upstream source capture) | `research_me_upstream_coverage.source_event` with `captured_at` cursor | CLOSED / `RUNNING` | closure ≠ data-completeness gate: the 24–25 Nov window is `UPSTREAM_SOURCE_COVERAGE_UNPROVEN` (Phase B) |
| H1-C / H1-D frozen packages | classifier + `fetch_htf_history` | merged in `main` | reused UNCHANGED; `htf_fetch` is the only transport entry point |
| H1-D shared limiter / retry budget / circuit breaker | cross-process guards | in `app/exchange` | transport goes through the shared `_public_get` entry; no private sockets |
| PostgreSQL 17.x (test cluster parity) | DDL apply target | local isolated test PG validated (9/9) | VPS apply only with owner GO |
| psycopg2 | driver for reader/store | 2.9.x (validated 2.9.13) | injected via `connection_factory`; no driver import at module import time |

## Isolation matrix (nothing existing is touched)

| Surface | Existing (untouched) | H1-E (new) |
|---|---|---|
| systemd units | `deploy/systemd/trad-bot-*.service` (296/305/H2 templates) | `deploy/systemd/trad-bot-h1e-observer.service` (TEMPLATE ONLY, `H1E_WORKER_ENABLE=0`) |
| credentials | existing DB roles | `tradbot_h1e_observer` (least privilege: SELECT on #305 source; writes ONLY inside `research_me_entry_quality_h1e`) |
| logging | existing journald contours | separate unit identity; `UMask=0077`; no shared log files |
| database writes | dds.*, market.*, scanner/paper/live, OOS | ONLY `research_me_entry_quality_h1e.*` (append-only ledger + pending + checkpoint) |
| API | existing trading callers | read-only public klines via the shared limiter; bounded request budget; no trading endpoints |

## Pure-package purity (unchanged claims)

`app/research/me_entry_quality_h1e_v1` remains free of I/O, DB drivers, network and SQL strings
(`tests/test_me_entry_quality_h1e_isolation_purity_v1.py`, 100% line coverage of the package).
All PostgreSQL/worker code lives in the separate `me_entry_quality_h1e_infra_v1` package;
`htf_transport.py` is the single sanctioned H1-D/H1-C consumer inside the pure package.

## Gate summary

| Gate | Owner action |
|---|---|
| deploy | install unit + env file (disabled) — GO #1 |
| DDL | apply `324_h1e_prospective_ledger_v1_proposal.sql` + role SQL — GO #2 |
| H1-E freeze | mint freeze (`freeze_ts`, `protocol_fingerprint`, approval ref) — GO #3 |
| activation | insert `h1e_activation` (`APPROVED`/`RUNNING`) + flip `H1E_WORKER_ENABLE=1` — GO #4 |

All four gates are independent and fail closed; the worker refuses to start without them
(`tests/test_me_entry_quality_h1e_worker_offline_v1.py`).
