#!/bin/bash
set -u
cd /opt/trad_bot

echo "=== GIT ==="
git rev-parse HEAD
git status -sb

echo "=== DB IDENTITY ==="
sudo -u postgres psql -d trad_bot -P pager=off -t -A \
  -c "SELECT current_database(), current_user, current_setting('TimeZone'), current_timestamp, txid_current();"

echo "=== OBSERVATION DDL ==="
sudo -u postgres psql -d trad_bot -P pager=off -c '\d research.prospective_observation'
echo "=== OUTCOME DDL ==="
sudo -u postgres psql -d trad_bot -P pager=off -c '\d research.prospective_outcome'
echo "=== INSTRUMENT DDL ==="
sudo -u postgres psql -d trad_bot -P pager=off -c '\d dds.instrument'
echo "=== CANDLE DDL ==="
sudo -u postgres psql -d trad_bot -P pager=off -c '\d market.candle'

echo "=== EXPERIMENTS ==="
sudo -u postgres psql -d trad_bot -P pager=off \
  -c "SELECT experiment_id, version, scanner_name, direction, experiment_type, status, started_at, stopped_at, primary_metric, horizons, paired_with FROM research.prospective_experiment WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1') ORDER BY experiment_id;"
