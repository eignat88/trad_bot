-- Migration 009: Market Candle Storage
-- Creates market schema and candle table for OHLCV data

-- 1. Create market schema
CREATE SCHEMA IF NOT EXISTS market;

-- 2. Create candle table
CREATE TABLE IF NOT EXISTS market.candle (
    exchange TEXT NOT NULL DEFAULT 'bybit',
    market_type TEXT NOT NULL DEFAULT 'linear',
    instrument_id BIGINT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time TIMESTAMPTZ NOT NULL,
    close_time TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    turnover NUMERIC,
    is_closed BOOLEAN NOT NULL DEFAULT TRUE,
    source TEXT NOT NULL DEFAULT 'bybit_api',
    source_received_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quality_status TEXT NOT NULL DEFAULT 'validated'
        CHECK (quality_status IN ('validated', 'suspect', 'invalid')),
    
    -- Primary key (natural key)
    PRIMARY KEY (exchange, market_type, instrument_id, timeframe, open_time),
    
    -- Data quality constraints
    CHECK (open > 0),
    CHECK (high > 0),
    CHECK (low > 0),
    CHECK (close > 0),
    CHECK (volume >= 0),
    CHECK (turnover IS NULL OR turnover >= 0),
    CHECK (close_time > open_time),
    CHECK (high >= open),
    CHECK (high >= close),
    CHECK (high >= low),
    CHECK (low <= open),
    CHECK (low <= close),
    
    -- Only closed candles in analytics
    CHECK (is_closed = TRUE)
);

-- 3. Add indexes for common queries
CREATE INDEX IF NOT EXISTS idx_candle_instrument_timeframe 
    ON market.candle (instrument_id, timeframe, open_time);
CREATE INDEX IF NOT EXISTS idx_candle_open_time 
    ON market.candle (open_time);
CREATE INDEX IF NOT EXISTS idx_candle_ingested_at 
    ON market.candle (ingested_at);
CREATE INDEX IF NOT EXISTS idx_candle_quality_status 
    ON market.candle (quality_status);
CREATE INDEX IF NOT EXISTS idx_candle_exchange_market_type 
    ON market.candle (exchange, market_type);

-- 4. Create function to validate candle data
CREATE OR REPLACE FUNCTION market.validate_candle()
RETURNS TRIGGER AS $$
BEGIN
    -- Validate OHLC relationships
    IF NEW.high < NEW.open OR NEW.high < NEW.close OR NEW.high < NEW.low THEN
        RAISE EXCEPTION 'Invalid candle: high must be >= open, close, and low';
    END IF;
    
    IF NEW.low > NEW.open OR NEW.low > NEW.close THEN
        RAISE EXCEPTION 'Invalid candle: low must be <= open and close';
    END IF;
    
    -- Validate timestamps
    IF NEW.close_time <= NEW.open_time THEN
        RAISE EXCEPTION 'Invalid candle: close_time must be > open_time';
    END IF;
    
    -- Validate prices are positive
    IF NEW.open <= 0 OR NEW.high <= 0 OR NEW.low <= 0 OR NEW.close <= 0 THEN
        RAISE EXCEPTION 'Invalid candle: all prices must be > 0';
    END IF;
    
    -- Validate volume is non-negative
    IF NEW.volume < 0 THEN
        RAISE EXCEPTION 'Invalid candle: volume must be >= 0';
    END IF;
    
    -- Validate turnover is non-negative if provided
    IF NEW.turnover IS NOT NULL AND NEW.turnover < 0 THEN
        RAISE EXCEPTION 'Invalid candle: turnover must be >= 0';
    END IF;
    
    -- Mark as closed for analytics
    NEW.is_closed := TRUE;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 5. Add trigger for candle validation
CREATE TRIGGER validate_candle_data
    BEFORE INSERT OR UPDATE ON market.candle
    FOR EACH ROW
    EXECUTE FUNCTION market.validate_candle();

-- 6. Create function to calculate close_time based on timeframe
CREATE OR REPLACE FUNCTION market.calculate_close_time(
    open_time TIMESTAMPTZ,
    timeframe TEXT
) RETURNS TIMESTAMPTZ AS $$
DECLARE
    interval_minutes INTEGER;
BEGIN
    -- Convert timeframe to minutes
    CASE timeframe
        WHEN '1' THEN interval_minutes := 1;
        WHEN '3' THEN interval_minutes := 3;
        WHEN '5' THEN interval_minutes := 5;
        WHEN '15' THEN interval_minutes := 15;
        WHEN '30' THEN interval_minutes := 30;
        WHEN '60' THEN interval_minutes := 60;
        WHEN '120' THEN interval_minutes := 120;
        WHEN '240' THEN interval_minutes := 240;
        WHEN '360' THEN interval_minutes := 360;
        WHEN '720' THEN interval_minutes := 720;
        WHEN 'D' THEN interval_minutes := 1440;
        WHEN 'W' THEN interval_minutes := 10080;
        WHEN 'M' THEN interval_minutes := 43200;
        ELSE interval_minutes := 5; -- default to 5m
    END CASE;
    
    RETURN open_time + (interval_minutes || ' minutes')::INTERVAL;
