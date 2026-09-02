# PostgreSQL Database Migration: Windows → VPS

This guide covers migrating the local `trad_bot` PostgreSQL database to the Hetzner VPS.

## Why Migrate?

The VPS starts with a fresh, empty PostgreSQL database. The expectancy filter needs historical scanner/signal data to function. Without it:

```
loaded expectancy filter: 0 scanner/direction records
expectancy filter rejected ... INSUFFICIENT_DATA(0)
```

Migrating the local database transfers accumulated paper-trading history.

## Prerequisites

- Local PostgreSQL running with `trad_bot` database
- `pg_dump` available locally (comes with PostgreSQL)
- VPS running with PostgreSQL, `trad_bot` user, and `trad_bot` database
- SSH access to VPS

## Migration Steps

### 1. Export local database

On Windows:

```powershell
cd D:\py_pro\trad_bot
.\deploy\export_postgres.ps1
```

Output:

```
artifacts/db_backup/trad_bot_20250101_120000.dump
```

### 2. Copy dump to VPS

```powershell
scp artifacts\db_backup\trad_bot_*.dump root@91.99.60.150:/opt/trad_bot/
```

Verify on VPS:

```bash
ls -la /opt/trad_bot/trad_bot_*.dump
```

### 3. Restore on VPS

```bash
ssh root@91.99.60.150

# Stop services
sudo systemctl stop trad-bot-scanner
sudo systemctl stop trad-bot-paper

# Run restore script
sudo bash /opt/trad_bot/deploy/import_postgres.sh /opt/trad_bot/trad_bot_*.dump
```

The script will:
- Stop running services
- Backup current VPS database
- Restore the dump with `--no-owner`
- Fix ownership for `trad_bot` user
- Verify row counts

### 4. Test manually

```bash
cd /opt/trad_bot
source .venv/bin/activate
python scanner_runner.py
```

Expected log:

```
loaded expectancy filter: N scanner/direction records
min_samples=30
universe_mode=dynamic top_n=50
scanner started: scanners=7
cycle #1 done: 50/50 symbols | X setups
```

### 5. Start services

```bash
sudo systemctl start trad-bot-scanner
sudo systemctl start trad-bot-paper
```

Verify:

```bash
sudo systemctl status trad-bot-scanner
sudo systemctl status trad-bot-paper
journalctl -u trad-bot-scanner -f
```

## DB Migration Checklist

- [ ] Stop scanner service
- [ ] Stop paper runner service
- [ ] Backup current VPS database
- [ ] Export local database (`.dump` file)
- [ ] Copy dump to VPS via `scp`
- [ ] Restore with `--no-owner`
- [ ] Fix ownership (db, schema, tables, sequences)
- [ ] Verify row counts (scanner_run, scanner_setup, paper_trade, etc.)
- [ ] Verify expectancy records > 0
- [ ] Verify `expectancy_min_samples=30`
- [ ] Run scanner manually, wait for 1 successful cycle
- [ ] Start systemd services
- [ ] Confirm services running independently of SSH

## Troubleshooting

### Permission denied on restore

```bash
# Grant full permissions to trad_bot user
sudo -u postgres psql -d trad_bot -c "
  GRANT ALL ON SCHEMA dds TO trad_bot;
  GRANT ALL ON ALL TABLES IN SCHEMA dds TO trad_bot;
  GRANT ALL ON ALL SEQUENCES IN SCHEMA dds TO trad_bot;
  ALTER DEFAULT PRIVILEGES IN SCHEMA dds GRANT ALL ON TABLES TO trad_bot;
  ALTER DEFAULT PRIVILEGES IN SCHEMA dds GRANT ALL ON SEQUENCES TO trad_bot;
"
```

### Advisory lock held by stale process

```bash
sudo -u postgres psql -d trad_bot -c "
  SELECT l.pid, a.usename, a.state
  FROM pg_locks l
  JOIN pg_stat_activity a ON a.pid = l.pid
  WHERE l.locktype = 'advisory' AND l.objid = 1937261;
"

# Kill stale process
sudo -u postgres psql -d trad_bot -c "SELECT pg_terminate_backend(<pid>);"
```

### Expectancy filter still shows 0 records

The dump may not contain `signal_outcome` data. Check:

```bash
sudo -u postgres psql -d trad_bot -c "SELECT COUNT(*) FROM dds.signal_outcome;"
```

If 0, the expectancy filter will need time to accumulate new data from scanner cycles.

### Rollback to VPS backup

If restore fails or causes issues:

```bash
sudo systemctl stop trad-bot-scanner
sudo systemctl stop trad-bot-paper

sudo -u postgres pg_restore \
    --clean --if-exists --no-owner --no-privileges \
    -d trad_bot \
    /opt/trad_bot/trad_bot_before_restore.dump

sudo systemctl start trad-bot-scanner
sudo systemctl start trad-bot-paper
```
