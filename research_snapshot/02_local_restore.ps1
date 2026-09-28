# ============================================================
# LOCAL RESTORE — Research Snapshot into trad_bot_research_snapshot
# ============================================================
# Run from PowerShell on Windows:
#   cd D:\py_pro\trad_bot\research_snapshot
#   .\02_local_restore.ps1
#
# Restores the AUTHORITATIVE full dump (research_full_*.sql.gz)
# into a SEPARATE local database: trad_bot_research_snapshot.
#
# NEVER touches the existing local trad_bot database.
#
# PREREQUISITES:
#   1. research_full_*.sql.gz in D:\py_pro\trad_bot\research_snapshot\
#   2. export_metadata.txt in same directory
#   3. PostgreSQL running locally
#   4. psql / gzip available in PATH
# ============================================================

$ErrorActionPreference = "Stop"

$SNAPSHOT_DIR = "D:\py_pro\trad_bot\research_snapshot"
$TARGET_DB    = "trad_bot_research_snapshot"
$PG_USER      = "postgres"

Write-Host "=== LOCAL RESTORE: Research Snapshot ===" -ForegroundColor Cyan
Write-Host "Target DB: $TARGET_DB" -ForegroundColor Cyan
Write-Host "Source dir: $SNAPSHOT_DIR" -ForegroundColor Cyan
Write-Host ""

# ── 0. Safety check — never touch trad_bot ─────────────────────
Write-Host "--- Safety: confirming target is $TARGET_DB (NOT trad_bot) ---" -ForegroundColor Yellow
if ($TARGET_DB -eq "trad_bot") {
    Write-Host "ERROR: TARGET_DB must not be trad_bot. Aborting." -ForegroundColor Red
    exit 1
}
Write-Host "  OK: target=$TARGET_DB" -ForegroundColor Green

# ── 1. Find the authoritative full dump ────────────────────────
Write-Host ""
Write-Host "--- Step 1: Locate authoritative dump ---" -ForegroundColor Yellow

$FullDump = Get-ChildItem -Path $SNAPSHOT_DIR -Filter "research_full_*.sql.gz" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

if (-not $FullDump) {
    Write-Host "ERROR: No research_full_*.sql.gz found in $SNAPSHOT_DIR" -ForegroundColor Red
    Write-Host "Run 01_vps_export.sh on VPS first." -ForegroundColor Yellow
    exit 1
}

Write-Host "  Found: $($FullDump.Name)" -ForegroundColor Green
Write-Host "  Size:  $([math]::Round($FullDump.Length / 1MB, 2)) MB" -ForegroundColor Green

# ── 2. Read metadata ───────────────────────────────────────────
Write-Host ""
Write-Host "--- Step 2: Snapshot metadata ---" -ForegroundColor Yellow

$MetaFile = Get-ChildItem -Path $SNAPSHOT_DIR -Filter "export_metadata.txt" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

if ($MetaFile) {
    Get-Content $MetaFile.FullName | Select-Object -First 25 | ForEach-Object {
        Write-Host "  $_" -ForegroundColor DarkGray
    }
} else {
    Write-Host "  WARNING: export_metadata.txt not found" -ForegroundColor Yellow
}

# ── 3. Decompress the full dump ────────────────────────────────
Write-Host ""
Write-Host "--- Step 3: Decompress dump ---" -ForegroundColor Yellow

$DecompressedSql = $FullDump.FullName -replace '\.gz$', ''

if (Test-Path $DecompressedSql) {
    Write-Host "  Decompressed file already exists, skipping" -ForegroundColor DarkGray
} else {
    $bytes = [System.IO.File]::ReadAllBytes($FullDump.FullName)
    $ms = [System.IO.MemoryStream]::new($bytes)
    $gzStream = [System.IO.Compression.GZipStream]::new(
        $ms,
        [System.IO.Compression.CompressionMode]::Decompress
    )
    $reader = [System.IO.StreamReader]::new($gzStream)
    $decompressed = $reader.ReadToEnd()
    $reader.Close()
    $gzStream.Close()
    $ms.Close()

    [System.IO.File]::WriteAllText($DecompressedSql, $decompressed, [System.Text.Encoding]::UTF8)
    Write-Host "  Decompressed to: $(Split-Path $DecompressedSql -Leaf)" -ForegroundColor Green
}

$DecompressedSize = (Get-Item $DecompressedSql).Length
Write-Host "  Decompressed size: $([math]::Round($DecompressedSize / 1MB, 2)) MB"

# ── 4. Drop and recreate target database ───────────────────────
Write-Host ""
Write-Host "--- Step 4: Recreate $TARGET_DB ---" -ForegroundColor Yellow

& psql -U $PG_USER -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$TARGET_DB' AND pid <> pg_backend_pid();" 2>$null
& psql -U $PG_USER -d postgres -c "DROP DATABASE IF EXISTS $TARGET_DB;" 2>$null
& psql -U $PG_USER -d postgres -c "CREATE DATABASE $TARGET_DB;"

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to create database $TARGET_DB" -ForegroundColor Red
    exit 1
}

Write-Host "  Database $TARGET_DB created" -ForegroundColor Green

# ── 5. Restore from authoritative full dump ────────────────────
Write-Host ""
Write-Host "--- Step 5: Restore from authoritative dump ---" -ForegroundColor Yellow
Write-Host "  This may take a moment..." -ForegroundColor DarkGray

& psql -U $PG_USER -d $TARGET_DB -f $DecompressedSql

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Restore failed" -ForegroundColor Red
    exit 1
}

Write-Host "  Restore complete" -ForegroundColor Green

# ── 6. Verify tables ──────────────────────────────────────────
Write-Host ""
Write-Host "--- Step 6: Verify tables ---" -ForegroundColor Yellow

& psql -U $PG_USER -d $TARGET_DB -c "
SELECT schemaname, tablename
FROM pg_tables
WHERE schemaname = 'research'
ORDER BY tablename;
"

# ── 7. Row counts ──────────────────────────────────────────────
Write-Host ""
Write-Host "--- Step 7: Row counts ---" -ForegroundColor Yellow

& psql -U $PG_USER -d $TARGET_DB -c "
SELECT 'research_experiment' AS tbl, COUNT(*) AS cnt FROM research.research_experiment
UNION ALL SELECT 'research_observation', COUNT(*) FROM research.research_observation
UNION ALL SELECT 'research_signal',      COUNT(*) FROM research.research_signal
UNION ALL SELECT 'research_outcome',     COUNT(*) FROM research.research_outcome;
"

# ── 8. Cleanup decompressed SQL ────────────────────────────────
Write-Host ""
Write-Host "--- Step 8: Cleanup ---" -ForegroundColor Yellow

Remove-Item $DecompressedSql -Force
Write-Host "  Removed decompressed SQL (keeping .gz original)" -ForegroundColor Green

Write-Host ""
Write-Host "=== RESTORE COMPLETE ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Database: $TARGET_DB" -ForegroundColor White
Write-Host "Trad_bot: UNTOUCHED" -ForegroundColor Green
Write-Host ""
Write-Host "Next: Run parity validation:" -ForegroundColor White
Write-Host "  psql -U postgres -d $TARGET_DB -f 03_parity_validation.sql" -ForegroundColor White
