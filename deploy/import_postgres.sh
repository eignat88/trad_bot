#!/usr/bin/env bash
# deploy/import_postgres.sh
# Restore a PostgreSQL dump into the VPS trad_bot database.
#
# Usage (on VPS as root):
#   sudo bash /opt/trad_bot/deploy/import_postgres.sh /opt/trad_bot/trad_bot.dump
#
# Prerequisites:
#   - Dump file must exist on VPS (copied via scp)
#   - PostgreSQL must be running
#   - trad_bot database and user must exist
<<<<<<< HEAD
#
# Exit codes:
#   0 - migration completed successfully
#   1 - argument / file error
#   2 - backup failed
#   3 - restore failed
#   4 - ownership / permissions failed
#   5 - verification failed
=======
>>>>>>> origin/main

set -euo pipefail

# --- Arguments ---
DUMP_FILE="${1:-}"
if [ -z "$DUMP_FILE" ]; then
    echo "Usage: $0 <dump_file>"
    echo "Example: $0 /opt/trad_bot/trad_bot.dump"
    exit 1
fi

if [ ! -f "$DUMP_FILE" ]; then
    echo "ERROR: Dump file not found: $DUMP_FILE"
    exit 1
fi

DB_NAME="trad_bot"
DB_USER="trad_bot"
BACKUP_FILE="/opt/trad_bot/trad_bot_before_restore.dump"

echo "=== Trad Bot DB Migration ==="
echo ""

# --- Step 1: Stop services ---
echo "Step 1: Stopping services..."
systemctl stop trad-bot-scanner 2>/dev/null || true
systemctl stop trad-bot-paper 2>/dev/null || true
sleep 2

# Verify no running processes
if pgrep -f "scanner_runner.py" > /dev/null 2>&1; then
    echo "WARNING: scanner_runner.py still running, killing..."
    pkill -f "scanner_runner.py" || true
fi
if pgrep -f "paper_runner.py" > /dev/null 2>&1; then
    echo "WARNING: paper_runner.py still running, killing..."
    pkill -f "paper_runner.py" || true
fi
sleep 1
echo "  Services stopped."
echo ""

# --- Step 2: Backup current VPS database ---
echo "Step 2: Backing up current VPS database..."
<<<<<<< HEAD

# Check if database has any tables (empty DB on first migration is OK)
TABLE_COUNT=$(sudo -u postgres psql -d "$DB_NAME" -t -c \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'dds';" 2>/dev/null || echo "0")

if [ "$TABLE_COUNT" -gt 0 ]; then
    echo "  Database has $TABLE_COUNT table(s) in dds schema, creating backup..."
    if ! sudo -u postgres pg_dump -Fc "$DB_NAME" -f "$BACKUP_FILE" 2>&1; then
        echo "ERROR: pg_dump failed. Aborting migration to prevent data loss."
        exit 2
    fi
    if [ ! -f "$BACKUP_FILE" ]; then
        echo "ERROR: pg_dump did not create backup file. Aborting migration."
        exit 2
    fi
    BACKUP_SIZE=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || stat -f%z "$BACKUP_FILE" 2>/dev/null || echo "0")
    echo "  Backup saved: $BACKUP_FILE ($BACKUP_SIZE bytes)"
else
    echo "  No existing data in dds schema (first migration). Skipping backup."
=======
sudo -u postgres pg_dump -Fc "$DB_NAME" -f "$BACKUP_FILE" 2>/dev/null || true
if [ -f "$BACKUP_FILE" ]; then
    echo "  Backup saved: $BACKUP_FILE"
else
    echo "  No existing data to backup (first migration)."
>>>>>>> origin/main
fi
echo ""

# --- Step 3: Restore dump ---
echo "Step 3: Restoring dump..."
<<<<<<< HEAD
# Capture pg_restore output and exit code; show last 10 lines on failure
RESTORE_LOG=$(mktemp)
RESTORE_EXIT=0
=======
>>>>>>> origin/main
sudo -u postgres pg_restore \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    -d "$DB_NAME" \
<<<<<<< HEAD
    "$DUMP_FILE" >"$RESTORE_LOG" 2>&1 || RESTORE_EXIT=$?

if [ "$RESTORE_EXIT" -ne 0 ]; then
    echo "ERROR: pg_restore failed with exit code $RESTORE_EXIT."
    echo "Last 10 lines of pg_restore output:"
    tail -10 "$RESTORE_LOG"
    rm -f "$RESTORE_LOG"
    echo ""
    echo "Rolling back: restoring backup..."
    if [ -f "$BACKUP_FILE" ]; then
        sudo -u postgres pg_restore \
            --clean --if-exists --no-owner --no-privileges \
            -d "$DB_NAME" "$BACKUP_FILE" 2>/dev/null || true
        echo "  Rollback restore attempted."
    fi
    exit 3
fi
rm -f "$RESTORE_LOG"
echo "  Restore completed successfully."
=======
    "$DUMP_FILE" 2>&1 | tail -5 || true
echo "  Restore completed."
>>>>>>> origin/main
echo ""

# --- Step 4: Fix ownership ---
echo "Step 4: Fixing ownership..."
<<<<<<< HEAD

if ! sudo -u postgres psql -d "$DB_NAME" -c \
    "ALTER DATABASE $DB_NAME OWNER TO $DB_USER;" 2>&1; then
    echo "ERROR: Failed to set database owner."
    exit 4
fi

if ! sudo -u postgres psql -d "$DB_NAME" -c \
    "ALTER SCHEMA public OWNER TO $DB_USER;" 2>&1; then
    echo "ERROR: Failed to set schema 'public' owner."
    exit 4