END;
$$ LANGUAGE plpgsql;

-- 7. Create view for candle statistics
CREATE OR REPLACE VIEW market.candle_stats AS
SELECT 
    exchange,
    market_type,
    instrument_id,
    timeframe,
    COUNT(*) AS total_candles,
    MIN(open_time) AS earliest_candle,
    MAX(open_time) AS latest_candle,
    EXTRACT(EPOCH FROM (MAX(open_time) - MIN(open_time))) / 3600 AS coverage_hours,
    COUNT(DISTINCT DATE_TRUNC('day', open_time)) AS coverage_days
FROM market.candle
WHERE is_closed = TRUE
GROUP BY exchange, market_type, instrument_id, timeframe;

-- 8. Create view for recent candles
CREATE OR REPLACE VIEW market.recent_candles AS
SELECT 
    exchange,
    market_type,
    instrument_id,
    timeframe,
    open_time,
    close_time,
    open,
    high,
    low,
    close,
    volume,
    turnover,
    ingested_at,
    quality_status
FROM market.candle
WHERE is_closed = TRUE
ORDER BY open_time DESC
LIMIT 1000;

-- 9. Add comments for documentation
COMMENT ON TABLE market.candle IS 'OHLCV candle data from exchanges';
COMMENT ON COLUMN market.candle.exchange IS 'Exchange identifier (bybit, binance, etc.)';
COMMENT ON COLUMN market.candle.market_type IS 'Market type (linear, inverse, spot)';
COMMENT ON COLUMN market.candle.instrument_id IS 'Internal instrument ID from dds.instrument';
COMMENT ON COLUMN market.candle.timeframe IS 'Candle timeframe (1, 5, 15, 60, D, W, M)';
COMMENT ON COLUMN market.candle.open_time IS 'Candle open timestamp (inclusive)';
COMMENT ON COLUMN market.candle.close_time IS 'Candle close timestamp (exclusive)';
COMMENT ON COLUMN market.candle.is_closed IS 'TRUE only for completed candles (never partial)';
COMMENT ON COLUMN market.candle.source IS 'Data source (bybit_api, manual, etc.)';
COMMENT ON COLUMN market.candle.quality_status IS 'Data quality status after validation';

-- 10. Create function to check candle coverage for a time range
CREATE OR REPLACE FUNCTION market.check_candle_coverage(
    p_instrument_id BIGINT,
    p_timeframe TEXT,
    p_from TIMESTAMPTZ,
    p_to TIMESTAMPTZ
) RETURNS TABLE (
    gap_start TIMESTAMPTZ,
    gap_end TIMESTAMPTZ,
    gap_duration INTERVAL
) AS $$
DECLARE
    timeframe_minutes INTEGER;
    current_time TIMESTAMPTZ;
    next_time TIMESTAMPTZ;
    candle_exists BOOLEAN;
BEGIN
    -- Convert timeframe to minutes
    CASE p_timeframe
        WHEN '1' THEN timeframe_minutes := 1;
        WHEN '3' THEN timeframe_minutes := 3;
        WHEN '5' THEN timeframe_minutes := 5;
        WHEN '15' THEN timeframe_minutes := 15;
        WHEN '30' THEN timeframe_minutes := 30;
        WHEN '60' THEN timeframe_minutes := 60;
        WHEN '120' THEN timeframe_minutes := 120;
        WHEN '240' THEN timeframe_minutes := 240;
        WHEN '360' THEN timeframe_minutes := 360;
        WHEN '720' THEN timeframe_minutes := 720;
        WHEN 'D' THEN timeframe_minutes := 1440;
        WHEN 'W' THEN timeframe_minutes := 10080;
        WHEN 'M' THEN timeframe_minutes := 43200;
        ELSE timeframe_minutes := 5;
    END CASE;
    
    -- Check each time slot
    current_time := p_from;
    WHILE current_time < p_to LOOP
        next_time := current_time + (timeframe_minutes || ' minutes')::INTERVAL;
        
        -- Check if candle exists for this time slot
        SELECT EXISTS(
            SELECT 1 FROM market.candle
            WHERE instrument_id = p_instrument_id
            AND timeframe = p_timeframe
            AND open_time = current_time
            AND is_closed = TRUE
        ) INTO candle_exists;
        
        IF NOT candle_exists THEN
            gap_start := current_time;
            gap_end := next_time;
            gap_duration := next_time - current_time;
            RETURN NEXT;
        END IF;
        
        current_time := next_time;
    END LOOP;
END;
$$ LANGUAGE plpgsql;