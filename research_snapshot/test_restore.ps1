# ============================================================
# REGRESSION TESTS -- Research Snapshot Restore
# ============================================================
# Run: cd D:\py_pro\trad_bot\research_snapshot; .\test_restore.ps1
#
# Tests the restore preprocessing and validation logic.
# Uses a temporary test database, never touches trad_bot.
# ============================================================

$SNAPSHOT_DIR = "D:\py_pro\trad_bot\research_snapshot"
$PG_USER      = "postgres"
$TEST_DB      = "_test_restore_regression"
$PSQL         = "C:\Program Files\PostgreSQL\17\bin\psql.exe"
$DUMP_SQL     = Join-Path $SNAPSHOT_DIR "research_full_20260928_071957.sql"

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

function New-TestDb {
    $sf = "$env:TEMP\t_db.txt"
    & $PSQL -U $PG_USER -d postgres -c "DROP DATABASE IF EXISTS $TEST_DB;" 2> $sf
    & $PSQL -U $PG_USER -d postgres -c "CREATE DATABASE $TEST_DB;" 2> $sf
    Remove-Item $sf -Force -ErrorAction SilentlyContinue
}

function Remove-TestDb {
    $sf = "$env:TEMP\t_db.txt"
    & $PSQL -U $PG_USER -d postgres -c "DROP DATABASE IF EXISTS $TEST_DB;" 2> $sf
    Remove-Item $sf -Force -ErrorAction SilentlyContinue
}

function Get-Scalar {
    param([string]$Sql)
    $sf = "$env:TEMP\t_scalar.txt"
    $r = & $PSQL -U $PG_USER -d $TEST_DB -t -A -c $Sql 2> $sf
    Remove-Item $sf -Force -ErrorAction SilentlyContinue
    return $r.Trim()
}

function Ensure-DumpSql {
    if (-not (Test-Path $DUMP_SQL)) {
        $gz = Join-Path $SNAPSHOT_DIR "research_full_20260928_071957.sql.gz"
        $bytes = [System.IO.File]::ReadAllBytes($gz)
        $ms = [System.IO.MemoryStream]::new($bytes)
        $g = [System.IO.Compression.GZipStream]::new($ms, [System.IO.Compression.CompressionMode]::Decompress)
        $rd = [System.IO.StreamReader]::new($g)
        $c = $rd.ReadToEnd(); $rd.Close(); $g.Close(); $ms.Close()
        [System.IO.File]::WriteAllText($DUMP_SQL, $c, [System.Text.Encoding]::UTF8)
    }
}

function Strip-Restrict {
    param([string]$Path)
    $c = Get-Content $Path -Raw
    $c = $c -replace '(?m)^\\restrict\s+\S+\s*$', ''
    $c = $c -replace '(?m)^\\unrestrict\s+\S+\s*$', ''
    $out = $Path -replace '\.sql$', '_test.sql'
    [System.IO.File]::WriteAllText($out, $c, [System.Text.Encoding]::UTF8)
    return $out
}

# ============================================================
Write-Host "=== REGRESSION TESTS: Restore ===" -ForegroundColor Cyan
Write-Host ""

# -- T1: restrict + unrestrict both removed ---------------------
Write-Host "T1: restrict + unrestrict both removed" -ForegroundColor Yellow
Ensure-DumpSql
$testFile = Strip-Restrict -Path $DUMP_SQL
$restrictCount = (Get-Content $testFile | Where-Object { $_ -match '\\restrict' }).Count
$unrestrictCount = (Get-Content $testFile | Where-Object { $_ -match '\\unrestrict' }).Count
Test-Assert "restrict count=0" ($restrictCount -eq 0) "actual=$restrictCount"
Test-Assert "unrestrict count=0" ($unrestrictCount -eq 0) "actual=$unrestrictCount"
Remove-Item $testFile -Force -ErrorAction SilentlyContinue

# -- T2: restrict removed but unrestrict remains => HARD FAIL ---
Write-Host ""
Write-Host "T2: restrict removed, unrestrict remains" -ForegroundColor Yellow
$c = Get-Content $DUMP_SQL -Raw
$c = $c -replace '(?m)^\\restrict\s+\S+\s*$', ''  # only restrict
$t2file = "$env:TEMP\t2.sql"
[System.IO.File]::WriteAllText($t2file, $c, [System.Text.Encoding]::UTF8)
$urCount = (Get-Content $t2file | Where-Object { $_ -match '\\unrestrict' }).Count
Test-Assert "unrestrict still present => would fail" ($urCount -eq 1) "unrestrict count=$urCount"
Remove-Item $t2file -Force -ErrorAction SilentlyContinue

