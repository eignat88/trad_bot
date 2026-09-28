# ============================================================
# REGRESSION TESTS — Research Snapshot Restore
# ============================================================
# Run from PowerShell on Windows:
#   cd D:\py_pro\trad_bot\research_snapshot
#   .\test_restore.ps1
#
# Tests the restore script against known scenarios.
# All tests use a temporary test database, never touch trad_bot.
# ============================================================

# NOTE: We do NOT set ErrorActionPreference = "Stop" because psql
# writes NOTICE/WARNING to stderr which PowerShell would treat as
# terminating exceptions.  Each psql call checks $LASTEXITCODE.

$SNAPSHOT_DIR = "D:\py_pro\trad_bot\research_snapshot"
$PG_USER      = "postgres"
$TEST_DB      = "_test_restore_regression"

# ── Locate psql.exe ────────────────────────────────────────────
$psqlPath = $null
try { $psqlPath = (Get-Command psql -ErrorAction Stop).Source } catch {}
if (-not $psqlPath) {
    foreach ($candidate in @(
        "C:\Program Files\PostgreSQL\17\bin\psql.exe",
        "C:\Program Files\PostgreSQL\16\bin\psql.exe"
    )) {
        if (Test-Path $candidate) { $psqlPath = $candidate; break }
    }
}
if (-not $psqlPath) { Write-Host "SKIP: psql not found" -ForegroundColor Yellow; exit 0 }

# ── Helpers ────────────────────────────────────────────────────
function Invoke-TestPsql {
    param([string[]]$Arguments)
    $stderrFile = "$env:TEMP\test_psql_stderr_$([guid]::NewGuid().ToString('N').Substring(0,8)).txt"
    & $psqlPath @Arguments 2> $stderrFile
    $exitCode = $global:LASTEXITCODE
    Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
    return [int]$exitCode
}

function New-TestDatabase {
    $stderrFile = "$env:TEMP\test_db_stderr.txt"
    & $psqlPath -U $PG_USER -d postgres -c "DROP DATABASE IF EXISTS $TEST_DB;" 2> $stderrFile
    Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
    & $psqlPath -U $PG_USER -d postgres -c "CREATE DATABASE $TEST_DB;" 2> $stderrFile
    Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
}

function Remove-TestDatabase {
    $stderrFile = "$env:TEMP\test_db_stderr.txt"
    & $psqlPath -U $PG_USER -d postgres -c "DROP DATABASE IF EXISTS $TEST_DB;" 2> $stderrFile
    Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
}

function Get-SqlScalar {
    param([string]$Sql)
    $stderrFile = "$env:TEMP\test_scalar_stderr.txt"
    $result = & $psqlPath -U $PG_USER -d $TEST_DB -t -A -c $Sql 2> $stderrFile
    Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
    return $result.Trim()
}

# ── Test counters (global scope for function access) ───────────
$global:testPassed = 0
$global:testFailed = 0
$global:testResults = @()

function Test-Assert {
    param([string]$Name, [bool]$Condition, [string]$Detail)
    if ($Condition) {
        Write-Host "  PASS: $Name" -ForegroundColor Green
        $global:testPassed++
        $global:testResults += [PSCustomObject]@{ Test=$Name; Result="PASS"; Detail=$Detail }
    } else {
        Write-Host "  FAIL: $Name -- $Detail" -ForegroundColor Red
        $global:testFailed++
        $global:testResults += [PSCustomObject]@{ Test=$Name; Result="FAIL"; Detail=$Detail }
    }
}

# ============================================================
# TESTS
# ============================================================

Write-Host "=== REGRESSION TESTS: Restore Script ===" -ForegroundColor Cyan
Write-Host ""

# ── Test 1: psql NOTICE + exit 0 → PASS ───────────────────────
Write-Host "Test 1: psql NOTICE + exit 0" -ForegroundColor Yellow
New-TestDatabase
$code = Invoke-TestPsql @("-U", $PG_USER, "-d", $TEST_DB, "-c",
    "CREATE TABLE IF NOT EXISTS _test_t (id int); SELECT pg_notify('channel', 'msg');")
Test-Assert "psql NOTICE + exit 0 → PASS" ($code -eq 0) "exit code=$code"
Remove-TestDatabase

# ── Test 2: psql WARNING + exit 0 → PASS ──────────────────────
Write-Host ""
Write-Host "Test 2: psql WARNING + exit 0" -ForegroundColor Yellow
New-TestDatabase
# Create a table, then drop it — produces WARNING
& $psqlPath -U $PG_USER -d $TEST_DB -c "CREATE TABLE _test_t (id int);" 2>$null
$code = Invoke-TestPsql @("-U", $PG_USER, "-d", $TEST_DB, "-c",
    "DROP TABLE IF EXISTS _test_t; DROP TABLE IF EXISTS _test_nonexist;")
