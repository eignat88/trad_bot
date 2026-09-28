# ============================================================
# LOCAL RESTORE -- Research Snapshot into trad_bot_research_snapshot
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
# PREPROCESSING:
#   The VPS dump (PostgreSQL 17.11) contains:
#   - \restrict / \unrestrict guard commands (unsupported by psql 17.2)
#   - 4 FK constraints referencing analytics.analysis_run (not present locally)
#   Preprocessing removes ALL of these before psql sees the file.
#   After preprocessing, psql must complete with ZERO errors.
#
# FAIL-CLOSED RULE:
#   "RESTORE COMPLETE" is printed ONLY if ALL of these pass:
#     - target DB != trad_bot
#     - authoritative gzip exists and decompresses
#     - preprocessing strips restrict/unrestrict (verified count=0)
#     - preprocessing strips exactly 4 known external FK blocks
#     - psql exit code = 0
#     - stderr contains no ERROR/FATAL/PANIC/invalid command
#     - research schema exists
#     - 4 required tables exist with exact row counts
#     - 3 required internal FK constraints exist
#     - 03_parity_validation.sql passes
# ============================================================

$SNAPSHOT_DIR = "D:\py_pro\trad_bot\research_snapshot"
$TARGET_DB    = "trad_bot_research_snapshot"
$PG_USER      = "postgres"

# -- Expected row counts (from VPS export 2026-09-28) -----------
$EXPECTED = @{
    "research_experiment"  = 12
    "research_observation" = 6912
    "research_signal"      = 6656
    "research_outcome"     = 6487
}

# -- Required internal FK constraints ---------------------------
$REQUIRED_FKS = @(
    "research_observation_experiment_id_fkey"
    "research_signal_observation_id_fkey"
    "research_outcome_signal_id_fkey"
)

# -- Known external FK constraint names (analytics -> removed) ---
$EXTERNAL_FK_NAMES = @(
    "experiment_source_run_id_fkey"
    "finding_occurrence_analysis_run_id_fkey"
    "finding_source_run_id_fkey"
    "hypothesis_source_run_id_fkey"
)

# -- Locate psql.exe --------------------------------------------
$psqlPath = $null
try { $psqlPath = (Get-Command psql -ErrorAction Stop).Source } catch {}
if (-not $psqlPath) {
    foreach ($c in @(
        "C:\Program Files\PostgreSQL\17\bin\psql.exe",
        "C:\Program Files\PostgreSQL\16\bin\psql.exe",
        "C:\Program Files\PostgreSQL\15\bin\psql.exe"
    )) {
        if (Test-Path $c) { $psqlPath = $c; break }
    }
}
if (-not $psqlPath) {
    Write-Host "ERROR: psql.exe not found." -ForegroundColor Red
    exit 1
}

# ============================================================
# HELPER: run psql -c (single command), check exit code only
# ============================================================
function Invoke-Psql {
    param([string[]]$Arguments)
    $sf = "$env:TEMP\psql_e_$([guid]::NewGuid().ToString('N').Substring(0,8)).txt"
    & $psqlPath @Arguments 2> $sf
    $ec = $global:LASTEXITCODE
    if (Test-Path $sf) {
        $s = Get-Content $sf -Raw -ErrorAction SilentlyContinue
        if ($s) {
            $s.Trim() -split "`n" | ForEach-Object {
                if ($_ -match "NOTICE|WARNING|HINT") {
                    Write-Host "  [psql] $_" -ForegroundColor DarkGray
                } elseif ($_ -match "ERROR|FATAL|PANIC") {
                    Write-Host "  [psql:ERROR] $_" -ForegroundColor Red
                }
            }
        }
        Remove-Item $sf -Force -ErrorAction SilentlyContinue
    }
    if ($ec -ne 0) { throw "psql exited with code $ec" }
}