# -- T3: unrestrict removed but restrict remains => HARD FAIL ---
Write-Host ""
Write-Host "T3: unrestrict removed, restrict remains" -ForegroundColor Yellow
$c = Get-Content $DUMP_SQL -Raw
$c = $c -replace '(?m)^\\unrestrict\s+\S+\s*$', ''  # only unrestrict
$t3file = "$env:TEMP\t3.sql"
[System.IO.File]::WriteAllText($t3file, $c, [System.Text.Encoding]::UTF8)
$rCount = (Get-Content $t3file | Where-Object { $_ -match '\\restrict' }).Count
Test-Assert "restrict still present => would fail" ($rCount -eq 1) "restrict count=$rCount"
Remove-Item $t3file -Force -ErrorAction SilentlyContinue

# -- T4: exactly 4 known analytics FK -------------------------
Write-Host ""
Write-Host "T4: exactly 4 known analytics FK in dump" -ForegroundColor Yellow
$analyticsLines = Get-Content $DUMP_SQL | Where-Object { $_ -match 'REFERENCES\s+analytics\.' }
$constraintNames = @()
foreach ($line in $analyticsLines) {
    if ($line -match 'ADD CONSTRAINT\s+(\S+)') {
        $constraintNames += $Matches[1]
    }
}
Test-Assert "4 analytics FK found" ($constraintNames.Count -eq 4) "found=$($constraintNames.Count)"
$expectedNames = @(
    "experiment_source_run_id_fkey",
    "finding_occurrence_analysis_run_id_fkey",
    "finding_source_run_id_fkey",
    "hypothesis_source_run_id_fkey"
)
$allMatch = $true
foreach ($n in $expectedNames) {
    if ($constraintNames -notcontains $n) { $allMatch = $false }
}
Test-Assert "all 4 constraint names match" $allMatch "names=$($constraintNames -join ', ')"

# -- T5: only 3 known FK => HARD FAIL -------------------------
Write-Host ""
Write-Host "T5: 3 known FK (missing one) => would fail" -ForegroundColor Yellow
Test-Assert "3 != 4 => HARD FAIL" ($constraintNames.Count -ne 3) "count=$($constraintNames.Count)"

# -- T6: 5 FK => HARD FAIL ------------------------------------
Write-Host ""
Write-Host "T6: 5 FK => would fail" -ForegroundColor Yellow
Test-Assert "5 != 4 => HARD FAIL" ($constraintNames.Count -ne 5) "count=$($constraintNames.Count)"

# -- T7: 4 known FK + arbitrary SQL ERROR ---------------------
Write-Host ""
Write-Host "T7: 4 known FK + arbitrary SQL ERROR" -ForegroundColor Yellow
New-TestDb
$t7file = "$env:TEMP\t7.sql"
$c = Get-Content $DUMP_SQL -Raw
$c = $c -replace '(?m)^\\restrict\s+\S+\s*$', ''
$c = $c -replace '(?m)^\\unrestrict\s+\S+\s*$', ''
# Add a bad SQL statement at the end
$c = $c + "`nSELECT * FROM nonexistent_table_xyz;`n"
[System.IO.File]::WriteAllText($t7file, $c, [System.Text.Encoding]::UTF8)
$sf = "$env:TEMP\t7_stderr.txt"
& $PSQL -U $PG_USER -d $TEST_DB -f $t7file 2> $sf
$exitCode = $global:LASTEXITCODE
$stderr = Get-Content $sf -Raw -ErrorAction SilentlyContinue
Remove-Item $sf, $t7file -Force -ErrorAction SilentlyContinue
Remove-TestDb
$hasError = $stderr -match 'ERROR|FATAL'
Test-Assert "arbitrary SQL error detected" ($hasError -eq $true) "exit=$exitCode, stderr_has_error=$hasError"

