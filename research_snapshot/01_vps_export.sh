#!/usr/bin/env bash
# ============================================================
# VPS EXPORT — Generic Research Framework Snapshot
# ============================================================
# Run on VPS as root:
#   bash /opt/trad_bot/research_snapshot/01_vps_export.sh
#
# Or copy to /tmp first:
#   scp research_snapshot/01_vps_export.sh root@91.99.60.150:/tmp/
#   ssh root@91.99.60.150 "bash /tmp/01_vps_export.sh"
#
# READ-ONLY on production data.
# Uses pg_dump — snapshot-only, non-blocking, no locks.
# Does NOT modify, lock, restart, or reconfigure anything.
#
# Output:
#   /tmp/research_export/research_full_<timestamp>.sql.gz  (authoritative)
#   /tmp/research_export/research_schema_<timestamp>.sql   (auxiliary)
#   /tmp/research_export/research_data_<timestamp>.sql.gz  (auxiliary)
#   /tmp/research_export/export_metadata.txt
# ============================================================
set -euo pipefail

# ── 0. Constants + export start timestamp ─────────────────────
EXPORT_DIR="/tmp/research_export"
TIMESTAMP=$(date -u '+%Y%m%d_%H%M%S')
EXPORT_STARTED_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
DUMP_FILE="${EXPORT_DIR}/research_full_${TIMESTAMP}.sql.gz"
SCHEMA_FILE="${EXPORT_DIR}/research_schema_${TIMESTAMP}.sql"
DATA_FILE="${EXPORT_DIR}/research_data_${TIMESTAMP}.sql.gz"
META_FILE="${EXPORT_DIR}/export_metadata.txt"

echo "=== RESEARCH SNAPSHOT EXPORT ==="
echo "Started (UTC): ${EXPORT_STARTED_UTC}"
echo "Source DB:     trad_bot"
echo "Export dir:    ${EXPORT_DIR}"
echo ""

# ── 1. Create export directory (owned by postgres) ────────────
install -d -o postgres -g postgres -m 700 "${EXPORT_DIR}"

# ── 2. Pre-export state ───────────────────────────────────────
echo "--- Git state ---"
cd /opt/trad_bot
GIT_HEAD=$(git rev-parse HEAD)
GIT_BRANCH=$(git branch --show-current)
echo "HEAD:   ${GIT_HEAD}"
echo "Branch: ${GIT_BRANCH}"
echo ""

echo "--- PostgreSQL version ---"
PG_VERSION=$(sudo -u postgres psql -d trad_bot -t -c "SHOW server_version;" | tr -d ' ')
echo "PostgreSQL: ${PG_VERSION}"
echo ""

echo "--- Hostname ---"
HOSTNAME=$(hostname -f 2>/dev/null || hostname)
echo "Hostname: ${HOSTNAME}"
echo ""

echo "--- Pre-export table counts ---"
sudo -u postgres psql -d trad_bot -P pager=off -c "
SELECT 'research_experiment'  AS tbl, COUNT(*) AS cnt FROM research.research_experiment
UNION ALL SELECT 'research_observation', COUNT(*) FROM research.research_observation
UNION ALL SELECT 'research_signal',      COUNT(*) FROM research.research_signal
UNION ALL SELECT 'research_outcome',     COUNT(*) FROM research.research_outcome;
"

