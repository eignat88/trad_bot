-- Migration 012: Align candle coverage grid
-- Fixes the issue where trade timestamps with arbitrary seconds/microseconds
-- (e.g. 2026-08-28 11:12:15.976987) never match candle open_times aligned
-- to timeframe boundaries (e.g. 11:10:00, 11:15:00 for 5m).
--
-- The updated function aligns p_from DOWN to the nearest candle boundary
-- and p_to UP to the next candle boundary, then scans canonical open times.
--
-- Idempotent: safe to run on both fresh installs and production.

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
    v_current_time TIMESTAMPTZ;
    v_next_time TIMESTAMPTZ;
    v_gap_start TIMESTAMPTZ;
    v_gap_end TIMESTAMPTZ;
    candle_exists BOOLEAN;
    in_gap BOOLEAN := FALSE;
    aligned_from TIMESTAMPTZ;
    aligned_to TIMESTAMPTZ;
    total_minutes INTEGER;
    aligned_minute INTEGER;
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

    -- Align p_from DOWN to the nearest candle boundary
    IF timeframe_minutes >= 1440 THEN
        -- Daily/weekly/monthly: align to midnight UTC
        aligned_from := date_trunc('day', p_from AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    ELSE
        -- Minute/hour: align down to the nearest boundary
        total_minutes := EXTRACT(HOUR FROM p_from AT TIME ZONE 'UTC') * 60
                        + EXTRACT(MINUTE FROM p_from AT TIME ZONE 'UTC');
        aligned_minute := (total_minutes / timeframe_minutes) * timeframe_minutes;
        aligned_from := date_trunc('day', p_from AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                       + (aligned_minute || ' minutes')::INTERVAL;
    END IF;

    -- Align p_to UP to the next candle boundary (so we scan past the end)
    IF timeframe_minutes >= 1440 THEN
        aligned_to := date_trunc('day', p_to AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                    + (timeframe_minutes || ' minutes')::INTERVAL;
    ELSE
        total_minutes := EXTRACT(HOUR FROM p_to AT TIME ZONE 'UTC') * 60
                        + EXTRACT(MINUTE FROM p_to AT TIME ZONE 'UTC');
        aligned_minute := CEIL(total_minutes::NUMERIC / timeframe_minutes) * timeframe_minutes;
        aligned_to := date_trunc('day', p_to AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                    + (aligned_minute || ' minutes')::INTERVAL;
    END IF;

    -- Scan each aligned time slot and coalesce consecutive missing slots
    v_current_time := aligned_from;
    WHILE v_current_time < aligned_to LOOP
        v_next_time := v_current_time + (timeframe_minutes || ' minutes')::INTERVAL;

        SELECT EXISTS(
            SELECT 1 FROM market.candle
            WHERE instrument_id = p_instrument_id
              AND timeframe = p_timeframe
              AND open_time = v_current_time
              AND is_closed = TRUE
        ) INTO candle_exists;

        IF NOT candle_exists THEN
            IF NOT in_gap THEN
                v_gap_start := v_current_time;
                in_gap := TRUE;
            END IF;
            v_gap_end := v_next_time;
        ELSE
            IF in_gap THEN
                gap_start := v_gap_start;
                gap_end := v_gap_end;
                gap_duration := v_gap_end - v_gap_start;
                RETURN NEXT;
                in_gap := FALSE;
            END IF;
        END IF;

        v_current_time := v_next_time;
    END LOOP;

    -- Emit any trailing gap
    IF in_gap THEN
        gap_start := v_gap_start;
        gap_end := v_gap_end;
        gap_duration := v_gap_end - v_gap_start;
        RETURN NEXT;
    END IF;
END;
$$ LANGUAGE plpgsql;