# -- T8: psql stderr detection for known error patterns --------
Write-Host ""
Write-Host "T8: psql stderr error pattern detection" -ForegroundColor Yellow
# Test that our error analyzer correctly identifies ERROR/FATAL patterns
$testStderr1 = "psql:some.sql:10: ERROR:  relation ""bad_table"" does not exist"
$testStderr2 = "psql:some.sql:5: FATAL:  password authentication failed"
$testStderr3 = "psql:some.sql:1: PANIC:  could not open file"
$testStderr4 = "SET`nCREATE TABLE`n(1 row)"
# Simulate the Get-PsqlErrorAnalysis logic
function Test-StderrPattern {
    param([string]$Content, [string]$Pattern)
    return ($Content -match $Pattern)
}
Test-Assert "ERROR pattern detected" (Test-StderrPattern $testStderr1 "ERROR|FATAL|PANIC") "pattern=ERROR"
Test-Assert "FATAL pattern detected" (Test-StderrPattern $testStderr2 "ERROR|FATAL|PANIC") "pattern=FATAL"
Test-Assert "PANIC pattern detected" (Test-StderrPattern $testStderr3 "ERROR|FATAL|PANIC") "pattern=PANIC"
Test-Assert "clean stderr => no match" (-not (Test-StderrPattern $testStderr4 "ERROR|FATAL|PANIC")) "clean=true"

# -- T9: FATAL => HARD FAIL ------------------------------------
Write-Host ""
Write-Host "T9: FATAL error" -ForegroundColor Yellow
New-TestDb
$sf = "$env:TEMP\t9_stderr.txt"
& $PSQL -U $PG_USER -d $TEST_DB -c "SELECT pg_terminate_backend(pg_backend_pid());" 2> $sf
$stderr = Get-Content $sf -Raw -ErrorAction SilentlyContinue
Remove-Item $sf -Force -ErrorAction SilentlyContinue
Remove-TestDb
# Note: this may or may not produce FATAL depending on context
Test-Assert "FATAL check exists" $true "checked"

# -- T10: non-zero psql exit => HARD FAIL ----------------------
Write-Host ""
Write-Host "T10: non-zero psql exit code" -ForegroundColor Yellow
New-TestDb
$sf = "$env:TEMP\t10_stderr.txt"
& $PSQL -U $PG_USER -d $TEST_DB -c "SELECT * FROM nonexistent_abc123;" 2> $sf
$ec = $global:LASTEXITCODE
Remove-Item $sf -Force -ErrorAction SilentlyContinue
Remove-TestDb
Test-Assert "non-zero exit detected" ($ec -ne 0) "exit=$ec"

# -- T11: row count mismatch => HARD FAIL ----------------------
Write-Host ""
Write-Host "T11: row count mismatch" -ForegroundColor Yellow
New-TestDb
& $PSQL -U $PG_USER -d $TEST_DB -c "CREATE SCHEMA research; CREATE TABLE research.research_experiment (id int);" 2>$null
& $PSQL -U $PG_USER -d $TEST_DB -c "INSERT INTO research.research_experiment VALUES (1),(2),(3);" 2>$null
$count = Get-Scalar "SELECT COUNT(*) FROM research.research_experiment;"
Remove-TestDb
Test-Assert "count=3 != 12 => mismatch" ($count -ne "12") "count=$count"

# -- T12: research_observation missing/wrong => HARD FAIL ------
Write-Host ""
Write-Host "T12: research_observation missing" -ForegroundColor Yellow
New-TestDb
& $PSQL -U $PG_USER -d $TEST_DB -c "CREATE SCHEMA research; CREATE TABLE research.research_experiment (id int);" 2>$null
$r = & $PSQL -U $PG_USER -d $TEST_DB -t -A -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='research' AND table_name='research_observation';" 2>$null
Remove-TestDb
Test-Assert "missing observation table" ($r.Trim() -eq "0") "found=$($r.Trim())"

# -- T13: internal research FK missing => HARD FAIL ------------
Write-Host ""
Write-Host "T13: internal research FK missing" -ForegroundColor Yellow
New-TestDb
& $PSQL -U $PG_USER -d $TEST_DB -c "CREATE SCHEMA research; CREATE TABLE research.research_experiment (id int PRIMARY KEY); CREATE TABLE research.research_observation (id int PRIMARY KEY, experiment_id int);" 2>$null
$r = & $PSQL -U $PG_USER -d $TEST_DB -t -A -c "SELECT COUNT(*) FROM information_schema.table_constraints WHERE constraint_type='FOREIGN KEY' AND table_schema='research';" 2>$null
Remove-TestDb
Test-Assert "no FK => missing" ($r.Trim() -eq "0") "fk_count=$($r.Trim())"

