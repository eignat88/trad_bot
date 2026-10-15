-- Issue #154: local-only market data for ME_R V3, never apply to trad_bot.
-- APPLY ONLY to the dedicated trad_bot_me_r_v3_oos database after review.
BEGIN;
DO $$
BEGIN
 IF current_database() <> 'trad_bot_me_r_v3_oos' THEN
  RAISE EXCEPTION 'ISOLATED_OOS_DATABASE_REQUIRED';
 END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS dds;
CREATE SCHEMA IF NOT EXISTS market;

CREATE TABLE IF NOT EXISTS dds.instrument (
 instrument_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 symbol TEXT NOT NULL UNIQUE CHECK (symbol ~ '^[A-Z0-9]{3,32}$'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS market.candle (
 exchange TEXT NOT NULL DEFAULT 'BYBIT' CHECK (exchange = 'BYBIT'),
 market_type TEXT NOT NULL DEFAULT 'linear' CHECK (market_type = 'linear'),
 instrument_id BIGINT NOT NULL REFERENCES dds.instrument(instrument_id),
 timeframe TEXT NOT NULL CHECK (timeframe = '5'),
 open_time TIMESTAMPTZ NOT NULL,
 close_time TIMESTAMPTZ NOT NULL,
 open NUMERIC NOT NULL CHECK (open > 0),
 high NUMERIC NOT NULL CHECK (high > 0),
 low NUMERIC NOT NULL CHECK (low > 0),
 close NUMERIC NOT NULL CHECK (close > 0),
 volume NUMERIC NOT NULL DEFAULT 0 CHECK (volume >= 0),
 turnover NUMERIC,
 is_closed BOOLEAN NOT NULL DEFAULT TRUE CHECK (is_closed),
 source TEXT NOT NULL DEFAULT 'BYBIT_REST_PROSPECTIVE',
 source_received_at TIMESTAMPTZ NOT NULL,
 ingested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 quality_status TEXT NOT NULL DEFAULT 'validated' CHECK (quality_status = 'validated'),
 PRIMARY KEY (exchange, market_type, instrument_id, timeframe, open_time),
 CHECK (close_time = open_time + INTERVAL '5 minutes'),
 CHECK (high >= GREATEST(open, close)),
 CHECK (low <= LEAST(open, close)),
 CHECK (high >= low),
 CHECK (turnover IS NULL OR turnover >= 0),
 CHECK (source_received_at >= close_time)
);

CREATE INDEX IF NOT EXISTS me_v3_local_candle_window_idx
 ON market.candle (instrument_id, timeframe, close_time);

-- Existing role was created by the operator. No rights to production trad_bot.
GRANT USAGE ON SCHEMA dds, market TO trad_bot_me_r_v3_oos;
GRANT SELECT, INSERT ON dds.instrument TO trad_bot_me_r_v3_oos;
GRANT USAGE ON SEQUENCE dds.instrument_instrument_id_seq TO trad_bot_me_r_v3_oos;
GRANT SELECT, INSERT ON market.candle TO trad_bot_me_r_v3_oos;
COMMIT;