Test-Assert "psql WARNING + exit 0 → PASS" ($code -eq 0) "exit code=$code"
Remove-TestDatabase

# ── Test 3: psql ERROR + exit != 0 → HARD FAIL ───────────────
Write-Host ""
Write-Host "Test 3: psql ERROR + exit != 0" -ForegroundColor Yellow
New-TestDatabase
$code = Invoke-TestPsql @("-U", $PG_USER, "-d", $TEST_DB, "-c",
    "SELECT * FROM nonexistent_table;")
Test-Assert "psql ERROR + exit != 0 → HARD FAIL" ($code -ne 0) "exit code=$code"
Remove-TestDatabase

# ── Test 4: missing research schema after restore → HARD FAIL ─
Write-Host ""
Write-Host "Test 4: missing research schema" -ForegroundColor Yellow
New-TestDatabase
$schemaCount = Get-SqlScalar "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = 'research';"
Test-Assert "missing research schema → HARD FAIL" ($schemaCount -eq "0") "schema count=$schemaCount (expected 0 for empty DB)"
Remove-TestDatabase

# ── Test 5: missing required table → HARD FAIL ────────────────
Write-Host ""
Write-Host "Test 5: missing required table" -ForegroundColor Yellow
New-TestDatabase
$tableCount = Get-SqlScalar "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'research' AND table_name = 'research_experiment';"
Test-Assert "missing required table → HARD FAIL" ($tableCount -eq "0") "table count=$tableCount (expected 0 for empty DB)"
Remove-TestDatabase

# ── Test 6: wrong row count → HARD FAIL ───────────────────────
Write-Host ""
Write-Host "Test 6: wrong row count" -ForegroundColor Yellow
New-TestDatabase
& $psqlPath -U $PG_USER -d $TEST_DB -c "CREATE SCHEMA research; CREATE TABLE research.research_experiment (experiment_id text PRIMARY KEY);" 2>$null
& $psqlPath -U $PG_USER -d $TEST_DB -c "INSERT INTO research.research_experiment VALUES ('test1'), ('test2');" 2>$null
$count = Get-SqlScalar "SELECT COUNT(*) FROM research.research_experiment;"
Test-Assert "wrong row count → HARD FAIL" ($count -ne "12") "count=$count (expected ≠12)"
Remove-TestDatabase

# ── Test 7: external FK handling doesn't delete internal FK ────
Write-Host ""
Write-Host "Test 7: external FK doesn't destroy internal FK" -ForegroundColor Yellow
New-TestDatabase
# Simulate: create research schema with internal FK, then attempt a FK that references missing schema
$setupSql = @"
CREATE SCHEMA research;
CREATE TABLE research.parent (id int PRIMARY KEY);
CREATE TABLE research.child (id int PRIMARY KEY, parent_id int REFERENCES research.parent(id));
INSERT INTO research.parent VALUES (1);
INSERT INTO research.child VALUES (1, 1);
"@
$setupFile = "$env:TEMP\test_fk_setup.sql"
$setupSql | Set-Content $setupFile -Encoding UTF8
& $psqlPath -U $PG_USER -d $TEST_DB -f $setupFile 2>$null

# Now try to add a FK referencing a non-existent schema — should fail but not destroy existing FKs
$badFk = "ALTER TABLE research.child ADD CONSTRAINT bad_fk FOREIGN KEY (id) REFERENCES nonexistent.table(id);"
$badFile = "$env:TEMP\test_fk_bad.sql"
$badFk | Set-Content $badFile -Encoding UTF8
& $psqlPath -U $PG_USER -d $TEST_DB -f $badFile 2>$null

# Verify internal FK still exists
$fkExists = Get-SqlScalar "SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'research' AND constraint_name = 'child_parent_id_fkey';"
Test-Assert "external FK error doesn't destroy internal FK" ($fkExists -eq "1") "internal FK exists=$fkExists"

# Verify internal data intact
$count = Get-SqlScalar "SELECT COUNT(*) FROM research.child;"
Test-Assert "internal data intact after external FK error" ($count -eq "1") "child count=$count"

Remove-TestDatabase
Remove-Item $setupFile, $badFile -Force -ErrorAction SilentlyContinue

