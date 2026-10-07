#!/bin/bash
set -euo pipefail
export LC_ALL=C

OUT_DIR="/tmp/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

run_sql() {
  local file="$1"
  sudo -u postgres psql -d trad_bot -P pager=off -t -A -F $'\t' -c "$2" > "$OUT_DIR/$file"
}

run_exp() {
  local file="$1"
  local sql_file="$2"
  sudo -u postgres psql -d trad_bot -P pager=off -x -f "$sql_file" > "$OUT_DIR/$file"
}

cat > /tmp/me_b_exp_meta.sql <<'SQL'
SELECT experiment_id, version, scanner_name, direction, experiment_type,
       status, started_at, primary_metric, horizons, paired_with,
       minimum_n, minimum_symbols, created_at
FROM research.prospective_experiment
WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
ORDER BY experiment_id;
SQL

cat > /tmp/me_b_dataset.sql <<'SQL'
COPY (
SELECT
  'experiment_id', experiment_id,
  'source_signal_id', source_signal_id::text,
  'observation_id', observation_id::text,
  'symbol', symbol,
  'direction', direction,
  'signal_time', to_char(signal_time AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'created_at', to_char(created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'reference_price', reference_price::text,
  'invalidation_price', invalidation_price::text,
  'target_1', target_1::text,
  'variant_entry', variant_entry::text,
  'variant_stop', variant_stop::text,
  'variant_target', variant_target::text,
  'score', score::text,
  'rule_passed', rule_passed::text,
  'filter_reason', filter_reason,
  'market_regime', market_regime,
  'features', features::text,
  'parameters', parameters::text
FROM research.prospective_observation
WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
ORDER BY source_signal_id, experiment_id
UNION ALL
SELECT
  'experiment_id', o.experiment_id,
  'source_signal_id', o.source_signal_id::text,
  'observation_id', o.observation_id::text,
  'symbol', o.symbol,
  'direction', o.direction,
  'signal_time', to_char(o.signal_time AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'created_at', to_char(o.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'reference_price', o.reference_price::text,
  'invalidation_price', o.invalidation_price::text,
  'target_1', o.target_1::text,
  'variant_entry', o.variant_entry::text,
  'variant_stop', o.variant_stop::text,
  'variant_target', o.variant_target::text,
  'score', o.score::text,
  'rule_passed', o.rule_passed::text,
  'filter_reason', o.filter_reason,
  'market_regime', o.market_regime,
  'features', o.features::text,
  'parameters', o.parameters::text
FROM research.prospective_observation o
JOIN research.prospective_outcome r ON r.observation_id = o.observation_id
WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
  AND (r.updated_at > '2026-10-07T13:04:55Z' OR r.evaluated_240m_at > '2026-10-07T13:04:55Z')
) TO STDOUT (FORMAT TEXT)
SQL

cat > /tmp/me_b_outcomes.sql <<'SQL'
COPY (
SELECT
  r.observation_id, o.experiment_id, o.source_signal_id, o.symbol, o.direction,
  o.signal_time, o.created_at,
  r.mfe_15m, r.mfe_30m, r.mfe_60m, r.mfe_120m, r.mfe_240m,
  r.mae_15m, r.mae_30m, r.mae_60m, r.mae_120m, r.mae_240m,
  r.mfe_r_15m, r.mfe_r_30m, r.mfe_r_60m, r.mfe_r_120m, r.mfe_r_240m,
  r.mae_r_15m, r.mae_r_30m, r.mae_r_60m, r.mae_r_120m, r.mae_r_240m,
  r.return_at_15m, r.return_at_30m, r.return_at_60m, r.return_at_120m, r.return_at_240m,
  r.tp_hit, r.sl_hit, r.tp_before_sl, r.sl_before_tp, r.ambiguous_intrabar,
  r.time_to_tp, r.time_to_sl,
  r.evaluated_15m_at, r.evaluated_30m_at, r.evaluated_60m_at, r.evaluated_120m_at, r.evaluated_240m_at,
  r.is_final, r.created_at AS outcome_created_at, r.updated_at AS outcome_updated_at
FROM research.prospective_outcome r
JOIN research.prospective_observation o ON o.observation_id = r.observation_id
WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
ORDER BY o.source_signal_id, o.experiment_id
) TO STDOUT (FORMAT TEXT, NULL '\N')
SQL

cat > /tmp/me_b_instruments.sql <<'SQL'
COPY (
SELECT i.instrument_id, i.symbol, i.quote_asset, i.category, i.status
FROM dds.instrument i
WHERE i.symbol IN (
  SELECT DISTINCT symbol FROM research.prospective_observation
  WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
)
) TO STDOUT (FORMAT TEXT, NULL '\N')
SQL

cat > /tmp/me_b_candle_coverage.sql <<'SQL'
COPY (
WITH target AS (
  SELECT DISTINCT o.symbol, o.signal_time
  FROM research.prospective_observation o
  WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
)
SELECT
  t.symbol,
  t.signal_time,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '240 minutes')
                      AND c.timeframe = '5') AS n_240m,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '120 minutes')
                      AND c.timeframe = '5') AS n_120m,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '60 minutes')
                      AND c.timeframe = '5') AS n_60m,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '30 minutes')
                      AND c.timeframe = '5') AS n_30m,
  COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time)
                      AND c.open_time < date_trunc('minute', t.signal_time + interval '15 minutes')
                      AND c.timeframe = '5') AS n_15m
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

