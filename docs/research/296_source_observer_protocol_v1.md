# ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1
## Phase 1: Source Observer Protocol

Issue: #296
Version: 1.0.0
Mode: RESEARCH_ONLY
Deployment status: DEPLOY_HOLD

### Source

dds.me_long_v2_inverse_short_v3_event

Source scanner:
MOMENTUM_EXHAUSTION_REVERSE_LONG_V2

Source direction: LONG
Capture route: PRE_FILTER_SOURCE

Source access: SELECT ONLY.

### Identity

Preserve original V3 event_key.
Never regenerate identity from observation time.

One event_key = one source event.

### Prospective boundary

New independent freeze_ts.

Eligible events require:
- signal_time > freeze_ts
- source created_at >= freeze_ts
- source captured_at >= freeze_ts
- complete source lineage

No backfill into the prospective cohort.

### Processing

- Append-only source_event
- Durable processed_event ledger
- Idempotent restart
- Transactional batch commit
- PostgreSQL advisory lock
- Explicit rejection reasons
- Source integrity and coverage audit

### Isolation

No modification to:
- scanner
- V3 runtime
- trading gates
- paper/live execution
- existing OOS

No restart or reload of existing services.

### Evaluation boundary

Source capture does not imply valid outcome evaluation.

H1, H2, H3 and H4 require separately frozen:
- feature definitions
- causal ASOF / closed candle rules
- control and paired comparison
- TP/SL/timeout
- fees and slippage
- minimum N and uncertainty policy

Incomplete source or candle lineage is excluded
from final OOS verdicts.

### Current decision

SOURCE_VALIDATED
DEPLOY_HOLD until isolated deployment acceptance.
