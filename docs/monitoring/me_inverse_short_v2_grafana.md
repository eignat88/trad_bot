# Grafana — ME Inverse SHORT V2 prospective OOS

Existing dashboard (extended in this change):
- Repository: `monitoring/grafana/dashboards/me-inverse-short-v2-oos.json`
- Grafana UID: `me-inverse-short-v2-oos`
- Title: `Trad Bot — ME Inverse SHORT V2 OOS`
- PostgreSQL datasource UID: `trad_bot_postgres`
- Research experiment: `ME_LONG_V2_INVERSE_SHORT_V2`

## What it measures

Activation, fixed fingerprint and `freeze_ts`; event counts and four terminal/pending dispositions; prospective source integrity; hourly signal volume; currently OPEN SHORT paper positions; finalized CLOSED outcomes; realized net PnL over a selected Grafana time range; win rate, profit factor and symbol summaries. Empty OOS cohorts are normal: counters show zero, and performance ratios with undefined denominators return NULL instead of fabricated results.

`CAPTURED` is a pending ledger disposition. It does **not** guarantee `now() - signal_time < 120 minutes`: the DB admission routine resolves stale and capacity-constrained events. A closed position is counted once, not per fee/funding/exit update. Realized PnL excludes unrealized exposure. No market-price or equity history is invented from the V2 event/position tables.

This dashboard does not show individual `NO_BREAKOUT`/`RSI_BELOW_65` funnel rejections: source-only Scanner does not persist those counters. No shared legacy `dds.signal_funnel_observation` table is required.

## Safe VPS rollout (dashboard only)

The existing provisioned Grafana may already have this dashboard. Inspect its real path before copying:

```bash
find /var/lib/grafana/dashboards /etc/grafana -type f -name 'me-inverse-short-v2-oos.json' -print 2>/dev/null
```

Confirm the installed provisioning provider points at the identified directory. Do **not** overwrite an unknown path automatically.

On the VPS, after the PR is merged and local code updated, choose the actual provisioned path and compare:

```bash
cd /opt/trad_bot
SRC=monitoring/grafana/dashboards/me-inverse-short-v2-oos.json
DEST=/var/lib/grafana/dashboards/trad_bot/me-inverse-short-v2-oos.json

# Review real path first; do not proceed unless this is your provisioned path.
test -f "$DEST" && diff -u "$DEST" "$SRC" || true

# Backup existing dashboard before approved replacement.
sudo install -d -m 700 /root/trad_bot_backups
if test -f "$DEST"; then
  sudo cp -p "$DEST" "/root/trad_bot_backups/me-inverse-short-v2-oos.json.before-monitoring"
fi

# File-only provisioned dashboard update (ONLY after verifying DEST).
sudo install -m 0644 "$SRC" "$DEST"
```

Grafana's file-provisioning refresh may pick up the change automatically. Verify dashboard `/d/me-inverse-short-v2-oos` and inspect the panels; do not restart any trading service. If provisioning requires a Grafana-only reload, follow the existing instance's change procedure. Roll back by restoring the backed-up JSON.

## Data access

The read-only `trad_bot_postgres` datasource must have `USAGE` on schema `dds` and `SELECT` on:
- `dds.me_long_v2_inverse_short_v2_activation`
- `dds.me_long_v2_inverse_short_v2_event`
- `dds.me_long_v2_inverse_short_v2_position`

The dashboard does not write to these tables or require a new migration. Confirm grants **without granting extra privileges unless separately approved**.

## Safety constraints

No changes to scanner, production runtime, paper execution, `freeze_ts`, activation row, fingerprints, feature flags, migrations, systemd or other OOS. Dashboard SQL is SELECT-only. This change does not deploy itself to the VPS.