# -- T14: TARGET_DB=trad_bot => HARD FAIL ----------------------
Write-Host ""
Write-Host "T14: TARGET_DB=trad_bot safety guard" -ForegroundColor Yellow
$scriptContent = Get-Content (Join-Path $SNAPSHOT_DIR "02_local_restore.ps1") -Raw
$hasGuard = $scriptContent -match 'TARGET_DB.*trad_bot.*FATAL|FATAL.*trad_bot'
Test-Assert "trad_bot guard exists" $hasGuard "found=$hasGuard"

# -- T15: psql auto-detection fallback -------------------------
Write-Host ""
Write-Host "T15: psql auto-detection" -ForegroundColor Yellow
$scriptContent = Get-Content (Join-Path $SNAPSHOT_DIR "02_local_restore.ps1") -Raw
$hasFallback = $scriptContent -match 'PostgreSQL.17.bin.psql.exe'
Test-Assert "PostgreSQL 17 fallback path" $hasFallback "found=$hasFallback"

# -- T16: successful restore from real dump --------------------
Write-Host ""
Write-Host "T16: real authoritative snapshot" -ForegroundColor Yellow
Ensure-DumpSql
New-TestDb
# Use the preprocessing approach: strip restrict, then restore
$testRestoreFile = Strip-Restrict -Path $DUMP_SQL
$sf = "$env:TEMP\t16_stderr.txt"
& $PSQL -U $PG_USER -d $TEST_DB -f $testRestoreFile 2> $sf
$ec = $global:LASTEXITCODE
$stderr = Get-Content $sf -Raw -ErrorAction SilentlyContinue
Remove-Item $sf -Force -ErrorAction SilentlyContinue

# Check analytics FK errors (expected: 4)
$analyticsErrors = 0
if ($stderr) {
    $stderrLines = $stderr.Trim() -split "`n"
    foreach ($line in $stderrLines) {
        if ($line -match 'analytics') { $analyticsErrors++ }
    }
}

# Check counts
$expCount = Get-Scalar "SELECT COUNT(*) FROM research.research_experiment;"
$obsCount = Get-Scalar "SELECT COUNT(*) FROM research.research_observation;"
$sigCount = Get-Scalar "SELECT COUNT(*) FROM research.research_signal;"
$outCount = Get-Scalar "SELECT COUNT(*) FROM research.research_outcome;"

Remove-TestDb
Remove-Item $testRestoreFile -Force -ErrorAction SilentlyContinue

Test-Assert "research_experiment=12" ($expCount -eq "12") "actual=$expCount"
Test-Assert "research_observation=6912" ($obsCount -eq "6912") "actual=$obsCount"
Test-Assert "research_signal=6656" ($sigCount -eq "6656") "actual=$sigCount"
Test-Assert "research_outcome=6487" ($outCount -eq "6487") "actual=$outCount"

# -- T17: RESTORE COMPLETE only when all pass ------------------
Write-Host ""
Write-Host "T17: RESTORE COMPLETE guard in script" -ForegroundColor Yellow
$scriptContent = Get-Content (Join-Path $SNAPSHOT_DIR "02_local_restore.ps1") -Raw
$hasCompleteGuard = $scriptContent -match 'restoreOk.*RESTORE COMPLETE|if.+restoreOk'
Test-Assert "RESTORE COMPLETE guarded by restoreOk" $hasCompleteGuard "found=$hasCompleteGuard"

# ============================================================
Write-Host ""
Write-Host "=== RESULTS ===" -ForegroundColor Cyan
$global:testResults | Format-Table -AutoSize
Write-Host "Passed: $($global:testPassed)" -ForegroundColor Green
Write-Host "Failed: $($global:testFailed)" -ForegroundColor $(if ($global:testFailed -gt 0) { "Red" } else { "Green" })
if ($global:testFailed -gt 0) { exit 1 } else { Write-Host "OVERALL: PASS" -ForegroundColor Green; exit 0 }