fi

if ! sudo -u postgres psql -d "$DB_NAME" -c \
    "ALTER SCHEMA dds OWNER TO $DB_USER;" 2>&1; then
    echo "ERROR: Failed to set schema 'dds' owner."
    exit 4
fi

# Grant permissions
if ! sudo -u postgres psql -d "$DB_NAME" -c "
=======
sudo -u postgres psql -d "$DB_NAME" -c "ALTER DATABASE $DB_NAME OWNER TO $DB_USER;" 2>/dev/null || true
sudo -u postgres psql -d "$DB_NAME" -c "ALTER SCHEMA public OWNER TO $DB_USER;" 2>/dev/null || true
sudo -u postgres psql -d "$DB_NAME" -c "ALTER SCHEMA dds OWNER TO $DB_USER;" 2>/dev/null || true

# Grant permissions
sudo -u postgres psql -d "$DB_NAME" -c "
>>>>>>> origin/main
    GRANT ALL ON SCHEMA dds TO $DB_USER;
    GRANT ALL ON ALL TABLES IN SCHEMA dds TO $DB_USER;
    GRANT ALL ON ALL SEQUENCES IN SCHEMA dds TO $DB_USER;
    ALTER DEFAULT PRIVILEGES IN SCHEMA dds GRANT ALL ON TABLES TO $DB_USER;
    ALTER DEFAULT PRIVILEGES IN SCHEMA dds GRANT ALL ON SEQUENCES TO $DB_USER;
<<<<<<< HEAD
" 2>&1; then
    echo "ERROR: Failed to grant permissions to $DB_USER."
    exit 4
fi
=======
" 2>/dev/null || true
>>>>>>> origin/main
echo "  Ownership fixed."
echo ""

# --- Step 5: Verify ---
echo "Step 5: Verifying restore..."
echo ""
<<<<<<< HEAD

REQUIRED_TABLES=("dds.scanner_run" "dds.scanner_setup" "dds.market_signal" "dds.paper_trade" "dds.signal_outcome")
VERIFY_FAILED=0

for TABLE in "${REQUIRED_TABLES[@]}"; do
    ROW_COUNT=$(sudo -u postgres psql -d "$DB_NAME" -t -c \
        "SELECT COUNT(*) FROM $TABLE;" 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$ROW_COUNT" ]; then
        echo "  FAIL: $TABLE does not exist or is not accessible."
        VERIFY_FAILED=1
    else
        ROW_COUNT=$(echo "$ROW_COUNT" | tr -d '[:space:]')
        echo "  OK:   $TABLE ($ROW_COUNT rows)"
    fi
done

echo ""

# Show full summary
sudo -u postgres psql -d "$DB_NAME" -c "
    SELECT 'scanner_run' AS table_name, COUNT(*) AS rows FROM dds.scanner_run
    UNION ALL SELECT 'scanner_setup', COUNT(*) FROM dds.scanner_setup
    UNION ALL SELECT 'market_signal', COUNT(*) FROM dds.market_signal
    UNION ALL SELECT 'paper_trade', COUNT(*) FROM dds.paper_trade
    UNION ALL SELECT 'instrument', COUNT(*) FROM dds.instrument
    UNION ALL SELECT 'signal_outcome', COUNT(*) FROM dds.signal_outcome
    ORDER BY table_name;
" 2>/dev/null || true

if [ "$VERIFY_FAILED" -ne 1 ]; then
    echo ""
    echo "=== Migration complete ==="
    echo ""
    echo "Next steps:"
    echo "  1. Test scanner manually:"
    echo "     cd /opt/trad_bot && source .venv/bin/activate"
    echo "     python scanner_runner.py"
    echo ""
    echo "  2. If successful, restart services:"
    echo "     sudo systemctl start trad-bot-scanner"
    echo "     sudo systemctl start trad-bot-paper"
    echo ""
    echo "  3. Verify services:"
    echo "     sudo systemctl status trad-bot-scanner"
    echo "     sudo systemctl status trad-bot-paper"
    exit 0
else
    echo "ERROR: Verification failed — one or more required tables are missing."
    echo "The database may be in an inconsistent state. Check manually:"
    echo "  sudo -u postgres psql -d trad_bot -c '\\dt dds.*'"
    exit 5
fi
=======
sudo -u postgres psql -d "$DB_NAME" -c "
    SELECT 'scanner_run' AS table_name, COUNT(*) AS rows FROM dds.scanner_run
    UNION ALL
    SELECT 'scanner_setup', COUNT(*) FROM dds.scanner_setup
    UNION ALL
    SELECT 'market_signal', COUNT(*) FROM dds.market_signal
    UNION ALL
    SELECT 'paper_trade', COUNT(*) FROM dds.paper_trade
    UNION ALL
    SELECT 'instrument', COUNT(*) FROM dds.instrument
    UNION ALL
    SELECT 'signal_outcome', COUNT(*) FROM dds.signal_outcome;
" 2>/dev/null || true

echo ""
echo "=== Migration complete ==="
echo ""
echo "Next steps:"
echo "  1. Test scanner manually:"
echo "     cd /opt/trad_bot && source .venv/bin/activate"
echo "     python scanner_runner.py"
echo ""
echo "  2. If successful, restart services:"
echo "     sudo systemctl start trad-bot-scanner"
echo "     sudo systemctl start trad-bot-paper"
echo ""
echo "  3. Verify services:"
echo "     sudo systemctl status trad-bot-scanner"
echo "     sudo systemctl status trad-bot-paper"
>>>>>>> origin/main
