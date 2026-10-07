#!/bin/bash
set -euo pipefail
export LC_ALL=C

OUT_DIR="/tmp/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007_v2"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

run_sql() {
  local file="$1"
  local sql_file="$2"
  sudo -u postgres psql -d trad_bot -P pager=off -t -A -F $'\t' -f "$sql_file" > "$OUT_DIR/$file"
}

cat > /tmp/me_b_dataset_v2.sql <<'SQL'
COPY (
SELECT
  experiment_id, source_signal_id, observation_id, symbol, direction,
  signal_time, created_at, reference_price, invalidation_price, target_1,
  variant_entry, variant_stop, variant_target, score, rule_passed,
  filter_reason, market_regime, features, parameters
FROM research.prospective_observation
WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
ORDER BY source_signal_id, experiment_id
) TO STDOUT (FORMAT TEXT, NULL '\N')
SQL

cat > /tmp/me_b_outcomes_v2.sql <<'SQL'
COPY (
SELECT r.observation_id, o.experiment_id, o.source_signal_id, o.symbol, o.direction,
       o.signal_time, o.created_at,
       r.mfe_15m, r.mfe_30m, r.mfe_60m, r.mfe_120m, r.mfe_240m,
       r.mae_15m, r.mae_30m, r.mae_60m, r.mae_120m, r.mae_240m,
       r.mfe_r_15m, r.mfe_r_30m, r.mfe_r_60m, r.mfe_r_120m, r.mfe_r_240m,
       r.mae_r_15m, r.mae_r_30m, r.mae_r_60m, r.mae_r_120m, r.mae_r_240m,
       r.return_at_15m, r.return_at_30m, r.return_at_60m, r.return_at_120m, r.return_at_240m,
       r.tp_hit, r.sl_hit, r.tp_before_sl, r.sl_before_tp, r.ambiguous_intrabar,
       r.time_to_tp, r.time_to_sl,
       r.evaluated_15m_at, r.evaluated_30m_at, r.evaluated_60m_at, r.evaluated_120m_at, r.evaluated_240m_at,
       r.is_final, r.created_at, r.updated_at
FROM research.prospective_outcome r
JOIN research.prospective_observation o ON o.observation_id = r.observation_id
WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
ORDER BY o.source_signal_id, o.experiment_id
) TO STDOUT (FORMAT TEXT, NULL '\N')
SQL

cat > /tmp/me_b_candles_v2.sql <<'SQL'
COPY (
WITH target AS (
  SELECT DISTINCT i.symbol, date_trunc('minute', o.signal_time) - interval '5 minutes' AS from_time,
                  date_trunc('minute', o.signal_time) + interval '240 minutes' AS to_time
  FROM research.prospective_observation o
  JOIN dds.instrument i ON i.symbol = o.symbol
  WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
)
SELECT c.exchange, c.market_type, i.symbol, c.timeframe,
       c.open_time AT TIME ZONE 'UTC',
       c.close_time AT TIME ZONE 'UTC',
       c.open, c.high, c.low, c.close, c.volume, c.turnover,
       c.is_closed, c.quality_status, c.source
FROM market.candle c
JOIN dds.instrument i ON i.instrument_id = c.instrument_id
JOIN target t ON i.symbol = t.symbol
WHERE c.exchange = 'bybit' AND c.market_type = 'linear'
  AND c.timeframe = '5'
  AND c.is_closed = TRUE
  AND c.quality_status <> 'invalid'
  AND c.open_time >= t.from_time AND c.open_time < t.to_time
GROUP BY c.exchange, c.market_type, i.symbol, c.timeframe, c.open_time,
         c.close_time, c.open, c.high, c.low, c.close, c.volume, c.turnover,
         c.is_closed, c.quality_status, c.source
ORDER BY i.symbol, c.open_time
) TO STDOUT (FORMAT TEXT, NULL '\N')
SQL

cat > /tmp/me_b_candle_unique_keys.sql <<'SQL'
COPY (
WITH target AS (
  SELECT DISTINCT i.instrument_id
  FROM research.prospective_observation o
  JOIN dds.instrument i ON i.symbol = o.symbol
  WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
)
SELECT c.exchange, c.market_type, c.instrument_id, c.timeframe, c.open_time,
       COUNT(*) AS raw_rows,
       COUNT(DISTINCT (c.close_time, c.open, c.high, c.low, c.close, c.volume,
                       c.turnover, c.is_closed, c.quality_status, c.source)) AS distinct_payloads
FROM market.candle c
JOIN target t ON t.instrument_id = c.instrument_id
WHERE c.exchange = 'bybit' AND c.market_type = 'linear'
  AND c.timeframe = '5' AND c.open_time >= '2026-09-28T00:00:00Z'
GROUP BY c.exchange, c.market_type, c.instrument_id, c.timeframe, c.open_time
HAVING COUNT(*) > 1
ORDER BY raw_rows DESC, c.instrument_id, c.open_time
) TO STDOUT (FORMAT TEXT, NULL '\N')
SQL

cat > /tmp/me_b_db_identity_v2.sql <<'SQL'
SELECT now() AS db_now, current_database() AS database, current_user AS current_user,
       current_setting('TimeZone') AS timezone, txid_current() AS txid;
SQL

