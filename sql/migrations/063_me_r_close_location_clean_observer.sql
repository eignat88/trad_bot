-- ME_R_CLOSE_LOCATION_CLEAN_OBSERVER_REPAIR_V1
-- Add timing provenance for clean prospective observations.
-- Legacy rows remain untouched.

ALTER TABLE dds.me_r_long_close_location_oos_signal
    ADD COLUMN IF NOT EXISTS decision_time TIMESTAMPTZ;

ALTER TABLE dds.me_r_long_close_location_oos_signal
    ADD COLUMN IF NOT EXISTS signal_candle_open_time TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_me_r_cl_oos_decision_time
    ON dds.me_r_long_close_location_oos_signal (decision_time)
    WHERE decision_time IS NOT NULL;