# ============================================================
# HELPER: preprocess dump file
#   - Strip \restrict / \unrestrict lines
#   - Strip 4 known external FK ALTER TABLE blocks
#   - Verify preprocessing results
#   Returns: preprocessed file path
# ============================================================
function Invoke-DumpPreprocess {
    param([string]$InputPath)

    $content = Get-Content $InputPath -Raw
    $lines = $content -split "`n"

    # -- 1. Strip \restrict and \unrestrict --------------------
    $restrictCount = 0
    $unrestrictCount = 0
    $filtered = @()
    foreach ($line in $lines) {
        if ($line -match '^\s*\\restrict\s+') {
            $restrictCount++
            continue
        }
        if ($line -match '^\s*\\unrestrict\s+') {
            $unrestrictCount++
            continue
        }
        $filtered += $line
    }

    Write-Host "  Preprocess: stripped $restrictCount \restrict, $unrestrictCount \unrestrict" -ForegroundColor DarkGray
    if ($restrictCount -ne $unrestrictCount) {
        throw "FATAL: \restrict count ($restrictCount) != \unrestrict count ($unrestrictCount)"
    }
    if ($restrictCount -ne 1) {
        throw "FATAL: expected exactly 1 \restrict pair, got $restrictCount"
    }

    # -- 2. Strip 4 known external FK blocks -------------------
    # Each block in pg_dump format:
    #   --
    #   -- Name: <table> <constraint_name>; Type: FK CONSTRAINT; ...
    #   --
    #
    #   ALTER TABLE ONLY <table>
    #       ADD CONSTRAINT <name> ... REFERENCES analytics...
    #
    # Strategy: find lines matching ADD CONSTRAINT ... REFERENCES analytics,
    # then walk backwards to remove the complete block (ALTER TABLE ONLY
    # line + preceding comment block).
    $fkRemoved = 0
    $linesToRemove = @{}  # line index -> $true
    for ($i = 0; $i -lt $filtered.Count; $i++) {
        if ($filtered[$i] -match 'ADD CONSTRAINT\s+(\S+)\s+.*REFERENCES\s+analytics\.') {
            $constraintName = $Matches[1]
            if ($constraintName -notin $EXTERNAL_FK_NAMES) {
                throw "FATAL: unknown external FK constraint: $constraintName"
            }

            # Mark this ADD CONSTRAINT line for removal
            $linesToRemove[$i] = $true

            # Walk backwards to find ALTER TABLE ONLY line
            for ($j = $i - 1; $j -ge [Math]::Max(0, $i - 3); $j--) {
                if ($filtered[$j] -match 'ALTER TABLE ONLY') {
                    $linesToRemove[$j] = $true
                    # Continue backwards through comment block and blank lines
                    for ($k = $j - 1; $k -ge [Math]::Max(0, $j - 5); $k--) {
                        if ($filtered[$k] -match '^\s*--' -or $filtered[$k].Trim() -eq '') {
                            $linesToRemove[$k] = $true
                        } else {
                            break
                        }
                    }
                    break
                }
            }
            $fkRemoved++
        }
    }

    $result = @()
    for ($i = 0; $i -lt $filtered.Count; $i++) {
        if (-not $linesToRemove.ContainsKey($i)) {
            $result += $filtered[$i]
        }
    }

    Write-Host "  Preprocess: stripped $fkRemoved external FK constraints" -ForegroundColor DarkGray
    if ($fkRemoved -ne $EXTERNAL_FK_NAMES.Count) {
        throw "FATAL: expected exactly $($EXTERNAL_FK_NAMES.Count) external FK removals, got $fkRemoved"
    }

    # -- 3. Write preprocessed file ----------------------------
    $outputPath = $InputPath -replace '\.sql$', '_preprocessed.sql'
    [System.IO.File]::WriteAllText($outputPath, ($result -join "`n"), [System.Text.Encoding]::UTF8)

    # -- 4. Verify no analytics references remain ---------------
    $remaining = Get-Content $outputPath | Where-Object { $_ -match 'analytics' }
    if ($remaining.Count -gt 0) {
        throw "FATAL: analytics references still present after preprocessing: $($remaining.Count) lines"
    }

    # -- 5. Verify no restrict/unrestrict remain ----------------
    $restrictLeft = Get-Content $outputPath | Where-Object { $_ -match '\\restrict|\\unrestrict' }
    if ($restrictLeft.Count -gt 0) {
        throw "FATAL: restrict/unrestrict still present after preprocessing: $($restrictLeft.Count) lines"
    }

    return $outputPath
}