cat > /tmp/me_b_observation_fingerprint_v2.sql <<'SQL'
COPY (SELECT md5(string_agg(row_hash, E'\n' ORDER BY experiment_id, observation_id)) FROM (
  SELECT experiment_id, observation_id,
         md5(concat_ws('|', experiment_id, source_signal_id, symbol, direction,
                       signal_time, reference_price, invalidation_price, target_1,
                       variant_entry, variant_stop, variant_target, features,
                       parameters, created_at)) AS row_hash
  FROM research.prospective_observation
  WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
) x) TO STDOUT;
SQL

cat > /tmp/me_b_outcome_fingerprint_v2.sql <<'SQL'
COPY (SELECT md5(string_agg(row_hash, E'\n' ORDER BY experiment_id, observation_id)) FROM (
  SELECT r.experiment_id, r.observation_id,
         md5(concat_ws('|', r.experiment_id, r.mfe_60m, r.mae_60m, r.mfe_r_60m,
                       r.mae_r_60m, r.return_at_60m, r.tp_hit, r.sl_hit,
                       r.tp_before_sl, r.sl_before_tp, r.ambiguous_intrabar,
                       r.time_to_tp, r.time_to_sl, r.evaluated_240m_at, r.is_final,
                       r.created_at, r.updated_at)) AS row_hash
  FROM research.prospective_outcome r
  JOIN research.prospective_observation o ON o.observation_id = r.observation_id
  WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
) x) TO STDOUT;
SQL

cat > /tmp/me_b_experiment_rows_v2.sql <<'SQL'
SELECT experiment_id, version, scanner_name, direction, experiment_type,
       status, started_at, primary_metric, horizons, paired_with,
       minimum_n, minimum_symbols, created_at
FROM research.prospective_experiment
WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
ORDER BY experiment_id;
SQL

cat > /tmp/me_b_instruments_v2.sql <<'SQL'
COPY (
SELECT i.instrument_id, i.symbol, i.quote_asset, i.category, i.status
FROM dds.instrument i
WHERE i.symbol IN (
  SELECT DISTINCT symbol FROM research.prospective_observation
  WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
)
) TO STDOUT (FORMAT TEXT, NULL '\N')
SQL

cat > /tmp/me_b_candle_coverage_v2.sql <<'SQL'
COPY (
WITH target AS (
  SELECT DISTINCT o.symbol, o.signal_time
  FROM research.prospective_observation o
  WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
)
SELECT
  t.symbol, t.signal_time,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '15 minutes')
                      AND c.timeframe = '5') AS n_15m,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '30 minutes')
                      AND c.timeframe = '5') AS n_30m,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '60 minutes')
                      AND c.timeframe = '5') AS n_60m,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '120 minutes')
                      AND c.timeframe = '5') AS n_120m,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '240 minutes')
                      AND c.timeframe = '5') AS n_240m
FROM target t
JOIN dds.instrument i ON i.symbol = t.symbol
LEFT JOIN market.candle c
  ON c.exchange = 'bybit' AND c.market_type = 'linear'
 AND c.instrument_id = i.instrument_id AND c.timeframe = '5'
 AND c.is_closed = TRUE AND c.quality_status <> 'invalid'
 AND c.open_time >= date_trunc('minute', t.signal_time)
 AND c.open_time < date_trunc('minute', t.signal_time + interval '240 minutes')
GROUP BY t.symbol, t.signal_time
) TO STDOUT (FORMAT TEXT, NULL '\N')
SQL

run_sql "dataset_pairs.tsv" /tmp/me_b_dataset_v2.sql
run_sql "outcomes.tsv" /tmp/me_b_outcomes_v2.sql
run_sql "candles.tsv" /tmp/me_b_candles_v2.sql
run_sql "candle_duplicate_keys.tsv" /tmp/me_b_candle_unique_keys.sql
run_sql "instruments.tsv" /tmp/me_b_instruments_v2.sql
run_sql "candle_coverage.tsv" /tmp/me_b_candle_coverage_v2.sql
sudo -u postgres psql -d trad_bot -P pager=off -x -f /tmp/me_b_experiment_rows_v2.sql > "$OUT_DIR/experiment_rows.txt"
sudo -u postgres psql -d trad_bot -P pager=off -x -f /tmp/me_b_db_identity_v2.sql > "$OUT_DIR/snapshot_meta.txt"
sudo -u postgres psql -d trad_bot -P pager=off -t -A -f /tmp/me_b_observation_fingerprint_v2.sql > "$OUT_DIR/observation_fingerprint.txt"
sudo -u postgres psql -d trad_bot -P pager=off -t -A -f /tmp/me_b_outcome_fingerprint_v2.sql > "$OUT_DIR/outcome_fingerprint.txt"

{
  echo "=== EXPORT METADATA ==="
  echo "export_started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "database=trad_bot"
  echo "timezone=Etc/UTC"
  echo "git_head=7c5e0959c5a8e6ce82f455f1ca727f56810ce0b1"
  echo "observation_fingerprint=$(tr -d '\n' < "$OUT_DIR/observation_fingerprint.txt")"
  echo "outcome_fingerprint=$(tr -d '\n' < "$OUT_DIR/outcome_fingerprint.txt")"
  echo "=== ROW COUNTS ==="
  wc -l "$OUT_DIR"/*
  echo "=== EXPORT FINISHED ==="
  echo "export_finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$OUT_DIR/export_metadata.txt"

cd /
tar -C "$OUT_DIR" -czf /tmp/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007_v2.tar.gz .
echo "/tmp/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007_v2.tar.gz"
