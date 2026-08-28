# deploy/export_postgres.ps1
# Export local PostgreSQL trad_bot database to a dump file.
# Usage: .\deploy\export_postgres.ps1
#
# Prerequisites:
#   - pg_dump must be available in PATH (installed with PostgreSQL)
#   - Local PostgreSQL must be running with trad_bot database
#
# Output: artifacts/db_backup/trad_bot_<timestamp>.dump

$ErrorActionPreference = "Stop"

# --- Check pg_dump ---
$pgDump = Get-Command pg_dump -ErrorAction SilentlyContinue
if (-not $pgDump) {
    Write-Error "pg_dump not found in PATH. Install PostgreSQL or add pg_dump to PATH."
    exit 1
}

# --- Configuration ---
$DB_HOST = "localhost"
$DB_PORT = "5432"
$DB_USER = "postgres"
$DB_NAME = "trad_bot"

$backupDir = Join-Path $PSScriptRoot ".." "artifacts" "db_backup"
if (-not (Test-Path $backupDir)) {
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dumpFile = Join-Path $backupDir "trad_bot_$timestamp.dump"

# --- Export ---
Write-Host "Exporting database '$DB_NAME' from $DB_HOST`:$DB_PORT as user $DB_USER..."
Write-Host "Output: $dumpFile"

pg_dump `
    -h $DB_HOST `
    -p $DB_PORT `
    -U $DB_USER `
    -d $DB_NAME `
    -Fc `
    -f $dumpFile

if ($LASTEXITCODE -ne 0) {
    Write-Error "pg_dump failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

$fileInfo = Get-Item $dumpFile
$sizeMB = [math]::Round($fileInfo.Length / 1MB, 2)
Write-Host "Export complete: $dumpFile ($sizeMB MB)"
Write-Host ""
Write-Host "Next step: copy to VPS with:"
Write-Host "  scp $dumpFile root@91.99.60.150:/opt/trad_bot/"
