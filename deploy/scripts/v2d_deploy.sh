#!/usr/bin/env bash
# ============================================================
# V2D Deployment Script — Run on VPS
# ============================================================
# Usage:
#   ssh <vps> "bash -s" < deploy/scripts/v2d_deploy.sh
#
# Or copy to VPS and run:
#   bash /opt/trad_bot/deploy/scripts/v2d_deploy.sh
# ============================================================
set -euo pipefail

echo "=== V2D DEPLOYMENT START ==="
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# ── 1. Git pull ──────────────────────────────────────────────
cd /opt/trad_bot
echo ""
echo "--- Step 1: Git pull ---"
git fetch origin
git status --short
CURRENT_HEAD=$(git rev-parse HEAD)
echo "Current HEAD: $CURRENT_HEAD"
git merge origin/main --ff-only
NEW_HEAD=$(git rev-parse HEAD)
echo "New HEAD: $NEW_HEAD"

if [ "$CURRENT_HEAD" = "$NEW_HEAD" ]; then
    echo "Already up to date."
else
    echo "Updated from $CURRENT_HEAD to $NEW_HEAD"
fi

# ── 2. Migration ─────────────────────────────────────────────
echo ""
echo "--- Step 2: Migration 047 ---"
sudo -u postgres psql \
    -v ON_ERROR_STOP=1 \
    -d trad_bot \
    -f sql/migrations/047_atr_wick_v2d_prospective_oos.sql

echo ""
echo "--- Migration verification ---"
sudo -u postgres psql -d trad_bot -P pager=off -c "
\d+ dds.v2d_signal
"
sudo -u postgres psql -d trad_bot -P pager=off -c "
\d+ dds.v2d_outcome
"
sudo -u postgres psql -d trad_bot -P pager=off -c "
SELECT * FROM dds.shadow_oos_experiment_registry
WHERE experiment_id = 'ATR_WICK_FILTER_OOS_V2_D';
"

# ── 3. Systemd units ─────────────────────────────────────────
echo ""
echo "--- Step 3: Systemd units ---"
sudo cp deploy/systemd/trad-bot-v2d-scanner.service /etc/systemd/system/
sudo cp deploy/systemd/trad-bot-v2d-evaluator.service /etc/systemd/system/
sudo cp deploy/systemd/trad-bot-v2d-evaluator.timer /etc/systemd/system/
sudo systemctl daemon-reload

echo ""
echo "--- Step 4: Enable and start services ---"
sudo systemctl enable --now trad-bot-v2d-scanner
sudo systemctl enable --now trad-bot-v2d-evaluator
sudo systemctl enable --now trad-bot-v2d-evaluator.timer

echo ""
echo "--- Step 5: Verify services ---"
systemctl is-active trad-bot-v2d-scanner
systemctl is-active trad-bot-v2d-evaluator
systemctl is-active trad-bot-v2d-evaluator.timer
systemctl is-active trad-bot-scanner
systemctl is-active trad-bot-paper

echo ""
echo "--- Step 6: Check V2D scanner logs (last 30) ---"
journalctl -u trad-bot-v2d-scanner -n 30 --no-pager -l

echo ""
echo "--- Step 7: V1 independence check ---"
sudo -u postgres psql -d trad_bot -P pager=off -c "
SELECT
    experiment_id,
    COUNT(*) AS signals,
    MIN(signal_time) AS first_signal,
    MAX(signal_time) AS last_signal
FROM dds.shadow_signal
WHERE experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
GROUP BY experiment_id;
"

echo ""
echo "--- Step 8: V2D initial check ---"
sudo -u postgres psql -d trad_bot -P pager=off -c "
SELECT
    experiment_id,
    COUNT(*) AS signals,
    MIN(signal_time) AS first_signal,
    MAX(signal_time) AS last_signal
FROM dds.v2d_signal
GROUP BY experiment_id;
"

echo ""
echo "--- Step 9: Registry ---"
sudo -u postgres psql -d trad_bot -P pager=off -c "
SELECT experiment_id, status, registered_at, started_at
FROM dds.shadow_oos_experiment_registry
ORDER BY experiment_id;
"

echo ""
echo "=== V2D DEPLOYMENT COMPLETE ==="
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
