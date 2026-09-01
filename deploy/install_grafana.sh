#!/usr/bin/env bash
# deploy/install_grafana.sh
# Install Grafana OSS on a Debian/Ubuntu VPS.
#
# Usage (on VPS as root):
#   sudo bash /opt/trad_bot/deploy/install_grafana.sh
#
# Prerequisites:
#   - Ubuntu 20.04+ or Debian 11+
#   - Internet access for apt repos
#
# Exit codes:
#   0 - success
#   1 - not root
#   2 - unsupported OS
#   3 - installation failed

set -euo pipefail

# --- Root check ---
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (sudo)."
    exit 1
fi

# --- OS detection ---
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_VERSION="${VERSION_ID:-unknown}"
else
    echo "ERROR: Cannot detect OS (/etc/os-release not found)."
    exit 2
fi

echo "=== Grafana Installation ==="
echo "OS: $OS_ID $OS_VERSION"
echo ""

case "$OS_ID" in
    ubuntu|debian)
        echo "Detected Debian-based system — proceeding."
        ;;
    *)
        echo "ERROR: Unsupported OS '$OS_ID'. Only Ubuntu/Debian are supported."
        exit 2
        ;;
esac

# --- Install dependencies ---
echo "Step 1: Installing dependencies..."
apt-get update -qq
apt-get install -y -qq apt-transport-https software-properties-common wget gnupg2

# --- Add Grafana GPG key and repo ---
echo "Step 2: Adding Grafana repository..."
mkdir -p /etc/apt/keyrings/

wget -q -O /etc/apt/keyrings/grafana.asc https://apt.grafana.com/gpg.key

echo "deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main" \
    > /etc/apt/sources.list.d/grafana.list

# --- Install Grafana ---
echo "Step 3: Installing Grafana OSS..."
apt-get update -qq
apt-get install -y -y grafana || {
    echo "ERROR: Grafana installation failed."
    exit 3
}

# --- Enable and start service ---
echo "Step 4: Enabling and starting grafana-server..."
systemctl daemon-reload
systemctl enable grafana-server
systemctl start grafana-server

# --- Verify ---
echo "Step 5: Verifying installation..."
sleep 3

if systemctl is-active --quiet grafana-server; then
    echo "  grafana-server is running."
else
    echo "  WARNING: grafana-server may not be running. Check: systemctl status grafana-server"
fi

GRAFANA_VERSION=$(grafana-server -v 2>/dev/null || echo "unknown")
echo "  Grafana version: $GRAFANA_VERSION"

# --- Check port ---
if ss -lntp | grep -q ':3000'; then
    echo "  Port 3000 is listening."
else
    echo "  WARNING: Port 3000 not yet listening. May need a moment to start."
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy provisioning files:"
echo "     sudo cp -r /opt/trad_bot/monitoring/grafana/provisioning/* /etc/grafana/provisioning/"
echo "     sudo mkdir -p /var/lib/grafana/dashboards/trad_bot"
echo "     sudo cp /opt/trad_bot/monitoring/grafana/dashboards/*.json /var/lib/grafana/dashboards/trad_bot/"
echo ""
echo "  2. Set the datasource password:"
echo "     export GRAFANA_READER_PASSWORD='<your_password>'"
echo "     # Then edit /etc/grafana/provisioning/datasources/postgres.yaml"
echo "     # or use environment variable in /etc/default/grafana-server"
echo ""
echo "  3. Restart Grafana:"
echo "     sudo systemctl restart grafana-server"
echo ""
echo "  4. Access Grafana:"
echo "     http://<your-vps-ip>:3000"
echo "     Default login: admin / admin"
echo ""
echo "  5. (Production) Setup Nginx reverse proxy with HTTPS."
echo "     See: /opt/trad_bot/deploy/nginx/grafana.conf"