cat > /tmp/me_b_candles.sql <<'SQL'
COPY (
WITH target AS (
  SELECT DISTINCT o.symbol, date_trunc('minute', o.signal_time) - interval '5 minutes' AS from_time,
                  date_trunc('minute', o.signal_time) + interval '240 minutes' AS to_time
  FROM research.prospective_observation o
  WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
)
SELECT
  c.exchange, c.market_type, i.symbol, c.timeframe,
  to_char(c.open_time AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  to_char(c.close_time AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  c.open, c.high, c.low, c.close, c.volume, c.turnover,
  c.is_closed, c.quality_status, c.source
FROM market.candle c
JOIN dds.instrument i ON i.instrument_id = c.instrument_id
JOIN target t
  ON i.symbol = t.symbol AND c.timeframe = '5'
 AND c.exchange = 'bybit' AND c.market_type = 'linear'
 AND c.is_closed = TRUE AND c.quality_status <> 'invalid'
 AND c.open_time >= t.from_time AND c.open_time < t.to_time
ORDER BY i.symbol, c.open_time
) TO STDOUT (FORMAT TEXT, NULL '\N')
SQL

run_exp "experiment_rows.txt" /tmp/me_b_exp_meta.sql
run_sql "dataset_pairs.tsv" "COPY (SELECT DISTINCT experiment_id, source_signal_id, observation_id, symbol, direction, signal_time, created_at, reference_price, invalidation_price, target_1, variant_entry, variant_stop, variant_target, score, rule_passed, filter_reason, market_regime, features, parameters FROM research.prospective_observation WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1') ORDER BY source_signal_id, experiment_id) TO STDOUT (FORMAT TEXT, NULL '\N')"
run_sql "outcomes.tsv" "COPY (SELECT r.observation_id, o.experiment_id, o.source_signal_id, o.symbol, o.direction, o.signal_time, o.created_at, r.mfe_15m, r.mfe_30m, r.mfe_60m, r.mfe_120m, r.mfe_240m, r.mae_15m, r.mae_30m, r.mae_60m, r.mae_120m, r.mae_240m, r.mfe_r_15m, r.mfe_r_30m, r.mfe_r_60m, r.mfe_r_120m, r.mfe_r_240m, r.mae_r_15m, r.mae_r_30m, r.mae_r_60m, r.mae_r_120m, r.mae_r_240m, r.return_at_15m, r.return_at_30m, r.return_at_60m, r.return_at_120m, r.return_at_240m, r.tp_hit, r.sl_hit, r.tp_before_sl, r.sl_before_tp, r.ambiguous_intrabar, r.time_to_tp, r.time_to_sl, r.evaluated_15m_at, r.evaluated_30m_at, r.evaluated_60m_at, r.evaluated_120m_at, r.evaluated_240m_at, r.is_final, r.created_at, r.updated_at FROM research.prospective_outcome r JOIN research.prospective_observation o ON o.observation_id = r.observation_id WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1') ORDER BY o.source_signal_id, o.experiment_id) TO STDOUT (FORMAT TEXT, NULL '\N')"
run_sql "instruments.tsv" "COPY (SELECT i.instrument_id, i.symbol, i.quote_asset, i.category, i.status FROM dds.instrument i WHERE i.symbol IN (SELECT DISTINCT symbol FROM research.prospective_observation WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1'))) TO STDOUT (FORMAT TEXT, NULL '\N')"
run_sql "candle_coverage.tsv" "COPY (WITH target AS (SELECT DISTINCT o.symbol, o.signal_time FROM research.prospective_observation o WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')) SELECT t.symbol, t.signal_time, COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time) AND c.open_time < date_trunc('minute', t.signal_time + interval '240 minutes') AND c.timeframe='5') AS n_240m, COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time) AND c.open_time < date_trunc('minute', t.signal_time + interval '120 minutes') AND c.timeframe='5') AS n_120m, COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time) AND c.open_time < date_trunc('minute', t.signal_time + interval '60 minutes') AND c.timeframe='5') AS n_60m, COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time) AND c.open_time < date_trunc('minute', t.signal_time + interval '30 minutes') AND c.timeframe='5') AS n_30m, COUNT(c.*) FILTER (WHERE c.open_time >= date_trunc('minute', t.signal_time) AND c.open_time < date_trunc('minute', t.signal_time + interval '15 minutes') AND c.timeframe='5') AS n_15m FROM target t JOIN dds.instrument i ON i.symbol=t.symbol LEFT JOIN market.candle c ON c.exchange='bybit' AND c.market_type='linear' AND c.instrument_id=i.instrument_id AND c.timeframe='5' AND c.is_closed=TRUE AND c.quality_status <> 'invalid' AND c.open_time >= date_trunc('minute', t.signal_time) AND c.open_time < date_trunc('minute', t.signal_time + interval '240 minutes') GROUP BY t.symbol,t.signal_time) TO STDOUT (FORMAT TEXT, NULL '\N')"
sudo -u postgres psql -d trad_bot -P pager=off -f /tmp/me_b_candles.sql > "$OUT_DIR/candles.tsv"

cat > /tmp/me_b_snapshot_meta.sql <<'SQL'
SELECT
  now() AS db_now,
  current_database() AS database,
  current_user AS current_user,
  current_setting('TimeZone') AS timezone,
  txid_current() AS txid,
  (SELECT COUNT(*) FROM research.prospective_observation WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')) AS observation_rows,
  (SELECT COUNT(*) FROM research.prospective_outcome r JOIN research.prospective_observation o ON o.observation_id=r.observation_id WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')) AS outcome_rows,
  (SELECT COUNT(*) FROM market.candle c JOIN dds.instrument i ON i.instrument_id=c.instrument_id WHERE i.symbol IN (SELECT DISTINCT symbol FROM research.prospective_observation WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')) AND c.timeframe='5' AND c.open_time >= (SELECT min(signal_time) - interval '5 minutes' FROM research.prospective_observation WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')) AND c.open_time < (SELECT max(signal_time) + interval '240 minutes' FROM research.prospective_observation WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1'))) AS candle_rows;
SQL
run_exp "snapshot_meta.txt" /tmp/me_b_snapshot_meta.sql

cat > /tmp/me_b_db_fingerprint.sql <<'SQL'
SELECT md5(string_agg(row_hash, E'\n' ORDER BY experiment_id, observation_id))
FROM (
  SELECT experiment_id, observation_id,
         md5(concat_ws('|', experiment_id, source_signal_id, symbol, direction,
                       signal_time, reference_price, invalidation_price, target_1,
                       variant_entry, variant_stop, variant_target, features,
                       parameters, created_at)) AS row_hash
  FROM research.prospective_observation
  WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
) x;
SQL
run_sql "observation_fingerprint.txt" "COPY (SELECT md5(string_agg(row_hash, E'\\n' ORDER BY experiment_id, observation_id)) FROM (SELECT experiment_id, observation_id, md5(concat_ws('|', experiment_id, source_signal_id, symbol, direction, signal_time, reference_price, invalidation_price, target_1, variant_entry, variant_stop, variant_target, features, parameters, created_at)) AS row_hash FROM research.prospective_observation WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')) x) TO STDOUT"

cat > /tmp/me_b_outcome_fingerprint.sql <<'SQL'
SELECT md5(string_agg(row_hash, E'\n' ORDER BY experiment_id, observation_id))
FROM (
  SELECT r.experiment_id, r.observation_id,
         md5(concat_ws('|', r.experiment_id, r.mfe_60m, r.mae_60m, r.mfe_r_60m,
                       r.mae_r_60m, r.return_at_60m, r.tp_hit, r.sl_hit,
                       r.tp_before_sl, r.sl_before_tp, r.ambiguous_intrabar,
                       r.time_to_tp, r.time_to_sl, r.evaluated_240m_at, r.is_final,
                       r.created_at, r.updated_at)) AS row_hash
  FROM research.prospective_outcome r
  JOIN research.prospective_observation o ON o.observation_id = r.observation_id
  WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')
) x;
SQL
run_sql "outcome_fingerprint.txt" "COPY (SELECT md5(string_agg(row_hash, E'\\n' ORDER BY experiment_id, observation_id)) FROM (SELECT r.experiment_id, r.observation_id, md5(concat_ws('|', r.experiment_id, r.mfe_60m, r.mae_60m, r.mfe_r_60m, r.mae_r_60m, r.return_at_60m, r.tp_hit, r.sl_hit, r.tp_before_sl, r.sl_before_tp, r.ambiguous_intrabar, r.time_to_tp, r.time_to_sl, r.evaluated_240m_at, r.is_final, r.created_at, r.updated_at)) AS row_hash FROM research.prospective_outcome r JOIN research.prospective_observation o ON o.observation_id=r.observation_id WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')) x) TO STDOUT"

{
  echo "=== EXPORT METADATA ==="
  echo "export_started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sudo -u postgres psql -d trad_bot -P pager=off -t -A \
    -c "SELECT 'db_now=' || now(), 'database=' || current_database(), 'user=' || current_user, 'timezone=' || current_setting('TimeZone'), 'txid=' || txid_current();"
  echo "git_head=$(git rev-parse HEAD)"
  echo "git_branch=$(git branch --show-current)"
  echo "observation_fingerprint=$(cat "$OUT_DIR/observation_fingerprint.txt")"
  echo "outcome_fingerprint=$(cat "$OUT_DIR/outcome_fingerprint.txt")"
  echo "=== ROW COUNTS ==="
  wc -l "$OUT_DIR"/*
  echo "=== EXPORT FINISHED ==="
  echo "export_finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$OUT_DIR/export_metadata.txt"

cd /
tar -C "$OUT_DIR" -czf /tmp/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007.tar.gz .
echo "/tmp/me_short_geometry_b_corrected_outcome_reconstruction_v1_20261007.tar.gz"