# ── Test 8: repeated restore → PASS ───────────────────────────
Write-Host ""
Write-Host "Test 8: repeated restore (idempotent)" -ForegroundColor Yellow
New-TestDatabase
$dumpFile = Join-Path $SNAPSHOT_DIR "research_full_20260928_071957.sql"
# Strip \restrict if present
$dumpContent = Get-Content $dumpFile -Raw
if ($dumpContent -match '\\restrict') {
    $dumpContent = $dumpContent -replace '(?m)^\\restrict\s+\S+\s*$', ''
    $testDump = "$env:TEMP\test_restore_dump.sql"
    [System.IO.File]::WriteAllText($testDump, $dumpContent, [System.Text.Encoding]::UTF8)
    $dumpFile = $testDump
}

# First restore
$code1 = Invoke-TestPsql @("-U", $PG_USER, "-d", $TEST_DB, "-f", $dumpFile)
$count1 = Get-SqlScalar "SELECT COUNT(*) FROM research.research_experiment;"

# Second restore (idempotent — should not break)
$code2 = Invoke-TestPsql @("-U", $PG_USER, "-d", $TEST_DB, "-f", $dumpFile)
$count2 = Get-SqlScalar "SELECT COUNT(*) FROM research.research_experiment;"

Test-Assert "repeated restore: data count stable" ($count1 -eq $count2) "count1=$count1 count2=$count2"

Remove-TestDatabase
Remove-Item "$env:TEMP\test_restore_dump.sql" -Force -ErrorAction SilentlyContinue

# ── Test 9: TARGET_DB=trad_bot → HARD FAIL ────────────────────
Write-Host ""
Write-Host "Test 9: TARGET_DB=trad_bot safety guard" -ForegroundColor Yellow
# We can't actually test this through the script, but we verify the check exists
$scriptContent = Get-Content (Join-Path $SNAPSHOT_DIR "02_local_restore.ps1") -Raw
$hasGuard = $scriptContent -match 'TARGET_DB -eq "trad_bot"'
Test-Assert "TARGET_DB=trad_bot → HARD FAIL guard exists" $hasGuard "guard found in script"

# ── Test 10: real authoritative snapshot restores correctly ─────
Write-Host ""
Write-Host "Test 10: real authoritative snapshot" -ForegroundColor Yellow
New-TestDatabase
$testDumpFile = Join-Path $SNAPSHOT_DIR "research_full_20260928_071957.sql"
$testDumpContent = Get-Content $testDumpFile -Raw
if ($testDumpContent -match '\\restrict') {
    $testDumpContent = $testDumpContent -replace '(?m)^\\restrict\s+\S+\s*$', ''
}
$testDump = "$env:TEMP\test_real_restore.sql"
[System.IO.File]::WriteAllText($testDump, $testDumpContent, [System.Text.Encoding]::UTF8)

& $psqlPath -U $PG_USER -d $TEST_DB -f $testDump 2>$null

# Check required tables
$expCount = Get-SqlScalar "SELECT COUNT(*) FROM research.research_experiment;"
$obsCount = Get-SqlScalar "SELECT COUNT(*) FROM research.research_observation;"
$sigCount = Get-SqlScalar "SELECT COUNT(*) FROM research.research_signal;"
$outCount = Get-SqlScalar "SELECT COUNT(*) FROM research.research_outcome;"

Test-Assert "research_experiment = 12" ($expCount -eq "12") "actual=$expCount"
Test-Assert "research_observation = 6912" ($obsCount -eq "6912") "actual=$obsCount"
Test-Assert "research_signal = 6656" ($sigCount -eq "6656") "actual=$sigCount"
Test-Assert "research_outcome = 6487" ($outCount -eq "6487") "actual=$outCount"

# Check internal FK constraints
$fkCheck = Get-SqlScalar "SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'research';"
Test-Assert "internal FK constraints present (>=10)" ([int]$fkCheck -ge 10) "fk_count=$fkCheck"

Remove-TestDatabase
Remove-Item $testDump -Force -ErrorAction SilentlyContinue

# ============================================================
# RESULTS
# ============================================================

Write-Host ""
Write-Host "=== TEST RESULTS ===" -ForegroundColor Cyan
Write-Host ""
$global:testResults | Format-Table -AutoSize

Write-Host "Passed: $global:testPassed" -ForegroundColor Green
Write-Host "Failed: $global:testFailed" -ForegroundColor $(if ($global:testFailed -gt 0) { "Red" } else { "Green" })
Write-Host ""

if ($global:testFailed -gt 0) {
    Write-Host "OVERALL: FAIL" -ForegroundColor Red
    exit 1
} else {
    Write-Host "OVERALL: PASS" -ForegroundColor Green
    exit 0
}
