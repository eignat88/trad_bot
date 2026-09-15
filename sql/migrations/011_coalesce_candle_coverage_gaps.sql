-- Migration 011: Coalesce candle coverage gaps
-- Rewrites market.check_candle_coverage() to return contiguous coalesced gaps
-- instead of one row per missing candle slot.
--
-- This migration is idempotent: safe to run on both fresh installs and
-- production databases that already have the old function.

-- Idempotent: replace existing function with contiguous-gap coalescing version
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

    -- Scan each time slot and coalesce consecutive missing slots into contiguous gaps
    v_current_time := p_from;
    WHILE v_current_time < p_to LOOP
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
                -- Start a new gap
                v_gap_start := v_current_time;
                in_gap := TRUE;
            END IF;
            -- Extend the gap end
            v_gap_end := v_next_time;
        ELSE
            IF in_gap THEN
                -- Gap ended — emit the coalesced gap
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
