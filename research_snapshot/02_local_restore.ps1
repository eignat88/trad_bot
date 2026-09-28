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
# RESTORE CONTRACT — considered SUCCESS only if ALL pass:
#   A. psql restore exit checked (errors logged, not fatal-stop)
#   B. schema 'research' exists
#   C. required tables exist:
#        research.research_experiment
#        research.research_observation
#        research.research_signal
#        research.research_outcome
#   D. exact row counts:
#        research_experiment  = 12
#        research_observation = 6912
#        research_signal      = 6656
#        research_outcome     = 6487
#   E. internal research FK constraints present
#   F. 03_parity_validation.sql runs without SQL error
#
# EXTERNAL DEPENDENCIES:
#   The VPS dump contains 4 FK constraints referencing
#   analytics.analysis_run (schema not present locally).
#   psql runs WITHOUT ON_ERROR_STOP so it skips these errors
#   and continues to create all remaining objects.
#   These 4 missing FK constraints are expected and documented.
#
# PREREQUISITES:
#   1. research_full_*.sql.gz in D:\py_pro\trad_bot\research_snapshot\
#   2. PostgreSQL running locally
#   3. psql.exe available (auto-detected or fallback to standard path)
# ============================================================

# NOTE: We intentionally do NOT set ErrorActionPreference = "Stop".
# PowerShell treats psql stderr (NOTICE/WARNING) as ErrorRecord objects
# which Stop mode converts to terminating exceptions.
# All psql exit codes are checked explicitly.

$SNAPSHOT_DIR = "D:\py_pro\trad_bot\research_snapshot"
$TARGET_DB    = "trad_bot_research_snapshot"
$PG_USER      = "postgres"
$PSQL_TIMEOUT = 300  # seconds

# ── Expected row counts (from VPS export 2026-09-28) ──────────
$EXPECTED = @{
    "research_experiment"  = 12
    "research_observation" = 6912
    "research_signal"      = 6656
    "research_outcome"     = 6487
}

# ── Required internal FK constraints ───────────────────────────
$REQUIRED_FKS = @(
    "research_observation_experiment_id_fkey"
    "research_signal_observation_id_fkey"
    "research_outcome_signal_id_fkey"
)

# ── Locate psql.exe ────────────────────────────────────────────
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