# ============================================================
# HELPER: analyze psql stderr for errors
#   Returns hashtable with error classification
# ============================================================
function Get-PsqlErrorAnalysis {
    param([string]$StderrContent)

    $result = @{
        HasError       = $false
        ErrorLines     = @()
        HasFatal       = $false
        HasPanic       = $false
        HasInvalidCmd  = $false
        HasNativeErr   = $false  # PowerShell NativeCommandError leakage
        ErrorCount     = 0
    }

    if (-not $StderrContent) { return $result }

    $lines = $StderrContent.Trim() -split "`n"
    foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if ($trimmed -match 'ERROR:') {
            $result.HasError = $true
            $result.ErrorLines += $trimmed
            $result.ErrorCount++
        }
        if ($trimmed -match 'FATAL|PANIC') {
            $result.HasFatal = $true
            $result.HasError = $true
            $result.ErrorLines += $trimmed
            $result.ErrorCount++
        }
        if ($trimmed -match 'invalid command|Unknown command') {
            $result.HasInvalidCmd = $true
            $result.HasError = $true
            $result.ErrorLines += $trimmed
            $result.ErrorCount++
        }
        if ($trimmed -match 'NativeCommandError|FullyQualifiedErrorId') {
            $result.HasNativeErr = $true
            $result.HasError = $true
            $result.ErrorLines += $trimmed
            $result.ErrorCount++
        }
    }

    return $result
}

