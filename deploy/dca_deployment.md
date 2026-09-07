# DCA Breakeven VPS Deployment Guide

## Phase 1: Deploy code + migration with DCA OFF

### 1. Update code on VPS

```bash
cd /opt/trad_bot
git status
git switch main
git fetch origin
git pull --ff-only origin main
git log -5 --oneline
```

**Expected**: HEAD should show `c3bd553 chore: set dca.enabled=false for initial VPS deployment`

### 2. Confirm DCA is disabled in config

```bash
grep -A 10 '"dca"' config.yaml
```

**Expected**: `"enabled": false`

### 3. Apply migration 007

```bash
sudo -u postgres psql \
  -v ON_ERROR_STOP=1 \
  -d trad_bot \
  -f sql/migrations/007_dca_breakeven.sql
```

**Expected**: ALTER TABLE statements complete without errors.

### 4. Verify DCA columns exist

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c "
SELECT column_name, data_type, column_default, is_nullable
FROM information_schema.columns
WHERE table_schema = 'dds'
  AND table_name = 'paper_trade'
  AND column_name LIKE '%dca%'
   OR column_name IN ('atr_at_entry', 'avg_entry_price', 'original_tp', 'active_tp', 'tp_mode')
ORDER BY ordinal_position;
"
```

### 5. Verify existing positions are NOT DCA-enabled

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c "
SELECT trade_id, symbol, scanner_name, direction, status, entered_at,
       dca_enabled, dca_state
FROM dds.paper_trade
WHERE status = 'OPEN'
ORDER BY entered_at DESC;
"
```

**Expected**: All existing open positions show `dca_enabled = false` or `NULL`.

### 6. Apply mart views

```bash
sudo -u postgres psql \
  -v ON_ERROR_STOP=1 \
  -d trad_bot \
  -f sql/mart/dca_performance.sql
```

### 7. Verify mart views

```bash
sudo -u postgres psql -d trad_bot -P pager=off -c "SELECT * FROM mart.dca_overview;"
sudo -u postgres psql -d trad_bot -P pager=off -c "SELECT * FROM mart.dca_performance ORDER BY scanner_name, direction;"
```

**Expected**: Empty/zero results (no DCA trades yet). No SQL errors.

### 8. Restart paper service (DCA OFF)

```bash
sudo systemctl restart trad-bot-paper
sudo systemctl status trad-bot-paper --no-pager -l
journalctl -u trad-bot-paper --since "5 minutes ago" --no-pager -l
```

**Expected**:
- Service is RUNNING
- Existing trades restored from DB
- No DCA orders created (feature disabled)
- No errors related to DCA columns

### 9. Monitor for 24 hours (Phase 1)

Watch for:
- No DCA-related log messages
- Existing positions continue normally
- New positions are created without DCA
- No DB/schema errors

---

## Phase 2: Enable DCA

**Only after Phase 1 is confirmed stable.**

### 10. Enable DCA

Edit `/opt/trad_bot/config.yaml`:

Change:
```json
"dca": {
    "enabled": false,
```

To:
```json
"dca": {
    "enabled": true,
```

### 11. Restart paper service (DCA ON)

```bash
sudo systemctl restart trad-bot-paper
sudo systemctl status trad-bot-paper --no-pager -l
journalctl -u trad-bot-paper --since "5 minutes ago" --no-pager -l
```

**Search logs for**: `DCA`, `DCA_ENTRY`, `DCA_FILLED`, `DCA_BREAKEVEN`

### 12. Monitor DCA activity

```bash
# Check new DCA-enabled positions
sudo -u postgres psql -d trad_bot -P pager=off -c "
SELECT trade_id, symbol, scanner_name, direction,
       dca_enabled, dca_state, dca_price, avg_entry_price
FROM dds.paper_trade
WHERE dca_enabled = TRUE
ORDER BY entered_at DESC
LIMIT 10;
"

# DCA overview
sudo -u postgres psql -d trad_bot -P pager=off -c "SELECT * FROM mart.dca_overview;"
```

---

## Rollback (Emergency)

At any time, if issues arise:

```bash
# Edit config
sudo sed -i 's/"enabled": true/"enabled": false/' /opt/trad_bot/config.yaml

# Restart
sudo systemctl restart trad-bot-paper

# Verify
grep '"enabled"' /opt/trad_bot/config.yaml
```

**Existing DCA positions continue to be managed safely.** Only new positions stop getting DCA.
