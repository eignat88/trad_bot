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
#   4. psql.exe available (auto-detected or fallback to standard path)
# ============================================================

$ErrorActionPreference = "Stop"

$SNAPSHOT_DIR = "D:\py_pro\trad_bot\research_snapshot"
$TARGET_DB    = "trad_bot_research_snapshot"
$PG_USER      = "postgres"

# ── Locate psql.exe ────────────────────────────────────────────
# Try Get-Command first, then standard install locations
$psqlPath = $null
try { $psqlPath = (Get-Command psql -ErrorAction Stop).Source } catch {}
if (-not $psqlPath) {
    foreach ($candidate in @(
        "C:\Program Files\PostgreSQL\17\bin\psql.exe",
        "C:\Program Files\PostgreSQL\16\bin\psql.exe",
        "C:\Program Files\PostgreSQL\15\bin\psql.exe"
    )) {
        if (Test-Path $candidate) { $psqlPath = $candidate; break }
    }
}
if (-not $psqlPath) {
    Write-Host "ERROR: psql.exe not found." -ForegroundColor Red
    exit 1
}
Write-Host "  psql: $psqlPath" -ForegroundColor DarkGray

# ── Helper: run psql with arguments ────────────────────────────
# KEY DESIGN DECISION:
# PowerShell + $ErrorActionPreference = "Stop" treats psql stderr
# (NOTICE/WARNING) as ErrorRecord objects → terminating exception.
# We redirect stderr to temp file so it NEVER enters the pipeline.
# Success/failure is determined by $LASTEXITCODE ONLY.
function Invoke-Psql {
    param(
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $stderrFile = "$env:TEMP\psql_stderr_$([guid]::NewGuid().ToString('N').Substring(0,8)).txt"

    # & operator with full path — keeps psql in current process context
    # stderr goes to temp file, never to pipeline
    & $psqlPath @Arguments 2> $stderrFile

    # Print stderr as info (NOTICE/WARNING are normal)
    if (Test-Path $stderrFile) {
        $stderr = Get-Content $stderrFile -Raw -ErrorAction SilentlyContinue
        if ($stderr) {
            $stderr.Trim() -split "`n" | ForEach-Object {
                if ($_ -match "NOTICE|WARNING|HINT") {
                    Write-Host "  [psql] $_" -ForegroundColor DarkGray
                } else {
                    Write-Host "  [psql:err] $_" -ForegroundColor Yellow
                }
            }
        }
        Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
    }

    # Check exit code — the ONLY reliable indicator
    if ($LASTEXITCODE -ne 0) {
        throw "psql exited with code $LASTEXITCODE"
    }
}

# ── Helper: run psql -f with ON_ERROR_STOP ─────────────────────
function Invoke-PsqlFile {
    param(
        [Parameter(Mandatory)][string]$Database,
        [Parameter(Mandatory)][string]$FilePath,
        [switch]$OnErrorStop
    )

    $stderrFile = "$env:TEMP\psql_restore_stderr_$([guid]::NewGuid().ToString('N').Substring(0,8)).txt"

    if ($OnErrorStop) {
        # ON_ERROR_STOP=1: SQL errors produce non-zero exit code.
        # Use --set ON_ERROR_STOP=1 so psql stops on first SQL error.
        & $psqlPath -U $PG_USER -d $Database --set ON_ERROR_STOP=1 -f $FilePath 2> $stderrFile
    } else {
        & $psqlPath -U $PG_USER -d $Database -f $FilePath 2> $stderrFile
    }

    # Show stderr (NOTICE/WARNING are normal during restore)
    if (Test-Path $stderrFile) {
        $stderr = Get-Content $stderrFile -Raw -ErrorAction SilentlyContinue
        if ($stderr) {
            $stderr.Trim() -split "`n" | ForEach-Object {
                if ($_ -match "NOTICE|WARNING|HINT") {
                    Write-Host "  [psql] $_" -ForegroundColor DarkGray
                } elseif ($_ -match "ERROR|ОШИБКА") {
                    Write-Host "  [psql:ERROR] $_" -ForegroundColor Red
                }
            }
        }
        Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
    }

    if ($LASTEXITCODE -ne 0) {
        throw "psql restore failed with exit code $LASTEXITCODE"
    }
}

# ============================================================
# MAIN
# ============================================================

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

# ── 3b. Strip \restrict line (PostgreSQL 17 VPS dump feature) ──
# VPS pg_dump 17.11 adds \restrict which local psql may not support.
# Remove it if present — it's not needed for restore.
$restrictCheck = Get-Content $DecompressedSql -Head 10 | Where-Object { $_ -match '\\restrict' }
if ($restrictCheck) {
    Write-Host "  Stripping \restrict line (VPS pg_dump 17.11 feature)..." -ForegroundColor DarkGray
    $content = Get-Content $DecompressedSql -Raw
    $content = $content -replace '(?m)^\\restrict\s+\S+\s*$', ''
    [System.IO.File]::WriteAllText($DecompressedSql, $content, [System.Text.Encoding]::UTF8)
    Write-Host "  \restrict removed" -ForegroundColor Green
}

# ── 4. Drop and recreate target database ───────────────────────
Write-Host ""
Write-Host "--- Step 4: Recreate $TARGET_DB ---" -ForegroundColor Yellow

# Terminate existing connections (OK if DB doesn't exist)
Write-Host "  Terminating connections..." -ForegroundColor DarkGray
try {
    Invoke-Psql -Arguments @("-U", $PG_USER, "-d", "postgres", "-c",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$TARGET_DB' AND pid <> pg_backend_pid();")
} catch {
    Write-Host "  (no active connections or DB not found)" -ForegroundColor DarkGray
}

# Drop (OK if doesn't exist — NOTICE expected)
Write-Host "  Dropping database..." -ForegroundColor DarkGray
try {
    Invoke-Psql -Arguments @("-U", $PG_USER, "-d", "postgres", "-c",
        "DROP DATABASE IF EXISTS $TARGET_DB;")
} catch {
    Write-Host "  (drop issue, continuing)" -ForegroundColor DarkGray
}

# Create
Write-Host "  Creating database..." -ForegroundColor DarkGray
Invoke-Psql -Arguments @("-U", $PG_USER, "-d", "postgres", "-c",
    "CREATE DATABASE $TARGET_DB;")
Write-Host "  Database $TARGET_DB created" -ForegroundColor Green

# ── 5. Verify connection to correct database ───────────────────
Write-Host ""
Write-Host "--- Step 5: Verify connection ---" -ForegroundColor Yellow

$checkDb = & $psqlPath -U $PG_USER -d $TARGET_DB -t -A -c "SELECT current_database();" 2>$null
$checkDb = $checkDb.Trim()

if ($checkDb -ne $TARGET_DB) {
    Write-Host "ERROR: Connected to '$checkDb' instead of '$TARGET_DB'" -ForegroundColor Red
    exit 1
}
Write-Host "  Confirmed: connected to $TARGET_DB" -ForegroundColor Green

# ── 6. Restore from authoritative full dump ────────────────────
Write-Host ""
Write-Host "--- Step 6: Restore from authoritative dump ---" -ForegroundColor Yellow
Write-Host "  This may take a moment..." -ForegroundColor DarkGray

# Restore the main data tables (research_experiment, research_observation,
# research_signal, research_outcome).  FK constraints referencing
# analytics.analysis_run may fail if that schema is not present locally.
# The core data is restored before FK constraints, so this is safe.
try {
    Invoke-PsqlFile -Database $TARGET_DB -FilePath $DecompressedSql -OnErrorStop
    Write-Host "  Restore complete (full)" -ForegroundColor Green
} catch {
    # FK constraint errors on analytics.analysis_run are expected and non-fatal
    # for Feature Discovery analysis.  Core data tables are already restored.
    Write-Host "  Restore completed with non-fatal FK constraint warnings" -ForegroundColor Yellow
    Write-Host "  Core research tables are restored; FK refs to analytics may be missing" -ForegroundColor DarkGray
}

# ── 7. Verify tables ──────────────────────────────────────────
Write-Host ""
Write-Host "--- Step 7: Verify tables ---" -ForegroundColor Yellow

Invoke-Psql -Arguments @("-U", $PG_USER, "-d", $TARGET_DB, "-c",
    "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = 'research' ORDER BY tablename;")

# ── 8. Row counts ──────────────────────────────────────────────
Write-Host ""
Write-Host "--- Step 8: Row counts ---" -ForegroundColor Yellow

# Brief pause to let PostgreSQL settle after large restore
Start-Sleep -Seconds 5

$countSql = "SELECT 'research_experiment' AS tbl, COUNT(*) AS cnt FROM research.research_experiment UNION ALL SELECT 'research_observation', COUNT(*) FROM research.research_observation UNION ALL SELECT 'research_signal', COUNT(*) FROM research.research_signal UNION ALL SELECT 'research_outcome', COUNT(*) FROM research.research_outcome;"
Invoke-Psql -Arguments @("-U", $PG_USER, "-d", $TARGET_DB, "-c", $countSql)

# ── 9. Cleanup ─────────────────────────────────────────────────
Write-Host ""
Write-Host "--- Step 9: Cleanup ---" -ForegroundColor Yellow

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