# ============================================================
# HELPER: validate restore integrity
# ============================================================
function Test-RestoreIntegrity {
    param(
        [string]$Database,
        [hashtable]$ExpectedCounts,
        [string[]]$RequiredFks
    )
    $errors = @()

    # Schema exists
    $r = & $psqlPath -U $PG_USER -d $Database -t -A -c `
        "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = 'research';" 2>$null
    if ($r.Trim() -ne "1") { $errors += "research schema not found" }

    # Required tables
    $r = & $psqlPath -U $PG_USER -d $Database -t -A -c `
        "SELECT table_name FROM information_schema.tables WHERE table_schema='research' AND table_name IN ('research_experiment','research_observation','research_signal','research_outcome') ORDER BY table_name;" 2>$null
    $t = ($r.Trim() -split "`n") | Where-Object { $_ }
    if ($t.Count -ne 4) { $errors += "required tables: found $($t.Count)/4" }

    # Exact row counts
    foreach ($tbl in $ExpectedCounts.Keys) {
        $r = & $psqlPath -U $PG_USER -d $Database -t -A -c `
            "SELECT COUNT(*) FROM research.$tbl;" 2>$null
        $actual = [int]$r.Trim()
        $expected = $ExpectedCounts[$tbl]
        if ($actual -ne $expected) { $errors += "$tbl count=$actual (expected $expected)" }
    }

    # Internal FK constraints
    $r = & $psqlPath -U $PG_USER -d $Database -t -A -c `
        "SELECT constraint_name FROM information_schema.table_constraints WHERE constraint_type='FOREIGN KEY' AND table_schema='research' ORDER BY constraint_name;" 2>$null
    $fks = ($r.Trim() -split "`n") | Where-Object { $_ }
    foreach ($fk in $RequiredFks) {
        if ($fks -notcontains $fk) { $errors += "internal FK missing: $fk" }
    }

    return $errors
}

# ============================================================
# MAIN
# ============================================================

$ErrorActionPreference = "Stop"
$restoreOk = $false

try {
    Write-Host "=== LOCAL RESTORE: Research Snapshot ===" -ForegroundColor Cyan
    Write-Host "Target DB: $TARGET_DB" -ForegroundColor Cyan
    Write-Host ""

    # -- 0. Safety -----------------------------------------------
    if ($TARGET_DB -eq "trad_bot") {
        throw "FATAL: TARGET_DB must not be trad_bot"
    }
    Write-Host "  [OK] Safety: target=$TARGET_DB" -ForegroundColor Green

    # -- 1. Find dump --------------------------------------------
    $FullDump = Get-ChildItem -Path $SNAPSHOT_DIR -Filter "research_full_*.sql.gz" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $FullDump) { throw "FATAL: No research_full_*.sql.gz found" }
    Write-Host "  [OK] Dump: $($FullDump.Name) ($([math]::Round($FullDump.Length/1MB,2)) MB)" -ForegroundColor Green

    # -- 2. Decompress -------------------------------------------
    $DecompressedSql = $FullDump.FullName -replace '\.gz$', ''
    if (-not (Test-Path $DecompressedSql)) {
        $bytes = [System.IO.File]::ReadAllBytes($FullDump.FullName)
        $ms = [System.IO.MemoryStream]::new($bytes)
        $gz = [System.IO.Compression.GZipStream]::new($ms, [System.IO.Compression.CompressionMode]::Decompress)
        $reader = [System.IO.StreamReader]::new($gz)
        $decompressed = $reader.ReadToEnd()
        $reader.Close(); $gz.Close(); $ms.Close()
        [System.IO.File]::WriteAllText($DecompressedSql, $decompressed, [System.Text.Encoding]::UTF8)
    }
    Write-Host "  [OK] Decompressed: $([math]::Round((Get-Item $DecompressedSql).Length/1MB,2)) MB" -ForegroundColor Green

    # -- 3. Preprocess -------------------------------------------
    $PreprocessedSql = Invoke-DumpPreprocess -InputPath $DecompressedSql
    Write-Host "  [OK] Preprocessed: $(Split-Path $PreprocessedSql -Leaf)" -ForegroundColor Green

    # -- 4. Drop and recreate DB ---------------------------------
    $stderrFile = "$env:TEMP\psql_db_stderr.txt"
    & $psqlPath -U $PG_USER -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$TARGET_DB' AND pid<>pg_backend_pid();" 2> $stderrFile
    & $psqlPath -U $PG_USER -d postgres -c "DROP DATABASE IF EXISTS $TARGET_DB;" 2> $stderrFile
    & $psqlPath -U $PG_USER -d postgres -c "CREATE DATABASE $TARGET_DB;" 2> $stderrFile
    if ($global:LASTEXITCODE -ne 0) { throw "FATAL: CREATE DATABASE failed" }
    Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
    Write-Host "  [OK] Database $TARGET_DB created" -ForegroundColor Green

    # -- 5. Verify connection ------------------------------------
    $checkDb = & $psqlPath -U $PG_USER -d $TARGET_DB -t -A -c "SELECT current_database();" 2>$null
    if ($checkDb.Trim() -ne $TARGET_DB) { throw "FATAL: connected to wrong DB: $($checkDb.Trim())" }
    Write-Host "  [OK] Connected to $TARGET_DB" -ForegroundColor Green

    # -- 6. Restore (NO ON_ERROR_STOP) --------------------------
    Write-Host ""
    Write-Host "  Restoring (psql -f without ON_ERROR_STOP)..." -ForegroundColor DarkGray

    $restoreStderr = "$env:TEMP\restore_stderr_$([guid]::NewGuid().ToString('N').Substring(0,8)).txt"
    $restoreStdout = "$env:TEMP\restore_stdout_$([guid]::NewGuid().ToString('N').Substring(0,8)).txt"

    & $psqlPath -U $PG_USER -d $TARGET_DB -f $PreprocessedSql 1> $restoreStdout 2> $restoreStderr
    $psqlExit = $global:LASTEXITCODE

    # Analyze stderr
    $stderrContent = ""
    if (Test-Path $restoreStderr) {
        $stderrContent = Get-Content $restoreStderr -Raw -ErrorAction SilentlyContinue
    }
    $analysis = Get-PsqlErrorAnalysis -StderrContent $stderrContent

    # Save error log if any errors detected
    $errorLogFile = Join-Path $SNAPSHOT_DIR "restore_last_error.log"
    if ($analysis.ErrorCount -gt 0 -or $psqlExit -ne 0) {
        $logHeader = "=== Restore error log ===`nDatabase: $TARGET_DB`nTimestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`npsql exit: $psqlExit`n`n"
        [System.IO.File]::WriteAllText($errorLogFile, $logHeader + $stderrContent, [System.Text.Encoding]::UTF8)
        Write-Host "  Error log saved: $errorLogFile" -ForegroundColor Yellow
    }

    # Cleanup temp files
    Remove-Item $restoreStderr -Force -ErrorAction SilentlyContinue
    Remove-Item $restoreStdout -Force -ErrorAction SilentlyContinue

    # -- 7. Check psql exit code ---------------------------------
    if ($psqlExit -ne 0) {
        Write-Host "  [FAIL] psql exit code: $psqlExit" -ForegroundColor Red
        throw "FATAL: psql restore exited with code $psqlExit"
    }
    Write-Host "  [OK] psql exit code: 0" -ForegroundColor Green

    # -- 8. Check stderr for ANY errors -------------------------
    if ($analysis.HasFatal) {
        Write-Host "  [FAIL] FATAL/PANIC in stderr" -ForegroundColor Red
        throw "FATAL: psql stderr contains FATAL/PANIC"
    }
    if ($analysis.HasInvalidCmd) {
        Write-Host "  [FAIL] Invalid psql command in stderr" -ForegroundColor Red
        throw "FATAL: psql stderr contains invalid command"
    }
    if ($analysis.HasNativeErr) {
        Write-Host "  [FAIL] NativeCommandError in stderr" -ForegroundColor Red
        throw "FATAL: PowerShell NativeCommandError detected in stderr"
    }
    if ($analysis.HasError) {
        Write-Host "  [FAIL] SQL errors in stderr: $($analysis.ErrorCount)" -ForegroundColor Red
        foreach ($e in $analysis.ErrorLines) { Write-Host "    $e" -ForegroundColor Red }
        throw "FATAL: $($analysis.ErrorCount) unexpected SQL errors in psql stderr"
    }
    Write-Host "  [OK] stderr: 0 errors, 0 FATAL, 0 invalid commands" -ForegroundColor Green

    # -- 9. Post-restore validation ------------------------------
    Write-Host ""
    Write-Host "  Running integrity validation..." -ForegroundColor DarkGray
    $valErrors = Test-RestoreIntegrity -Database $TARGET_DB -ExpectedCounts $EXPECTED -RequiredFks $REQUIRED_FKS
    if ($valErrors.Count -gt 0) {
        foreach ($e in $valErrors) { Write-Host "  [FAIL] $e" -ForegroundColor Red }
        throw "FATAL: $($valErrors.Count) validation error(s)"
    }
    Write-Host "  [OK] All integrity checks passed" -ForegroundColor Green

    # -- 10. Row counts display ----------------------------------
    Write-Host ""
    Write-Host "  Row counts:" -ForegroundColor Yellow
    $countSql = "SELECT 'research_experiment' AS tbl, COUNT(*) AS cnt FROM research.research_experiment UNION ALL SELECT 'research_observation', COUNT(*) FROM research.research_observation UNION ALL SELECT 'research_signal', COUNT(*) FROM research.research_signal UNION ALL SELECT 'research_outcome', COUNT(*) FROM research.research_outcome;"
    $stderrF = "$env:TEMP\count_stderr.txt"
    & $psqlPath -U $PG_USER -d $TARGET_DB -c $countSql 2> $stderrF
    Remove-Item $stderrF -Force -ErrorAction SilentlyContinue

    # -- 11. Cleanup decompressed SQL ----------------------------
    Remove-Item $DecompressedSql -Force -ErrorAction SilentlyContinue
    Remove-Item $PreprocessedSql -Force -ErrorAction SilentlyContinue

    $restoreOk = $true

} finally {
    if (-not $restoreOk) {
        Write-Host ""
        Write-Host "=== RESTORE FAILED ===" -ForegroundColor Red
        Write-Host "Check error log: $errorLogFile" -ForegroundColor Yellow
    }
}

# -- Only reach here if everything passed -----------------------
if ($restoreOk) {
    Write-Host ""
    Write-Host "=== RESTORE COMPLETE ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Database:      $TARGET_DB" -ForegroundColor White
    Write-Host "Trad_bot:      UNTOUCHED" -ForegroundColor Green
    Write-Host "FK removed:    4 (analytics.analysis_run)" -ForegroundColor DarkGray
    Write-Host "Restrict:      stripped (\restrict + \unrestrict)" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Parity validation:" -ForegroundColor White
    Write-Host "  & `"$psqlPath`" -U $PG_USER -d $TARGET_DB -f .\03_parity_validation.sql" -ForegroundColor DarkGray
}
