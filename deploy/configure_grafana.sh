#!/usr/bin/env bash
# deploy/configure_grafana.sh
# Apply provisioning + dashboards + DB schema on VPS after Grafana is installed.
#
# Usage (on VPS as root):
#   sudo bash /opt/trad_bot/deploy/configure_grafana.sh
#
# What it does:
#   1. Creates grafana_reader PostgreSQL user
#   2. Runs mart schema SQL migrations
#   3. Copies Grafana provisioning files
#   4. Copies dashboard JSON files
#   5. Restarts Grafana
#
# Prerequisites:
#   - Grafana installed (install_grafana.sh already run)
#   - PostgreSQL running with trad_bot database
#   - Project at /opt/trad_bot

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PROJ_ROOT="/opt/trad_bot"

# --- Root check ---
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (sudo)."
    exit 1
fi

echo "=== Trad Bot — Grafana Configuration ==="
echo ""

# --- 1. Create grafana_reader PostgreSQL user ---
echo "Step 1: Setting up grafana_reader PostgreSQL user..."
GRAFANA_PASSWORD="${GRAFANA_READER_PASSWORD:-}"

if [ -z "$GRAFANA_PASSWORD" ]; then
    echo "  WARNING: GRAFANA_READER_PASSWORD not set."
    echo "  Generating random password..."
    GRAFANA_PASSWORD=$(openssl rand -base64 24)
    echo "  Generated password: $GRAFANA_PASSWORD"
    echo ""
    echo "  SAVE THIS PASSWORD! Add to Grafana datasource config."
fi

sudo -u postgres psql -d trad_bot -c "
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        CREATE ROLE grafana_reader LOGIN NOINHERIT PASSWORD '$GRAFANA_PASSWORD';
    ELSE
        ALTER ROLE grafana_reader PASSWORD '$GRAFANA_PASSWORD';
    END IF;
END
\$\$;
" 2>/dev/null || {
    echo "  Failed to create/update grafana_reader role."
    exit 1
}
echo "  grafana_reader user ready."

# --- 2. Run SQL migrations and Grafana MART views ---
echo ""
echo "Step 2: Running SQL migrations..."
for SQL_DIR in "$PROJ_ROOT"/sql/migrations "$PROJ_ROOT"/sql/mart; do
    for SQL_FILE in "$SQL_DIR"/*.sql; do
        if [ -f "$SQL_FILE" ]; then
            echo "  Applying: $(basename "$SQL_FILE")"
            sudo -u postgres psql -d trad_bot -f "$SQL_FILE" 2>&1 | tail -1
        fi
    done
done
echo "  SQL migrations complete."

# --- 3. Copy provisioning ---
echo ""
echo "Step 3: Copying Grafana provisioning files..."
mkdir -p /etc/grafana/provisioning/datasources
mkdir -p /etc/grafana/provisioning/dashboards

cp "$PROJ_ROOT/monitoring/grafana/provisioning/datasources/postgres.yaml" \
    /etc/grafana/provisioning/datasources/postgres.yaml

cp "$PROJ_ROOT/monitoring/grafana/provisioning/dashboards/dashboards.yaml" \
    /etc/grafana/provisioning/dashboards/dashboards.yaml

# Substitute password in datasource config
sed -i "s|\${GRAFANA_READER_PASSWORD}|${GRAFANA_PASSWORD}|g" \
    /etc/grafana/provisioning/datasources/postgres.yaml

echo "  Provisioning files copied."

# --- 4. Copy dashboards ---
echo ""
echo "Step 4: Copying dashboard JSON files..."
mkdir -p /var/lib/grafana/dashboards/trad_bot
cp "$PROJ_ROOT/monitoring/grafana/dashboards/"*.json \
    /var/lib/grafana/dashboards/trad_bot/
chown -R grafana:grafana /var/lib/grafana/dashboards/trad_bot
echo "  Dashboards copied."

# --- 5. Grant Grafana access to dashboard dir ---
echo ""
echo "Step 5: Fixing permissions..."
chown -R grafana:grafana /var/lib/grafana/dashboards

# --- 6. Restart Grafana ---
echo ""
echo "Step 6: Restarting Grafana..."
systemctl restart grafana-server
sleep 3

if systemctl is-active --quiet grafana-server; then
    echo "  grafana-server restarted successfully."
else
    echo "  WARNING: grafana-server may not be running."
    echo "  Check: systemctl status grafana-server"
fi

echo ""
echo "=== Configuration complete ==="
echo ""
echo "Grafana access:"
echo "  http://<your-vps-ip>:3000"
echo "  Login: admin / admin (change on first login)"
echo ""
echo "Dashboards available in 'Trad Bot' folder:"
echo "  - Scanner Overview"
echo "  - Scanner Performance"
echo "  - Paper Trading"
echo "  - Signal Funnel"
echo "  - System Health"
