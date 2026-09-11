# Analytics Foundation Deployment Guide

## Overview

This guide covers the deployment of the Analytics Foundation (Stage 1) for the trad_bot project. The analytics pipeline provides daily automated analysis of trading data with PROVISIONAL and FINAL maturity states.

## Prerequisites

### 1. VPS Requirements
- Ubuntu 24.04 or compatible Linux distribution
- PostgreSQL 17+
- Python 3.11+
- Systemd

### 2. OS User Creation
Create a dedicated OS user for the analytics service:

```bash
sudo useradd -r -s /bin/false tradbot-analytics
sudo usermod -aG tradbot tradbot-analytics
```

### 3. PostgreSQL Role Creation
Create a dedicated PostgreSQL role for the analytics pipeline:

**Option A: Using migration 010_analytics_rbac.sql (Recommended)**
```bash
cd /opt/trad_bot
sudo -u postgres psql -d trad_bot -f sql/migrations/010_analytics_rbac.sql
```

**Option B: Manual creation (if migration fails)**
```sql
-- Connect as postgres
sudo -u postgres psql

-- Create role without password (credential set separately)
CREATE ROLE analytics_runner WITH LOGIN NOINHERIT;

-- Set safe role attributes
ALTER ROLE analytics_runner
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS;

-- Grant permissions
GRANT CONNECT ON DATABASE trad_bot TO analytics_runner;
GRANT USAGE ON SCHEMA analytics, market, dds TO analytics_runner;

-- Analytics schema: full CRUD
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA analytics TO analytics_runner;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA analytics TO analytics_runner;

-- Market schema: SELECT, INSERT, UPDATE on market.candle only
GRANT SELECT, INSERT, UPDATE ON market.candle TO analytics_runner;

-- DDS schema: SELECT only on dds.paper_trade
GRANT SELECT ON dds.paper_trade TO analytics_runner;

-- Explicitly deny write permissions on critical tables
REVOKE UPDATE, INSERT, DELETE ON config.scanner_direction_gate FROM analytics_runner;
REVOKE UPDATE, INSERT, DELETE ON config.scanner_grafana_visibility FROM analytics_runner;
REVOKE UPDATE, INSERT, DELETE ON dds.paper_trade FROM analytics_runner;
REVOKE UPDATE, INSERT, DELETE ON dds.paper_account FROM analytics_runner;
REVOKE UPDATE, INSERT, DELETE ON dds.paper_trade_stats FROM analytics_runner;
```

**Important:** The analytics_runner role has minimal permissions:
- ✅ SELECT, INSERT, UPDATE on analytics.*
- ✅ SELECT, INSERT, UPDATE on market.candle
- ✅ SELECT on dds.paper_trade (for post-exit coverage check)
- ❌ NO DELETE on any table
- ❌ NO access to config.* (except read-only if needed)
- ❌ NO write access to dds.* except market.candle
- ❌ NO SUPERUSER, CREATEDB, CREATEROLE, REPLICATION, BYPASSRLS

### 4. Environment File
Create `/etc/trad-bot/analytics.env`:

```bash
# Analytics Pipeline Configuration
ANALYTICS_SCHEDULE_TIME=06:00
ANALYTICS_TIMEZONE=Europe/Sofia
ANALYTICS_POST_EXIT_HOURS=4
ANALYTICS_CANDLE_RETENTION_DAYS=180
ANALYTICS_CANDLE_WORKERS=2
ANALYTICS_API_RETRY_COUNT=3
ANALYTICS_STAGE_TIMEOUT_SECONDS=3600
ANALYTICS_ENABLED=false

# Database configuration
DB_HOST=localhost
DB_PORT=5432
DB_NAME=trad_bot
DB_USER=analytics_runner
DB_PASSWORD=<SET_SEPARATELY>

# Bybit API (read-only)
BYBIT_API_KEY=<SET_SEPARATELY>
BYBIT_API_SECRET=<SET_SEPARATELY>
```

**IMPORTANT**: 
- Set the database password and API credentials securely. Never commit secrets to Git.
- **Initial state:** `ANALYTICS_ENABLED=false` - only enable after migrations, DDL/grants verification, and manual smoke test.
- **Enable only after:** All migrations applied, RBAC verified, manual test run successful.

## Deployment Steps

### 1. Pull Latest Code
```bash
cd /opt/trad_bot
git fetch origin
git switch main
git pull --ff-only origin main
```

### 2. Install Dependencies
```bash
cd /opt/trad_bot
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Apply Database Migrations
```bash
# Apply analytics foundation migration
sudo -u postgres psql -d trad_bot -f sql/migrations/008_analytics_foundation.sql

