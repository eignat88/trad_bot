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
#
# Exit codes:
#   0 - migration completed successfully
#   1 - argument / file error
#   2 - backup failed
#   3 - restore failed
#   4 - ownership / permissions failed
#   5 - verification failed

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
BACKUP_DIR="/opt/trad_bot/db_backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/trad_bot_before_restore_${TIMESTAMP}.dump"
BACKUP_CREATED=0

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

# Check if database has any tables (empty DB on first migration is OK)
TABLE_COUNT=$(sudo -u postgres psql -d "$DB_NAME" -t -c \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'dds';" 2>/dev/null || echo "0")

if [ "$TABLE_COUNT" -gt 0 ]; then
    echo "  Database has $TABLE_COUNT table(s) in dds schema, creating backup..."
    mkdir -p "$BACKUP_DIR"
    if ! sudo -u postgres pg_dump -Fc "$DB_NAME" -f "$BACKUP_FILE" 2>&1; then
        echo "ERROR: pg_dump failed. Aborting migration to prevent data loss."
        exit 2
    fi
    if [ ! -f "$BACKUP_FILE" ]; then
        echo "ERROR: pg_dump did not create backup file. Aborting migration."
        exit 2
    fi
    BACKUP_SIZE=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || stat -f%z "$BACKUP_FILE" 2>/dev/null || echo "0")
    BACKUP_CREATED=1
    echo "  Backup saved: $BACKUP_FILE ($BACKUP_SIZE bytes)"
else
    echo "  No existing data in dds schema (first migration). Skipping backup."
fi
echo ""

# --- Step 3: Restore dump ---
echo "Step 3: Restoring dump..."
# Capture pg_restore output and exit code; show last 10 lines on failure
RESTORE_LOG=$(mktemp)
RESTORE_EXIT=0
sudo -u postgres pg_restore \
    --role="$DB_USER" \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    -d "$DB_NAME" \
    "$DUMP_FILE" >"$RESTORE_LOG" 2>&1 || RESTORE_EXIT=$?

if [ "$RESTORE_EXIT" -ne 0 ]; then
    echo "ERROR: pg_restore failed with exit code $RESTORE_EXIT."
    echo "Last 10 lines of pg_restore output:"
    tail -10 "$RESTORE_LOG"
    rm -f "$RESTORE_LOG"
    echo ""
    if [ "$BACKUP_CREATED" -eq 1 ]; then
        echo "Rolling back: restoring backup..."
        ROLLBACK_EXIT=0
        sudo -u postgres pg_restore \
            --role="$DB_USER" \
            --clean --if-exists --no-owner --no-privileges \
            -d "$DB_NAME" "$BACKUP_FILE" 2>&1 || ROLLBACK_EXIT=$?
        if [ "$ROLLBACK_EXIT" -ne 0 ]; then
            echo "WARNING: Rollback restore also failed (exit $ROLLBACK_EXIT)."
            echo "  Manual intervention required."
        else
            echo "  Rollback restore succeeded."
        fi
    else
        echo "  No backup was created in this run — skipping rollback."
    fi
    exit 3
fi
rm -f "$RESTORE_LOG"
echo "  Restore completed successfully."
echo ""

# --- Step 4: Fix ownership ---
echo "Step 4: Fixing ownership..."

# 4a. Set database owner
if ! sudo -u postgres psql -d "$DB_NAME" -c \
    "ALTER DATABASE $DB_NAME OWNER TO $DB_USER;" 2>&1; then
    echo "ERROR: Failed to set database owner."
    exit 4
fi

# 4b. Set schema owners
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

