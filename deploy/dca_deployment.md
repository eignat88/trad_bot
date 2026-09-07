# DCA Breakeven VPS Deployment Guide

DCA operational flag is controlled via `DCA_ENABLED` environment variable,
not via `config.yaml`. This means `git pull` never overwrites the DCA toggle.

## Initial VPS Setup

### 1. Deploy code

```bash
cd /opt/trad_bot
git fetch origin
git switch main
git pull --ff-only origin main
```

### 2. Apply migration (idempotent)

```bash
sudo -u postgres psql \
  -v ON_ERROR_STOP=1 \
  -d trad_bot \
  -f sql/migrations/007_dca_breakeven.sql
```

### 3. Apply mart views (idempotent)

```bash
sudo -u postgres psql \
  -v ON_ERROR_STOP=1 \
  -d trad_bot \
  -f sql/mart/dca_performance.sql
```

### 4. Create environment file for paper service

```bash
sudo mkdir -p /etc/trad-bot
printf 'DCA_ENABLED=false\n' | sudo tee /etc/trad-bot/paper.env
```

### 5. Ensure systemd unit uses the env file

Add `EnvironmentFile=-/etc/trad-bot/paper.env` to `trad-bot-paper.service`.
The `-` prefix means the file is optional (service starts without it).

After editing the unit file:
```bash
sudo systemctl daemon-reload
```

### 6. Verify config.yaml has DCA disabled as default

The Git-tracked `config.yaml` should have `"enabled": false` in the `dca` section.
This is the safe default — DCA is enabled only via env override.

### 7. Start service

```bash
sudo systemctl restart trad-bot-paper
sudo systemctl status trad-bot-paper --no-pager -l
```

### 8. Verify effective config in logs

```bash
journalctl -u trad-bot-paper --since "2 minutes ago" --no-pager -l | grep "DCA config"
```

Expected:
```
DCA config: enabled=false source=env level_atr=0.75 ...
```

---

## Enable DCA

```bash
printf 'DCA_ENABLED=true\n' | sudo tee /etc/trad-bot/paper.env
sudo systemctl restart trad-bot-paper
journalctl -u trad-bot-paper --since "2 minutes ago" --no-pager -l | grep "DCA config"
```

Expected:
```
DCA config: enabled=true source=env level_atr=0.75 ...
```

## Disable / Rollback DCA

```bash
printf 'DCA_ENABLED=false\n' | sudo tee /etc/trad-bot/paper.env
sudo systemctl restart trad-bot-paper
journalctl -u trad-bot-paper --since "2 minutes ago" --no-pager -l | grep "DCA config"
```

Existing DCA-enabled positions continue to be managed safely.
Only new positions stop getting DCA.

## Verify Git Status Stays Clean

After toggling DCA via env:
```bash
cd /opt/trad_bot
git status
```

Must show `nothing to commit, working tree clean`.
The `config.yaml` is NOT modified.

## Deploy After Updates

```bash
cd /opt/trad_bot
git pull --ff-only origin main

sudo -u postgres psql -v ON_ERROR_STOP=1 -d trad_bot \
  -f sql/migrations/007_dca_breakeven.sql

sudo -u postgres psql -v ON_ERROR_STOP=1 -d trad_bot \
  -f sql/mart/dca_performance.sql

sudo systemctl restart trad-bot-paper
```

`DCA_ENABLED=true` persists in `/etc/trad-bot/paper.env` — no manual re-enablement needed.

## Priority Order

```
DCA_ENABLED env var  →  highest priority
config.yaml dca.enabled  →  fallback
DCASettings default (false)  →  safety net
```

## Accepted Values for DCA_ENABLED

Truthy: `true`, `1`, `yes`, `on` (case-insensitive)
Falsy:  `false`, `0`, `no`, `off` (case-insensitive)

Invalid values cause a **fail-fast** error at startup (ValueError).

## Monitoring

```sql
-- Overview KPIs
SELECT * FROM mart.dca_overview;

-- Per scanner/direction
SELECT * FROM mart.dca_performance ORDER BY scanner_name, direction;
```

## Rollback (No Code/DB Changes Needed)

```bash
printf 'DCA_ENABLED=false\n' | sudo tee /etc/trad-bot/paper.env
sudo systemctl restart trad-bot-paper
```

Do NOT drop DCA columns or mart views during operational rollback.
