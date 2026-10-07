#!/bin/bash
set -euo pipefail
cd /opt/trad_bot
{
  echo "=== FINAL POST-ANALYSIS READ-ONLY FINGERPRINT ==="
  echo "verified_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sudo -u postgres psql -d trad_bot -P pager=off -t -A \
    -c "COPY (SELECT md5(string_agg(row_hash, E'\n' ORDER BY experiment_id, observation_id)) FROM (SELECT experiment_id, observation_id, md5(concat_ws('|', experiment_id, source_signal_id, symbol, direction, signal_time, reference_price, invalidation_price, target_1, variant_entry, variant_stop, variant_target, features, parameters, created_at)) AS row_hash FROM research.prospective_observation WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')) x) TO STDOUT;" \
    | awk '{print "observation_fingerprint=" $0}'
  sudo -u postgres psql -d trad_bot -P pager=off -t -A \
    -c "COPY (SELECT md5(string_agg(row_hash, E'\n' ORDER BY experiment_id, observation_id)) FROM (SELECT r.experiment_id, r.observation_id, md5(concat_ws('|', r.experiment_id, r.mfe_60m, r.mae_60m, r.mfe_r_60m, r.mae_r_60m, r.return_at_60m, r.tp_hit, r.sl_hit, r.tp_before_sl, r.sl_before_tp, r.ambiguous_intrabar, r.time_to_tp, r.time_to_sl, r.evaluated_240m_at, r.is_final, r.created_at, r.updated_at)) AS row_hash FROM research.prospective_outcome r JOIN research.prospective_observation o ON o.observation_id = r.observation_id WHERE o.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1')) x) TO STDOUT;" \
    | awk '{print "outcome_fingerprint=" $0}'
} > /tmp/me_short_geometry_b_corrected_outcome_reconstruction_v1_final_fingerprint.txt