# 4c. Grant schema-level and default privileges
if ! sudo -u postgres psql -d "$DB_NAME" -c "
    GRANT ALL ON SCHEMA dds TO $DB_USER;
    GRANT ALL ON ALL TABLES IN SCHEMA dds TO $DB_USER;
    GRANT ALL ON ALL SEQUENCES IN SCHEMA dds TO $DB_USER;
    GRANT ALL ON ALL FUNCTIONS IN SCHEMA dds TO $DB_USER;
    GRANT ALL ON ALL PROCEDURES IN SCHEMA dds TO $DB_USER;
    ALTER DEFAULT PRIVILEGES IN SCHEMA dds GRANT ALL ON TABLES TO $DB_USER;
    ALTER DEFAULT PRIVILEGES IN SCHEMA dds GRANT ALL ON SEQUENCES TO $DB_USER;
    ALTER DEFAULT PRIVILEGES IN SCHEMA dds GRANT ALL ON FUNCTIONS TO $DB_USER;
" 2>&1; then
    echo "ERROR: Failed to grant permissions to $DB_USER."
    exit 4
fi
echo "  Ownership fixed."
echo ""

# --- Step 5: Verify ownership ---
echo "Step 5: Verifying ownership..."
echo ""

REQUIRED_TABLES=("dds.scanner_run" "dds.scanner_setup" "dds.market_signal" "dds.paper_trade" "dds.signal_outcome")
VERIFY_FAILED=0

for TABLE in "${REQUIRED_TABLES[@]}"; do
    # Check table exists and is accessible
    ROW_COUNT=$(sudo -u postgres psql -d "$DB_NAME" -t -c \
        "SELECT COUNT(*) FROM $TABLE;" 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$ROW_COUNT" ]; then
        echo "  FAIL: $TABLE does not exist or is not accessible."
        VERIFY_FAILED=1
        continue
    fi

    # Check table owner = trad_bot
    TABLE_OWNER=$(sudo -u postgres psql -d "$DB_NAME" -t -c \
        "SELECT tableowner FROM pg_tables WHERE schemaname || '.' || tablename = '$TABLE';" 2>/dev/null | tr -d '[:space:]')
    if [ "$TABLE_OWNER" != "$DB_USER" ]; then
        echo "  FAIL: $TABLE owner is '$TABLE_OWNER', expected '$DB_USER'."
        VERIFY_FAILED=1
    else
        ROW_COUNT=$(echo "$ROW_COUNT" | tr -d '[:space:]')
        echo "  OK:   $TABLE ($ROW_COUNT rows, owner=$TABLE_OWNER)"
    fi
done

# Check schema owner
SCHEMA_OWNER=$(sudo -u postgres psql -d "$DB_NAME" -t -c \
    "SELECT schema_owner FROM information_schema.schemata WHERE schema_name = 'dds';" 2>/dev/null | tr -d '[:space:]')
if [ "$SCHEMA_OWNER" != "$DB_USER" ]; then
    echo "  FAIL: schema 'dds' owner is '$SCHEMA_OWNER', expected '$DB_USER'."
    VERIFY_FAILED=1
else
    echo "  OK:   schema dds (owner=$SCHEMA_OWNER)"
fi

echo ""

# Show full summary
SUMMARY_EXIT=0
sudo -u postgres psql -d "$DB_NAME" -c "
    SELECT 'scanner_run' AS table_name, COUNT(*) AS rows FROM dds.scanner_run
    UNION ALL SELECT 'scanner_setup', COUNT(*) FROM dds.scanner_setup
    UNION ALL SELECT 'market_signal', COUNT(*) FROM dds.market_signal
    UNION ALL SELECT 'paper_trade', COUNT(*) FROM dds.paper_trade
    UNION ALL SELECT 'instrument', COUNT(*) FROM dds.instrument
    UNION ALL SELECT 'signal_outcome', COUNT(*) FROM dds.signal_outcome
    ORDER BY table_name;
" 2>&1 || SUMMARY_EXIT=$?

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
    echo "ERROR: Verification failed — one or more required tables are missing or have wrong owner."
    echo "The database may be in an inconsistent state. Check manually:"
    echo "  sudo -u postgres psql -d trad_bot -c '\\dt dds.*'"
    echo "  sudo -u postgres psql -d trad_bot -c \"SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'dds';\""
    exit 5
fi
