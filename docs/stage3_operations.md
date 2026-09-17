# Stage 3 Analytics — Operations Runbook

## Architecture

- Stage 3 agents analyze canonical trading data produced by the analytics pipeline
- Three specialist agents:
  - **Funnel & Performance** — signal funnel metrics, hit rates, PnL attribution
  - **Execution Quality** — fill quality, slippage, latency analysis
  - **Drift & Anomaly** — distribution shifts, outlier detection, regime changes
- **Chief Trading Analyst** synthesizes specialist outputs into a human-readable report
- No production mutations — analytical only; agents write to `analytics.*` tables

### Data Flow

```
Scanner / Paper Trading
        ↓
   dds.* tables (source of truth)
        ↓
   analytics canonical build (migrations 028-030)
        ↓
   Stage 3 Agents (LLM calls)
        ↓
   agent_run / agent_result / daily_trading_report
        ↓
   Grafana dashboards
```

---

## Services

| Service | Command | Trigger |
|---------|---------|---------|
| `trad-bot-analytics` | `python -m app.analytics.cli run` | Timer 06:00 Europe/Sofia |
| `trad-bot-analytics-finalize` | `python -m app.analytics.cli finalize` | Timer 10:05 Europe/Sofia |

---

## Schedule

| Time (Europe/Sofia) | Service | Maturity | Description |
|---------------------|---------|----------|-------------|
| 06:00 | `trad-bot-analytics` | PROVISIONAL | Provisional analytics run with current data |
| 10:05 | `trad-bot-analytics-finalize` | FINAL | Finalizes previous day's report with complete data |

- Both timers have `Persistent=true` — missed runs execute on next boot.
- The finalize timer waits for the provisional window to ensure data completeness.

---

## Database

| Property | Value |
|----------|-------|
| Database | `trad_bot` |
| Role | `analytics_runner` (existing) or `analytics_agent` (migration 033) |
| Permissions | Least privilege: SELECT on `market`/`dds`, INSERT on `analytics`-owned tables |
| Connection | See `/etc/trad-bot/analytics.env` |

### Key Tables

| Table | Purpose |
|-------|---------|
| `analytics.agent_definition` | Registry of AI agents (INSERT, UPDATE) |
| `analytics.agent_run` | Execution records per agent invocation (INSERT) |
| `analytics.agent_input_manifest` | Immutable input snapshots (INSERT) |
| `analytics.agent_result` | Immutable output snapshots (INSERT) |
| `analytics.daily_trading_report` | Final human-readable reports (INSERT) |
| `analytics.analysis_run` | Pipeline run metadata (INSERT) |
| `analytics.analysis_stage_run` | Pipeline stage metadata (INSERT) |
| `analytics.dataset_publication` | Dataset version tracking (INSERT, UPDATE) |
| `analytics.dataset_publication_log` | Publication event log (INSERT, UPDATE) |

---

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `ANALYTICS_ENABLED` | `true`/`false` — master switch | Yes |
| `ANALYTICS_SCHEDULE_TIME` | `HH:MM` — override schedule | No |
| `ANALYTICS_TIMEZONE` | IANA timezone (default: `Europe/Sofia`) | No |
| `LLM_API_KEY` | LLM provider API key | Yes |
| `LLM_BASE_URL` | Provider endpoint URL | Yes |
| `LLM_SPECIALIST_MODEL` | Model for specialist agents | Yes |
| `LLM_CHIEF_MODEL` | Model for Chief Trading Analyst | Yes |
| `DB_HOST` | PostgreSQL host | Yes |
| `DB_PORT` | PostgreSQL port | Yes |
| `DB_NAME` | Database name (`trad_bot`) | Yes |
| `DB_USER` | Database role (`analytics_agent`) | Yes |
| `DB_PASSWORD` | Database password | Yes |

---

## How to Deploy

1. **Apply migrations** 028–033 in order:
   ```bash
   sudo -u postgres psql -d trad_bot -f sql/migrations/028_agent_foundation.sql
   sudo -u postgres psql -d trad_bot -f sql/migrations/029_dataset_publication.sql
   sudo -u postgres psql -d trad_bot -f sql/migrations/030_canonical_build_functions.sql
   sudo -u postgres psql -d trad_bot -f sql/migrations/031_agent_definition_model.sql
   sudo -u postgres psql -d trad_bot -f sql/migrations/032_stage3_observability.sql
   sudo -u postgres psql -d trad_bot -f sql/migrations/033_analytics_agent_role.sql
   ```

2. **Deploy code** to `/opt/trad_bot`:
   ```bash
   cd /opt/trad_bot
   git fetch origin
   git switch main
   git pull --ff-only origin main
   ```