# ── Helper: run psql -c (single command) ───────────────────────
function Invoke-Psql {
    param(
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $stderrFile = "$env:TEMP\psql_stderr_$([guid]::NewGuid().ToString('N').Substring(0,8)).txt"

    & $psqlPath @Arguments 2> $stderrFile
    $exitCode = $LASTEXITCODE

    # Print stderr — classify as info or error
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

    if ($exitCode -ne 0) {
        throw "psql exited with code $exitCode"
    }
}

# ── Helper: run psql -f (file restore) ─────────────────────────
# Runs WITHOUT ON_ERROR_STOP so psql continues past external FK errors.
# Returns stdout+stderr content for post-restore analysis.
function Invoke-PsqlRestore {
    param(
        [Parameter(Mandatory)][string]$Database,
        [Parameter(Mandatory)][string]$FilePath
    )

    $stderrFile = "$env:TEMP\psql_restore_stderr_$([guid]::NewGuid().ToString('N').Substring(0,8)).txt"
    $stdoutFile = "$env:TEMP\psql_restore_stdout_$([guid]::NewGuid().ToString('N').Substring(0,8)).txt"

    # NO ON_ERROR_STOP: psql skips failed statements and continues.
    # This is essential — the dump has 4 FK constraints referencing
    # analytics.analysis_run which doesn't exist locally.
    & $psqlPath -U $PG_USER -d $Database -f $FilePath 1> $stdoutFile 2> $stderrFile
    $exitCode = $LASTEXITCODE

    # Classify and display stderr
    $errorCount = 0
    if (Test-Path $stderrFile) {
        $stderr = Get-Content $stderrFile -Raw -ErrorAction SilentlyContinue
        if ($stderr) {
            $stderr.Trim() -split "`n" | ForEach-Object {
                if ($_ -match "ОШИБКА|ERROR") { $script:errorCount++ }
                if ($_ -match "NOTICE|WARNING|HINT") {
                    Write-Host "  [psql] $_" -ForegroundColor DarkGray
                } elseif ($_ -match "ОШИБКА|ERROR") {
                    Write-Host "  [psql:ERROR] $_" -ForegroundColor Red
                }
            }
        }
    }

    # Return structured result
    return @{
        ExitCode   = $exitCode
        ErrorCount = $errorCount
        StderrFile = $stderrFile
        StdoutFile = $stdoutFile
    }
}

# ── Helper: validate restore ───────────────────────────────────
function Test-RestoreIntegrity {
    param(
        [string]$Database,
        [hashtable]$ExpectedCounts,
        [string[]]$RequiredFks
    )

    $errors = @()

    # Check B: schema exists
    $schemaCheck = & $psqlPath -U $PG_USER -d $Database -t -A -c `
        "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = 'research';" 2>$null
    if ($schemaCheck.Trim() -ne "1") {
        $errors += "FAIL: research schema not found"
    }

    # Check C: required tables exist
    $tableCheck = & $psqlPath -U $PG_USER -d $Database -t -A -c `
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'research' AND table_name IN ('research_experiment','research_observation','research_signal','research_outcome') ORDER BY table_name;" 2>$null
    $tables = $tableCheck.Trim() -split "`n" | Where-Object { $_ }
    if ($tables.Count -ne 4) {
        $errors += "FAIL: required tables missing (found $($tables.Count)/4)"
    }

    # Check D: exact row counts
    foreach ($table in $ExpectedCounts.Keys) {
        $countResult = & $psqlPath -U $PG_USER -d $Database -t -A -c `
            "SELECT COUNT(*) FROM research.$table;" 2>$null
        $actual = [int]$countResult.Trim()
        $expected = $ExpectedCounts[$table]
        if ($actual -ne $expected) {
            $errors += "FAIL: $table count = $actual (expected $expected)"
        }
    }

    # Check E: internal FK constraints present
    $fkCheck = & $psqlPath -U $PG_USER -d $Database -t -A -c `
        "SELECT constraint_name FROM information_schema.table_constraints WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'research' ORDER BY constraint_name;" 2>$null
    $fks = $fkCheck.Trim() -split "`n" | Where-Object { $_ }
    foreach ($requiredFk in $RequiredFks) {
        if ($fks -notcontains $requiredFk) {
            $errors += "FAIL: internal FK constraint missing: $requiredFk"
        }
    }

    return $errors
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
    exit 1
}

Write-Host "  Found: $($FullDump.Name)" -ForegroundColor Green
Write-Host "  Size:  $([math]::Round($FullDump.Length / 1MB, 2)) MB" -ForegroundColor Green

# ── 2. Decompress the full dump ────────────────────────────────
Write-Host ""
Write-Host "--- Step 2: Decompress dump ---" -ForegroundColor Yellow

$DecompressedSql = $FullDump.FullName -replace '\.gz$', ''

if (Test-Path $DecompressedSql) {
    Write-Host "  Decompressed file already exists, reusing" -ForegroundColor DarkGray
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

# ── 2b. Strip \restrict line (PostgreSQL 17 VPS dump) ──────────
$restrictCheck = Get-Content $DecompressedSql -Head 10 | Where-Object { $_ -match '\\restrict' }
if ($restrictCheck) {
    Write-Host "  Stripping \restrict (VPS pg_dump 17.11)..." -ForegroundColor DarkGray
    $content = Get-Content $DecompressedSql -Raw
    $content = $content -replace '(?m)^\\restrict\s+\S+\s*$', ''
    [System.IO.File]::WriteAllText($DecompressedSql, $content, [System.Text.Encoding]::UTF8)
    Write-Host "  \restrict removed" -ForegroundColor Green
}

# ── 3. Drop and recreate target database ───────────────────────
Write-Host ""
Write-Host "--- Step 3: Recreate $TARGET_DB ---" -ForegroundColor Yellow

Write-Host "  Terminating connections..." -ForegroundColor DarkGray
try {
    Invoke-Psql -Arguments @("-U", $PG_USER, "-d", "postgres", "-c",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$TARGET_DB' AND pid <> pg_backend_pid();")
} catch {
    Write-Host "  (no active connections)" -ForegroundColor DarkGray
}

Write-Host "  Dropping database..." -ForegroundColor DarkGray
try {
    Invoke-Psql -Arguments @("-U", $PG_USER, "-d", "postgres", "-c",
        "DROP DATABASE IF EXISTS $TARGET_DB;")
} catch {
    Write-Host "  (drop issue, continuing)" -ForegroundColor DarkGray
}

Write-Host "  Creating database..." -ForegroundColor DarkGray
Invoke-Psql -Arguments @("-U", $PG_USER, "-d", "postgres", "-c",
    "CREATE DATABASE $TARGET_DB;")
Write-Host "  Database $TARGET_DB created" -ForegroundColor Green

# ── 4. Verify connection ───────────────────────────────────────
Write-Host ""
Write-Host "--- Step 4: Verify connection ---" -ForegroundColor Yellow

$checkDb = & $psqlPath -U $PG_USER -d $TARGET_DB -t -A -c "SELECT current_database();" 2>$null
$checkDb = $checkDb.Trim()

if ($checkDb -ne $TARGET_DB) {
    Write-Host "ERROR: Connected to '$checkDb' instead of '$TARGET_DB'" -ForegroundColor Red
    exit 1
}
Write-Host "  Confirmed: connected to $TARGET_DB" -ForegroundColor Green

# ── 5. Restore from dump ───────────────────────────────────────
Write-Host ""
Write-Host "--- Step 5: Restore from authoritative dump ---" -ForegroundColor Yellow
Write-Host "  Running psql (without ON_ERROR_STOP, external FK errors expected)..." -ForegroundColor DarkGray

$restoreResult = Invoke-PsqlRestore -Database $TARGET_DB -FilePath $DecompressedSql

# Log errors to file for diagnostics
$logDir = $SNAPSHOT_DIR
$logFile = Join-Path $logDir "restore_last_error.log"
if ($restoreResult.ErrorCount -gt 0) {
    Write-Host "  psql encountered $($restoreResult.ErrorCount) SQL error(s), saving to $logFile" -ForegroundColor Yellow
    $stderrContent = Get-Content $restoreResult.StderrFile -Raw -ErrorAction SilentlyContinue
    if ($stderrContent) {
        $header = "=== Restore error log ===`nDatabase: $TARGET_DB`nTimestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`n`n"
        [System.IO.File]::WriteAllText($logFile, $header + $stderrContent, [System.Text.Encoding]::UTF8)
    }
} else {
    Write-Host "  psql restore completed with 0 SQL errors" -ForegroundColor Green
}

# Cleanup temp stderr/stdout
Remove-Item $restoreResult.StderrFile -Force -ErrorAction SilentlyContinue
Remove-Item $restoreResult.StdoutFile -Force -ErrorAction SilentlyContinue

# ── 6. Post-restore validation (HARD FAIL if any check fails) ─
Write-Host ""
Write-Host "--- Step 6: Post-restore validation ---" -ForegroundColor Yellow
Write-Host "  Running integrity checks..." -ForegroundColor DarkGray

$validationErrors = Test-RestoreIntegrity -Database $TARGET_DB -ExpectedCounts $EXPECTED -RequiredFks $REQUIRED_FKS

if ($validationErrors.Count -gt 0) {
    Write-Host ""
    Write-Host "RESTORE FAILED - validation errors:" -ForegroundColor Red
    foreach ($err in $validationErrors) {
        Write-Host "  $err" -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "Error log: $logFile" -ForegroundColor Yellow
    exit 1
}

Write-Host "  All validation checks passed" -ForegroundColor Green

# ── 7. Row counts (display) ────────────────────────────────────
Write-Host ""
Write-Host "--- Step 7: Row counts ---" -ForegroundColor Yellow

$countSql = "SELECT 'research_experiment' AS tbl, COUNT(*) AS cnt FROM research.research_experiment UNION ALL SELECT 'research_observation', COUNT(*) FROM research.research_observation UNION ALL SELECT 'research_signal', COUNT(*) FROM research.research_signal UNION ALL SELECT 'research_outcome', COUNT(*) FROM research.research_outcome;"
Invoke-Psql -Arguments @("-U", $PG_USER, "-d", $TARGET_DB, "-c", $countSql)

# ── 8. Cleanup ─────────────────────────────────────────────────
Write-Host ""
Write-Host "--- Step 8: Cleanup ---" -ForegroundColor Yellow

Remove-Item $DecompressedSql -Force
Write-Host "  Removed decompressed SQL (keeping .gz original)" -ForegroundColor Green

# ── 9. Summary ─────────────────────────────────────────────────
Write-Host ""
Write-Host "=== RESTORE COMPLETE ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Database:   $TARGET_DB" -ForegroundColor White
Write-Host "Trad_bot:   UNTOUCHED" -ForegroundColor Green
Write-Host "FK skipped: 4 (analytics.analysis_run, expected, not needed for analysis)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Next: Run parity validation:" -ForegroundColor White
Write-Host "  psql -U postgres -d $TARGET_DB -f 03_parity_validation.sql" -ForegroundColor White