# ── 3. Verify signal_time column exists ────────────────────────
# Migration 049 defines signal_time on research_observation and
# research_signal.  Verify before we reference it in metadata.
echo "--- Schema verification ---"
SIGNAL_TIME_COL=$(sudo -u postgres psql -d trad_bot -t -A -c "
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'research'
  AND table_name   = 'research_signal'
  AND column_name  = 'signal_time';
")

if [ -z "${SIGNAL_TIME_COL}" ]; then
    echo "WARNING: signal_time not found in research.research_signal" >&2
    echo "         Falling back to created_at for time range metadata." >&2
    SIGNAL_TIME_EXPR="created_at"
else
    echo "  signal_time column: confirmed"
    SIGNAL_TIME_EXPR="signal_time"
fi
echo ""

# ── 4. AUTHORITATIVE full dump (structure + data, compressed) ─
# This is the single authoritative snapshot artifact.
echo "--- Authoritative full dump ---"
sudo -u postgres pg_dump \
    -d trad_bot \
    --schema=research \
    --no-owner \
    --no-privileges \
    --no-comments \
    | gzip > "${DUMP_FILE}"

# Verify gzip integrity and file size
echo "--- Verifying dump integrity ---"
gzip -t "${DUMP_FILE}"
DUMP_SHA256=$(sha256sum "${DUMP_FILE}" | awk '{print $1}')
DUMP_SIZE=$(ls -lh "${DUMP_FILE}" | awk '{print $5}')

echo "  SHA-256: ${DUMP_SHA256}"
echo "  Size:    ${DUMP_SIZE}"

# ── 5. Auxiliary: schema-only dump ────────────────────────────
echo ""
echo "--- Auxiliary schema-only dump ---"
sudo -u postgres pg_dump \
    -d trad_bot \
    --schema=research \
    --schema-only \
    --no-owner \
    --no-privileges \
    --no-comments \
    -f "${SCHEMA_FILE}"

# ── 6. Auxiliary: data-only dump (COPY format, compressed) ────
echo "--- Auxiliary data-only dump ---"
sudo -u postgres pg_dump \
    -d trad_bot \
    --schema=research \
    --data-only \
    --no-owner \
    --no-privileges \
    --no-comments \
    | gzip > "${DATA_FILE}"

# ── 7. Export finished timestamp ──────────────────────────────
EXPORT_FINISHED_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# ── 8. Write metadata ─────────────────────────────────────────
cat > "${META_FILE}" <<EOF
=== RESEARCH SNAPSHOT EXPORT METADATA ===

export_started_at:  ${EXPORT_STARTED_UTC}
export_finished_at: ${EXPORT_FINISHED_UTC}
source_database:    trad_bot
postgresql_version: ${PG_VERSION}
hostname:           ${HOSTNAME}
git_head:           ${GIT_HEAD}
git_branch:         ${GIT_BRANCH}

--- Authoritative artifact ---
file:       research_full_${TIMESTAMP}.sql.gz
sha256:     ${DUMP_SHA256}
size:       ${DUMP_SIZE}

--- Table counts (at export time) ---
$(sudo -u postgres psql -d trad_bot -P pager=off -t -c "
SELECT 'research_experiment:  ' || COUNT(*) FROM research.research_experiment
UNION ALL
SELECT 'research_observation: ' || COUNT(*) FROM research.research_observation
UNION ALL
SELECT 'research_signal:      ' || COUNT(*) FROM research.research_signal
UNION ALL
SELECT 'research_outcome:     ' || COUNT(*) FROM research.research_outcome;
")

--- Signal time range (snapshot cutoff) ---
$(sudo -u postgres psql -d trad_bot -P pager=off -t -c "
SELECT 'signal_${SIGNAL_TIME_EXPR}_min: ' || MIN(${SIGNAL_TIME_EXPR})::text FROM research.research_signal
UNION ALL
SELECT 'signal_${SIGNAL_TIME_EXPR}_max: ' || MAX(${SIGNAL_TIME_EXPR})::text FROM research.research_signal;
")

--- Unique symbols ---
$(sudo -u postgres psql -d trad_bot -P pager=off -t -c "
SELECT 'unique_symbols: ' || COUNT(DISTINCT symbol) FROM research.research_observation;
")

--- Unique days ---
$(sudo -u postgres psql -d trad_bot -P pager=off -t -c "
SELECT 'unique_days: ' || COUNT(DISTINCT ${SIGNAL_TIME_EXPR}::date) FROM research.research_signal;
")
EOF

# ── 9. Final listing ──────────────────────────────────────────
echo ""
echo "--- Export files ---"
ls -lh "${EXPORT_DIR}/" | grep -E "research_|export_metadata"

echo ""
echo "=== EXPORT COMPLETE ==="
echo ""
echo "Authoritative artifact:"
echo "  ${DUMP_FILE}"
echo "  SHA-256: ${DUMP_SHA256}"
echo ""
echo "Transfer to local machine:"
echo "  scp root@<vps-host>:${DUMP_FILE} D:\\py_pro\\trad_bot\\research_snapshot\\"
echo "  scp root@<vps-host>:${META_FILE} D:\\py_pro\\trad_bot\\research_snapshot\\"