3. **Set environment file** `/etc/trad-bot/analytics.env`:
   ```bash
   sudo touch /etc/trad-bot/analytics.env
   sudo chmod 600 /etc/trad-bot/analytics.env
   sudo vim /etc/trad-bot/analytics.env
   ```
   Populate with the variables listed above.

4. **Enable timers**:
   ```bash
   sudo systemctl enable --now trad-bot-analytics.timer trad-bot-analytics-finalize.timer
   ```

5. **Verify**:
   ```bash
   sudo systemctl status trad-bot-analytics.timer
   sudo systemctl status trad-bot-analytics-finalize.timer
   ```

---

## How to Check Status

```bash
# Service status
sudo systemctl status trad-bot-analytics.service
sudo systemctl status trad-bot-analytics-finalize.service

# Recent logs
journalctl -u trad-bot-analytics -n 50 --no-pager
journalctl -u trad-bot-analytics-finalize -n 50 --no-pager

# Database status
sudo -u postgres psql -d trad_bot -c "SELECT * FROM analytics.v_daily_stage3_status LIMIT 5"
```

---

## How to Inspect Agent Failures

```sql
SELECT
    business_date,
    agent_name,
    status,
    error_class,
    error_message,
    started_at,
    finished_at
FROM analytics.v_agent_run_observability
WHERE status = 'FAILED'
ORDER BY started_at DESC
LIMIT 20;
```

### Common Failure Classes

| Error Class | Likely Cause | Action |
|-------------|--------------|--------|
| `ConnectionError` | LLM API unreachable | Check `LLM_BASE_URL`, network, API key |
| `RateLimitError` | Provider rate limit hit | Check retry config, wait, or reduce concurrency |
| `ValidationError` | Agent output failed schema check | Review agent prompt, check `agent_result.validation_status` |
| `TimeoutError` | LLM response too slow | Check model load, increase timeout |
| `PermissionError` | DB role missing grants | Verify `analytics_agent` grants with migration 033 |

---

## How to Inspect Cost / Tokens

```sql
-- Daily token usage per agent
SELECT * FROM analytics.v_llm_usage_daily
ORDER BY business_date DESC
LIMIT 20;

-- Token usage filtered by model
SELECT
    business_date,
    agent_name,
    model,
    total_input_tokens,
    total_output_tokens,
    total_tokens,
    avg_latency_ms,
    success_count,
    failure_count
FROM analytics.v_llm_usage_daily
WHERE model = 'gpt-4o'
ORDER BY business_date DESC;

-- Total tokens for current week
SELECT
    agent_name,
    SUM(total_tokens) AS week_tokens
FROM analytics.v_llm_usage_daily
WHERE business_date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY agent_name
ORDER BY week_tokens DESC;
```

---

## How to Rotate LLM API Key

1. Update the environment file:
   ```bash
   sudo vim /etc/trad-bot/analytics.env
   # Update LLM_API_KEY value
   sudo chmod 600 /etc/trad-bot/analytics.env
   ```

2. Restart timers (no code change required):
   ```bash
   sudo systemctl restart trad-bot-analytics.timer trad-bot-analytics-finalize.timer
   ```

3. Verify next run uses the new key:
   ```bash
   journalctl -u trad-bot-analytics -n 20 --no-pager
   ```

---

## How to Rollback

Stage 3 agents are **completely isolated** from the scanner and paper trading systems.

### Disable analytics (safe, no effect on trading):

```bash
# Option A: environment variable
sudo sed -i 's/ANALYTICS_ENABLED=true/ANALYTICS_ENABLED=false/' /etc/trad-bot/analytics.env
sudo systemctl restart trad-bot-analytics.timer

# Option B: disable timers
sudo systemctl disable --now trad-bot-analytics.timer trad-bot-analytics-finalize.timer
```

### Rollback database migration:

```bash
# If migration 033 needs to be reversed:
sudo -u postgres psql -d trad_bot -c "DROP ROLE IF EXISTS analytics_agent;"
```

The canonical analytics pipeline (migrations 014–024) continues independently — Stage 3 agents consume data but do not produce it.

---

## Monitoring Integration

Stage 3 data is surfaced in Grafana via the **Analytics Agents** dashboard (`analytics-agents`).

### Available Views

| View | Purpose |
|------|---------|
| `analytics.v_agent_run_observability` | Agent execution details for Grafana |
| `analytics.v_daily_stage3_status` | Pipeline status with deduplicated reports |
| `analytics.v_llm_usage_daily` | Token usage, reliability, and model breakdown |

### Dashboard Panels

- **Pipeline Status** — last PROVISIONAL/FINAL status, daily report status, chief action
- **Agent Runs** — detailed table with agent_name, model, status, latency, tokens
- **Reliability** — success rate 7d trend, failures by agent
- **Token Usage** — tokens by agent per day, latency by agent
- **Daily Reports** — report version, status, action class, partial flag
