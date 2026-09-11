# Analytics Foundation Implementation Plan

## 1. Base Commit
- **Current commit:** dc3e673 (origin/main)
- **Branch:** feat/analytics-foundation
- **Created:** 2026-09-12

## 2. Detected Instructions
- trad-bot-development skill instructions
- Этап 1. Агент ведущий Python backend и data engineer instructions
- Project README.md
- Existing migration patterns (001-007)

## 3. Last Migration Numbers
- **Last migration:** 007_dca_breakeven.sql
- **Next migrations:** 008_analytics_foundation.sql, 009_market_candle.sql

## 4. Discrepancies with Technical Document
| Requirement | Document State | Current Code State | Decision |
|-------------|----------------|-------------------|----------|
| Candle storage | Required | Not implemented | Create market.candle table |
| Analytics schemas | Required | Not implemented | Create analytics and market schemas |
| Daily scheduling | Required | Not implemented | Create systemd timers and runner |
| Data Quality Gate | Required | Not implemented | Implement quality checks |
| PROVISIONAL/FINAL states | Required | Not implemented | Implement maturity states |
| Retention policy | Required | Not implemented | Implement 180-day retention |
| Separate analytics user | Required | Not implemented | Create tradbot-analytics OS user and analytics_runner DB role |

## 5. Files to Create/Modify

### New Files:
1. `app/analytics/__init__.py`
2. `app/analytics/models.py`
3. `app/analytics/repository.py`
4. `app/analytics/candle_sync.py`
5. `app/analytics/candle_ranges.py`
6. `app/analytics/quality.py`
7. `app/analytics/retention.py`
8. `app/analytics/runner.py`
9. `app/analytics/cli.py`
10. `sql/migrations/008_analytics_foundation.sql`
11. `sql/migrations/009_market_candle.sql`
12. `sql/mart/analytics_health.sql`
13. `deploy/systemd/trad-bot-analytics.service`
14. `deploy/systemd/trad-bot-analytics.timer`
15. `deploy/systemd/trad-bot-analytics-finalize.service`
16. `deploy/systemd/trad-bot-analytics-finalize.timer`
17. `tests/test_analytics_models.py`
18. `tests/test_analysis_runner.py`
19. `tests/test_candle_ranges.py`
20. `tests/test_candle_sync.py`
21. `tests/test_data_quality.py`
22. `tests/test_analytics_retention.py`
23. `tests/test_analytics_migrations.py`
24. `docs/analytics-foundation-deployment.md`

### Files to Modify:
1. `app/config/settings.py` - Add analytics configuration
2. `app/db/repository.py` - Add analytics repository methods

## 6. Implementation Sequence
1. **Phase 1: Database Foundation**
   - Create analytics and market schemas
   - Create analytics.analysis_run table
   - Create analytics.analysis_stage_run table
   - Create analytics.data_quality_result table
   - Create market.candle table
   - Add necessary indexes and constraints

2. **Phase 2: Analytics Models**
   - Create dataclass models for analysis_run, analysis_stage_run, data_quality_result
   - Create Candle model for market data
   - Add validation logic

3. **Phase 3: Repository Layer**
   - Create AnalyticsRepository class
   - Implement CRUD operations for analytics tables
   - Add advisory lock mechanism
   - Add idempotency checks

4. **Phase 4: Candle Collection**
   - Extend BybitClient to fetch historical candles
   - Implement candle validation (OHLC rules)
   - Implement UPSERT logic for candles
   - Add watermark tracking

5. **Phase 5: Range Planning**
   - Implement candle range calculation
   - Implement range merging algorithm
   - Implement gap detection
   - Add batch splitting for Bybit API limits

6. **Phase 6: Data Quality Gate**
   - Implement quality checks as per specification
   - Add severity levels (BLOCKING, DEGRADED, WARNING)
   - Add quality result storage

7. **Phase 7: Daily Runner**
   - Implement PROVISIONAL workflow
   - Implement FINAL workflow
   - Add idempotency and crash recovery
   - Add structured logging

8. **Phase 8: Retention**
   - Implement 180-day candle retention
   - Add dry-run mode
   - Add batch deletion

9. **Phase 9: Systemd Artifacts**
   - Create service and timer files
   - Configure Europe/Sofia timezone
   - Add resource limits

10. **Phase 10: Documentation**
    - Create deployment guide
    - Create verification checklist
    - Create rollback procedure

11. **Phase 11: Testing**
    - Write unit tests for all components
    - Write integration tests
    - Run full regression suite

## 7. Test Plan

### Unit Tests:
- DST handling for Europe/Sofia
- Business date calculation
- Semi-open intervals
- Candle range merging
- Gap extraction
- Closed/unclosed candle validation
- OHLC validation
- Direction-independent candle storage
- UPSERT idempotency
- Retry and rate limiting
- PROVISIONAL → FINAL transition
- Retention cutoff
- Secret redaction

### Integration Tests:
- Migrations apply to empty database
- Migrations are idempotent
- Existing tables not damaged
- Two parallel runners don't create duplicate runs
- Crash recovery continues unfinished stage
- Incomplete post-exit context doesn't become FINAL
- Repeated candle backfill doesn't create duplicates
- Analytics role can't update scanner gates
- Existing scanner and paper tests pass without regression

## 8. Known Risks
1. **Bybit API rate limits** - Need careful concurrency control
2. **DST transitions** - Europe/Sofia timezone handling must be tested
3. **Large candle volumes** - Retention cleanup must be efficient
4. **Production data safety** - No destructive operations allowed
5. **Secret management** - Grafana credential must be rotated

## 9. Questions/Blockers
1. **Grafana credential rotation** - Must be done before deployment
2. **OS user creation** - tradbot-analytics user must be created on VPS
3. **DB role creation** - analytics_runner role must be created on VPS
4. **Environment file** - /etc/trad-bot/analytics.env must be created

## 10. Acceptance Criteria Checklist
- [ ] Versioned SQL migrations created (008, 009)
- [ ] Existing production tables unchanged
- [ ] market.candle has correct natural key and constraints
- [ ] Candle loader stores only closed candles
- [ ] Repeated loading doesn't create duplicates
- [ ] Trade ranges merged before API requests
- [ ] Quality Gate stores each check
- [ ] BLOCKING failure prevents valid result publication
- [ ] 06:00 creates PROVISIONAL
- [ ] 4 hours later same run can become FINAL
- [ ] Two parallel runs don't create duplicates
- [ ] Retention set to 180 days with dry-run
- [ ] Systemd timer uses Europe/Sofia and handles DST
- [ ] Service templates contain CPU and memory limits
- [ ] Separate OS and DB identities prepared
- [ ] Analytics role can't change scanner gates and trading tables
- [ ] No secrets in code or documentation
- [ ] All new and existing tests pass
- [ ] Deployment, verification, and rollback instructions prepared
- [ ] Nothing deployed to VPS, production services not restarted