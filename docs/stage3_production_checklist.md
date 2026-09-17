# Stage 3 Production Deployment Checklist

## Pre-deployment

- [ ] Migrations 028–033 applied successfully
- [ ] `analytics_agent` role exists (or `analytics_runner` verified)
  ```bash
  sudo -u postgres psql -d trad_bot -c "SELECT rolname FROM pg_roles WHERE rolname IN ('analytics_agent', 'analytics_runner');"
  ```
- [ ] Role privileges verified:
  ```bash
  sudo -u postgres psql -d trad_bot -c "\dp+ analytics.agent_run"
  ```
- [ ] Config/paper trading tables write-denied for analytics role
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT has_table_privilege('analytics_agent', 'config.scanner_direction_gate', 'INSERT'),
           has_table_privilege('analytics_agent', 'dds.paper_trade', 'INSERT'),
           has_table_privilege('analytics_agent', 'market.candle', 'INSERT');
  "
  # Expected: all false
  ```
- [ ] OS user `tradbot-analytics` exists (system account, no login shell)
  ```bash
  id tradbot-analytics
  ```
- [ ] `/etc/trad-bot/analytics.env` created, permissions 600
  ```bash
  ls -la /etc/trad-bot/analytics.env
  # Expected: -rw------- 1 tradbot-analytics tradbot-analytics
  ```
- [ ] `LLM_API_KEY` configured (not in git)
- [ ] `LLM_BASE_URL` configured
- [ ] `LLM_SPECIALIST_MODEL` configured
- [ ] `LLM_CHIEF_MODEL` configured
- [ ] DB credentials configured

## Systemd

- [ ] `trad-bot-analytics.service` enabled
- [ ] `trad-bot-analytics-finalize.service` enabled
- [ ] `trad-bot-analytics.timer` active
- [ ] `trad-bot-analytics-finalize.timer` active
- [ ] `CPUQuota=60%` applied (resource limit)
- [ ] `MemoryMax=1G` applied (resource limit)
- [ ] `NoNewPrivileges=true` applied (security hardening)

```bash
# Verify all of the above:
sudo systemctl is-enabled trad-bot-analytics.service trad-bot-analytics-finalize.service
sudo systemctl is-active trad-bot-analytics.timer trad-bot-analytics-finalize.timer
sudo systemctl show trad-bot-analytics.service -p CPUQuota,MemoryMax,NoNewPrivileges
```

## Smoke Tests

- [ ] **PROVISIONAL run** succeeded:
  ```bash
  journalctl -u trad-bot-analytics -n 50 --no-pager
  # Look for: "Stage 3 analytics run completed" or similar success message
  ```

- [ ] **FINAL run** succeeded:
  ```bash
  journalctl -u trad-bot-analytics-finalize -n 50 --no-pager
  ```

- [ ] `agent_run` table has new rows with status `SUCCEEDED`:
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT agent_name, status, model, latency_ms, total_tokens
    FROM analytics.agent_run
    ORDER BY created_at DESC
    LIMIT 10;
  "
  ```

- [ ] `daily_trading_report` has new `VALIDATED` report:
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT business_date, maturity, status, action_class
    FROM analytics.daily_trading_report
    ORDER BY created_at DESC
    LIMIT 5;
  "
  ```

- [ ] `v_daily_stage3_status` shows current report:
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT * FROM analytics.v_daily_stage3_status
    WHERE business_date = CURRENT_DATE
    LIMIT 5;
  "
  ```

## Security

- [ ] **No secrets in git repository**:
  ```bash
  git log --all --oneline -- '*.env' '*.key' '*.pem'
  # Expected: no results
  ```

- [ ] API key not in systemd unit file:
  ```bash
  grep -i 'key\|secret\|password' /etc/systemd/system/trad-bot-analytics*.service
  # Expected: no results (keys are in .env file only)
  ```

- [ ] Error messages contain `[REDACTED]` where secrets detected (verify in code review)

- [ ] Provider tool_calls rejected (verify in agent prompt/code review)

- [ ] Agent role cannot UPDATE `config` tables:
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT has_table_privilege('analytics_agent', 'config.scanner_direction_gate', 'UPDATE');
  "
  # Expected: false
  ```

- [ ] Agent role cannot DELETE `dds.paper_trade`:
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT has_table_privilege('analytics_agent', 'dds.paper_trade', 'DELETE');
  "
  # Expected: false
  ```

## Observability

- [ ] **Grafana analytics-agents dashboard** shows data:
  - Navigate to Grafana → Dashboards → Trad Bot — Analytics Agents
  - Pipeline Status row shows PROVISIONAL/FINAL status
  - Agent Runs table has recent rows

- [ ] `v_agent_run_observability` returns rows:
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT COUNT(*) AS total_runs,
           COUNT(*) FILTER (WHERE status = 'SUCCEEDED') AS succeeded,
           COUNT(*) FILTER (WHERE status = 'FAILED') AS failed
    FROM analytics.v_agent_run_observability
    WHERE business_date >= CURRENT_DATE - INTERVAL '7 days';
  "
  ```

- [ ] `v_llm_usage_daily` returns token counts:
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT business_date, agent_name, total_tokens, avg_latency_ms
    FROM analytics.v_llm_usage_daily
    ORDER BY business_date DESC
    LIMIT 10;
  "
  ```

- [ ] `v_daily_stage3_status` shows reports:
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT * FROM analytics.v_daily_stage3_status LIMIT 5;
  "
  ```

## Cost

- [ ] Token counts visible in `v_llm_usage_daily`:
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT agent_name, SUM(total_tokens) AS tokens
    FROM analytics.v_llm_usage_daily
    WHERE business_date >= CURRENT_DATE - INTERVAL '7 days'
    GROUP BY agent_name
    ORDER BY tokens DESC;
  "
  ```

- [ ] `compute_cost()` returns correct values for known models (if function exists):
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT analytics.compute_cost('gpt-4o', 1000, 500);
  "
  ```

- [ ] Unknown models return `NULL` (not `0`):
  ```bash
  sudo -u postgres psql -d trad_bot -c "
    SELECT analytics.compute_cost('unknown-model-xyz', 1000, 500);
  "
  # Expected: NULL
  ```
