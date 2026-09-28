# Research Snapshot — Export & Restore Instructions

## Overview

This pipeline creates a **read-only snapshot** of the Generic Research Framework
schema from VPS PostgreSQL and restores it locally into a **separate** database
for offline Feature Discovery / OOS analysis.

**Safety guarantees:**
- `pg_dump` is a **read-only** snapshot operation — no production data is modified
- No production services are restarted or reconfigured
- Target database `trad_bot_research_snapshot` is **completely separate** from `trad_bot`
- The local `trad_bot` database is **never touched**
- No credentials are stored in export files

---

## Authoritative artifact

The single source of truth is:

```
research_full_<YYYYMMDD_HHMMSS>.sql.gz
```

This is a gzip-compressed plain SQL dump of the entire `research` schema
(structure + data), produced by one `pg_dump` call.

Auxiliary files (`research_schema_*.sql`, `research_data_*.sql.gz`) are
convenience copies and are **not** used for restore.

---

## Step 1: Copy export script to VPS

```powershell
cd D:\py_pro\trad_bot
scp research_snapshot\01_vps_export.sh root@<vps-host>:/tmp/
```

Or paste the script content directly into VPS shell.

## Step 2: Run export on VPS

```bash
ssh root@<vps-host>
bash /tmp/01_vps_export.sh
```

Expected output:
```
=== RESEARCH SNAPSHOT EXPORT ===
Started (UTC): ...
...
  SHA-256: <hash>
  Size:    <size>
...
=== EXPORT COMPLETE ===
```

**No services are restarted. No production data is modified.**

Files created on VPS at `/tmp/research_export/`:
- `research_full_<timestamp>.sql.gz` — **authoritative** (structure + data)
- `research_schema_<timestamp>.sql` — auxiliary (structure only)
- `research_data_<timestamp>.sql.gz` — auxiliary (data only)
- `export_metadata.txt` — counts, git state, SHA-256, time range

## Step 3: Transfer authoritative dump from VPS to local

```powershell
cd D:\py_pro\trad_bot\research_snapshot

# Get filenames:
ssh root@<vps-host> "ls /tmp/research_export/"

# Copy the authoritative dump + metadata (replace timestamp):
scp root@<vps-host>:/tmp/research_export/research_full_YYYYMMDD_HHMMSS.sql.gz .
scp root@<vps-host>:/tmp/research_export/export_metadata.txt .
```

Or use WinSCP:
```
Host: <vps-host>
User: root
Remote dir: /tmp/research_export/
Local dir:  D:\py_pro\trad_bot\research_snapshot\
```

**You only need `research_full_*.sql.gz` and `export_metadata.txt`.**

## Step 4: Run local restore

```powershell
cd D:\py_pro\trad_bot\research_snapshot
.\02_local_restore.ps1
```

This will:
1. Confirm target is `trad_bot_research_snapshot` (never `trad_bot`)
2. Locate `research_full_*.sql.gz`
3. Decompress, restore into `trad_bot_research_snapshot`
4. Show row counts for verification
5. Clean up decompressed SQL

## Step 5: Validate parity

```powershell
psql -U postgres -d trad_bot_research_snapshot -f 03_parity_validation.sql
```

Compare results with `export_metadata.txt`. Minor count differences are
expected if production was writing during the export window.

## Step 6: Proceed with analysis

After parity validation passes, the Feature Discovery analysis runs against
`trad_bot_research_snapshot`.

---

## File inventory

| File | Purpose |
|------|---------|
| `01_vps_export.sh` | Run on VPS — exports research schema (read-only) |
| `02_local_restore.ps1` | Run locally — restores authoritative dump into separate DB |
| `03_parity_validation.sql` | Run locally — validates snapshot integrity |
| `EXPORT_INSTRUCTIONS.md` | This file |

## Metadata fields

`export_metadata.txt` contains:
- `export_started_at` / `export_finished_at` (UTC)
- `source_database`, `postgresql_version`, `hostname`
- `git_head`, `git_branch`
- `sha256` — checksum of the authoritative dump
- `size` — file size of the authoritative dump
- Table counts at export time
- Signal time range (snapshot cutoff)
- Unique symbols and days