# Apply market candle migration
sudo -u postgres psql -d trad_bot -f sql/migrations/009_market_candle.sql
```

### 4. Deploy Systemd Files
```bash
# Copy service files
sudo cp deploy/systemd/trad-bot-analytics.service /etc/systemd/system/
sudo cp deploy/systemd/trad-bot-analytics.timer /etc/systemd/system/
sudo cp deploy/systemd/trad-bot-analytics-finalize.service /etc/systemd/system/
sudo cp deploy/systemd/trad-bot-analytics-finalize.timer /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable timers
sudo systemctl enable trad-bot-analytics.timer
sudo systemctl enable trad-bot-analytics-finalize.timer

# Start timers
sudo systemctl start trad-bot-analytics.timer
sudo systemctl start trad-bot-analytics-finalize.timer
```

### 5. Verify Deployment
```bash
# Check service status
sudo systemctl status trad-bot-analytics.service
sudo systemctl status trad-bot-analytics-finalize.service

# Check timer status
sudo systemctl status trad-bot-analytics.timer
sudo systemctl status trad-bot-analytics-finalize.timer

# Check next run time
sudo systemctl list-timers | grep trad-bot

# Check logs
sudo journalctl -u trad-bot-analytics -n 50
sudo journalctl -u trad-bot-analytics-finalize -n 50
```

## Verification Checklist

### Database
- [ ] Analytics schema exists
- [ ] Market schema exists
- [ ] analytics.analysis_run table exists
- [ ] analytics.analysis_stage_run table exists
- [ ] analytics.data_quality_result table exists
- [ ] market.candle table exists
- [ ] analytics_runner role has correct permissions

### Systemd
- [ ] trad-bot-analytics.service exists
- [ ] trad-bot-analytics.timer exists
- [ ] trad-bot-analytics-finalize.service exists
- [ ] trad-bot-analytics-finalize.timer exists
- [ ] Timers are enabled and started
- [ ] Next run time is correct (06:00 and 10:00 Europe/Sofia)

### Functionality
- [ ] Manual test run succeeds
- [ ] Advisory lock works correctly
- [ ] Quality gate runs without errors
- [ ] Retention cleanup works
- [ ] Logs are written to journal

## Rollback Procedure

### 1. Stop Timers
```bash
sudo systemctl stop trad-bot-analytics.timer
sudo systemctl stop trad-bot-analytics-finalize.timer
```

### 2. Disable Timers
```bash
sudo systemctl disable trad-bot-analytics.timer
sudo systemctl disable trad-bot-analytics-finalize.timer
```

### 3. Remove Systemd Files
```bash
sudo rm /etc/systemd/system/trad-bot-analytics.service
sudo rm /etc/systemd/system/trad-bot-analytics.timer
sudo rm /etc/systemd/system/trad-bot-analytics-finalize.service
sudo rm /etc/systemd/system/trad-bot-analytics-finalize.timer
sudo systemctl daemon-reload
```

### 4. Revert Database Changes (Optional)
```sql
-- Only if necessary
DROP SCHEMA IF EXISTS analytics CASCADE;
DROP SCHEMA IF EXISTS market CASCADE;
```

### 5. Revert Code
```bash
cd /opt/trad_bot
git switch main
git reset --hard <previous-commit-hash>
```

## Monitoring

### Grafana Dashboard
A health mart view is available for Grafana monitoring:

```sql
-- View: analytics.analysis_run_summary
-- View: analytics.stage_performance
-- View: analytics.quality_gate_summary
```

### Key Metrics
- Last successful run
- Last FINAL run
- Stage durations
- Candle lag
- Unresolved gaps
- Quality gate status

### Alerts
Configure alerts for:
- Failed runs
- BLOCKING quality gate failures
- Missing FINAL runs
- High candle lag

## Troubleshooting

### Common Issues

1. **Timer not firing**
   - Check systemd journal: `sudo journalctl -u trad-bot-analytics.timer`
   - Verify timezone: `timedatectl`
   - Check timer list: `systemctl list-timers`

2. **Database connection failed**
   - Verify role permissions
   - Check environment file
   - Test connection: `psql -U analytics_runner -d trad_bot`

3. **Bybit API errors**
   - Check API keys
   - Verify rate limits
   - Check network connectivity

4. **Quality gate failures**
   - Review specific check results
   - Address BLOCKING failures before FINAL

## Security Notes

1. **Credential Rotation**: Rotate the Grafana credential before deployment
2. **Secret Management**: Never commit secrets to Git
3. **Access Control**: Analytics role cannot modify scanner gates
4. **Audit Trail**: All changes are logged in analytics tables

## Performance Considerations

1. **Candle Retention**: Runs daily, deletes candles older than 180 days
2. **Batch Processing**: Candles are processed in batches to avoid memory issues
3. **Rate Limiting**: Bybit API calls are rate-limited to avoid bans
4. **Resource Limits**: Systemd services have CPU and memory limits

## Next Steps

After deployment:
1. Monitor first few runs
2. Adjust retention if needed
3. Configure Grafana dashboards
4. Set up alerting
5. Document any production-specific configurations