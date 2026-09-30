#!/usr/bin/env python3
"""
ATR_WICK V2D POST-MORTEM & V3 HYPOTHESIS DISCOVERY
Date: 2026-09-29
Scope: READ-ONLY analysis
"""

import psycopg2
import pandas as pd
import numpy as np
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# Database connection
DB_CONFIG = {
    'host': 'localhost',
    'database': 'trad_bot',
    'user': 'postgres',
    'password': 'qwerty'
}

def get_connection():
    return psycopg2.connect(**DB_CONFIG)

def execute_query(conn, query):
    return pd.read_sql_query(query, conn)

def analyze_post_mortem():
    """Comprehensive post-mortem analysis of V2_D failure"""
    
    conn = get_connection()
    
    print("=" * 80)
    print("ATR_WICK V2D POST-MORTEM ANALYSIS")
    print("=" * 80)
    
    # 1. Basic statistics
    query_basic = """
    SELECT 
        COUNT(*) as total_signals,
        COUNT(DISTINCT symbol) as distinct_symbols,
        MIN(signal_time) as first_signal,
        MAX(signal_time) as last_signal
    FROM dds.v2d_signal
    WHERE experiment_id = 'ATR_WICK_FILTER_OOS_V2_D';
    """
    
    df_basic = execute_query(conn, query_basic)
    print("\n1. BASIC STATISTICS:")
    print(df_basic.to_string(index=False))
    
    # 2. StochRSI distribution
    query_stoch_rsi = """
    SELECT 
        CASE 
            WHEN stoch_rsi < 0.20 THEN '[0.00, 0.20)'
            WHEN stoch_rsi >= 0.20 AND stoch_rsi < 0.40 THEN '[0.20, 0.40)'
            WHEN stoch_rsi >= 0.40 AND stoch_rsi < 0.60 THEN '[0.40, 0.60)'
            WHEN stoch_rsi >= 0.60 AND stoch_rsi < 0.80 THEN '[0.60, 0.80)'
            WHEN stoch_rsi >= 0.80 THEN '[0.80, 1.00]'
            ELSE 'NULL'
        END as stoch_rsi_bucket,
        COUNT(*) as n,
        COUNT(DISTINCT symbol) as symbols,
        ROUND(AVG(stoch_rsi)::numeric, 4) as avg_stoch_rsi,
        MIN(stoch_rsi) as min_stoch_rsi,
        MAX(stoch_rsi) as max_stoch_rsi
    FROM dds.v2d_signal
    WHERE experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY 
        CASE 
            WHEN stoch_rsi < 0.20 THEN '[0.00, 0.20)'
            WHEN stoch_rsi >= 0.20 AND stoch_rsi < 0.40 THEN '[0.20, 0.40)'
            WHEN stoch_rsi >= 0.40 AND stoch_rsi < 0.60 THEN '[0.40, 0.60)'
            WHEN stoch_rsi >= 0.60 AND stoch_rsi < 0.80 THEN '[0.60, 0.80)'
            WHEN stoch_rsi >= 0.80 THEN '[0.80, 1.00]'
            ELSE 'NULL'
        END
    ORDER BY stoch_rsi_bucket;
    """
    
    df_stoch_rsi = execute_query(conn, query_stoch_rsi)
    print("\n2. STOCHRSI DISTRIBUTION:")
    print(df_stoch_rsi.to_string(index=False))
    
    # 3. Wick geometry analysis
    query_wick = """
    SELECT 
        CASE 
            WHEN wick_size / NULLIF(high - low, 0) < 0.3 THEN 'LOW_WICK'
            WHEN wick_size / NULLIF(high - low, 0) >= 0.3 AND wick_size / NULLIF(high - low, 0) < 0.6 THEN 'MID_WICK'
            WHEN wick_size / NULLIF(high - low, 0) >= 0.6 THEN 'HIGH_WICK'
            ELSE 'NULL'
        END as wick_bucket,
        COUNT(*) as n,
        COUNT(DISTINCT symbol) as symbols,
        ROUND(AVG(wick_size / NULLIF(high - low, 0))::numeric, 4) as avg_wick_ratio,
        COUNT(CASE WHEN o.mfe_60m IS NOT NULL THEN 1 END) as evaluated,
        ROUND(AVG(CASE WHEN o.mfe_60m IS NOT NULL THEN o.mfe_60m END)::numeric, 4) as avg_mfe_60m,
        ROUND(AVG(CASE WHEN o.mae_60m IS NOT NULL THEN o.mae_60m END)::numeric, 4) as avg_mae_60m,
        COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END) as good,
        COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END) as bad,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              NULLIF(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END), 0), 2) as good_bad_ratio,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as good_pct,
        ROUND(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as bad_pct
    FROM dds.v2d_signal s
    LEFT JOIN dds.v2d_outcome o ON s.signal_id = o.signal_id
    WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY 
        CASE 
            WHEN wick_size / NULLIF(high - low, 0) < 0.3 THEN 'LOW_WICK'
            WHEN wick_size / NULLIF(high - low, 0) >= 0.3 AND wick_size / NULLIF(high - low, 0) < 0.6 THEN 'MID_WICK'
            WHEN wick_size / NULLIF(high - low, 0) >= 0.6 THEN 'HIGH_WICK'
            ELSE 'NULL'
        END
    ORDER BY wick_bucket;
    """
    
    df_wick = execute_query(conn, query_wick)
    print("\n3. WICK GEOMETRY ANALYSIS:")
    print(df_wick.to_string(index=False))
    
    # 4. ATR regime analysis
    query_atr = """
    WITH atr_stats AS (
        SELECT 
            s.signal_id,
            s.atr_pct,
            PERCENT_RANK() OVER (ORDER BY s.atr_pct) as atr_percentile
        FROM dds.v2d_signal s
        WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    )
    SELECT 
        CASE 
            WHEN atr_percentile < 0.33 THEN 'LOW_ATR'
            WHEN atr_percentile >= 0.33 AND atr_percentile < 0.66 THEN 'MID_ATR'
            WHEN atr_percentile >= 0.66 THEN 'HIGH_ATR'
            ELSE 'NULL'
        END as atr_regime,
        COUNT(*) as n,
        COUNT(DISTINCT s.symbol) as symbols,
        ROUND(AVG(s.atr_pct)::numeric, 4) as avg_atr_pct,
        COUNT(CASE WHEN o.mfe_60m IS NOT NULL THEN 1 END) as evaluated,
        ROUND(AVG(CASE WHEN o.mfe_60m IS NOT NULL THEN o.mfe_60m END)::numeric, 4) as avg_mfe_60m,
        ROUND(AVG(CASE WHEN o.mae_60m IS NOT NULL THEN o.mae_60m END)::numeric, 4) as avg_mae_60m,
        COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END) as good,
        COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END) as bad,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              NULLIF(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END), 0), 2) as good_bad_ratio,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as good_pct,
        ROUND(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as bad_pct
    FROM dds.v2d_signal s
    LEFT JOIN dds.v2d_outcome o ON s.signal_id = o.signal_id
    JOIN atr_stats a ON s.signal_id = a.signal_id
    WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY 
        CASE 
            WHEN atr_percentile < 0.33 THEN 'LOW_ATR'
            WHEN atr_percentile >= 0.33 AND atr_percentile < 0.66 THEN 'MID_ATR'
            WHEN atr_percentile >= 0.66 THEN 'HIGH_ATR'
            ELSE 'NULL'
        END
    ORDER BY atr_regime;
    """
    
    df_atr = execute_query(conn, query_atr)
    print("\n4. ATR REGIME ANALYSIS:")
    print(df_atr.to_string(index=False))
    
    # 5. Volume regime analysis
    query_vol = """
    WITH vol_stats AS (
        SELECT 
            s.signal_id,
            s.volume_ratio,
            PERCENT_RANK() OVER (ORDER BY s.volume_ratio) as volume_percentile
        FROM dds.v2d_signal s
        WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    )
    SELECT 
        CASE 
            WHEN volume_percentile < 0.33 THEN 'LOW_VOL'
            WHEN volume_percentile >= 0.33 AND volume_percentile < 0.66 THEN 'MID_VOL'
            WHEN volume_percentile >= 0.66 THEN 'HIGH_VOL'
            ELSE 'NULL'
        END as volume_regime,
        COUNT(*) as n,
        COUNT(DISTINCT s.symbol) as symbols,
        ROUND(AVG(s.volume_ratio)::numeric, 4) as avg_volume_ratio,
        COUNT(CASE WHEN o.mfe_60m IS NOT NULL THEN 1 END) as evaluated,
        ROUND(AVG(CASE WHEN o.mfe_60m IS NOT NULL THEN o.mfe_60m END)::numeric, 4) as avg_mfe_60m,
        ROUND(AVG(CASE WHEN o.mae_60m IS NOT NULL THEN o.mae_60m END)::numeric, 4) as avg_mae_60m,
        COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END) as good,
        COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END) as bad,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              NULLIF(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END), 0), 2) as good_bad_ratio,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as good_pct,
        ROUND(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as bad_pct
    FROM dds.v2d_signal s
    LEFT JOIN dds.v2d_outcome o ON s.signal_id = o.signal_id
    JOIN vol_stats v ON s.signal_id = v.signal_id
    WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY 
        CASE 
            WHEN volume_percentile < 0.33 THEN 'LOW_VOL'
            WHEN volume_percentile >= 0.33 AND volume_percentile < 0.66 THEN 'MID_VOL'
            WHEN volume_percentile >= 0.66 THEN 'HIGH_VOL'
            ELSE 'NULL'
        END
    ORDER BY volume_regime;
    """
    
    df_vol = execute_query(conn, query_vol)
    print("\n5. VOLUME REGIME ANALYSIS:")
    print(df_vol.to_string(index=False))
    
    # 6. Trend regime analysis
    query_trend = """
    SELECT 
        CASE 
            WHEN ema_slope < -0.01 THEN 'STRONG_DOWN'
            WHEN ema_slope >= -0.01 AND ema_slope < 0.01 THEN 'FLAT'
            WHEN ema_slope >= 0.01 THEN 'STRONG_UP'
            ELSE 'NULL'
        END as trend_regime,
        COUNT(*) as n,
        COUNT(DISTINCT symbol) as symbols,
        ROUND(AVG(ema_slope)::numeric, 4) as avg_ema_slope,
        COUNT(CASE WHEN o.mfe_60m IS NOT NULL THEN 1 END) as evaluated,
        ROUND(AVG(CASE WHEN o.mfe_60m IS NOT NULL THEN o.mfe_60m END)::numeric, 4) as avg_mfe_60m,
        ROUND(AVG(CASE WHEN o.mae_60m IS NOT NULL THEN o.mae_60m END)::numeric, 4) as avg_mae_60m,
        COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END) as good,
        COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END) as bad,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              NULLIF(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END), 0), 2) as good_bad_ratio,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as good_pct,
        ROUND(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as bad_pct
    FROM dds.v2d_signal s
    LEFT JOIN dds.v2d_outcome o ON s.signal_id = o.signal_id
    WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY 
        CASE 
            WHEN ema_slope < -0.01 THEN 'STRONG_DOWN'
            WHEN ema_slope >= -0.01 AND ema_slope < 0.01 THEN 'FLAT'
            WHEN ema_slope >= 0.01 THEN 'STRONG_UP'
            ELSE 'NULL'
        END
    ORDER BY trend_regime;
    """
    
    df_trend = execute_query(conn, query_trend)
    print("\n6. TREND REGIME ANALYSIS:")
    print(df_trend.to_string(index=False))
    
    # 7. Time-of-day analysis
    query_time = """
    SELECT 
        CASE 
            WHEN EXTRACT(HOUR FROM signal_time) >= 0 AND EXTRACT(HOUR FROM signal_time) < 6 THEN '00-06'
            WHEN EXTRACT(HOUR FROM signal_time) >= 6 AND EXTRACT(HOUR FROM signal_time) < 12 THEN '06-12'
            WHEN EXTRACT(HOUR FROM signal_time) >= 12 AND EXTRACT(HOUR FROM signal_time) < 18 THEN '12-18'
            WHEN EXTRACT(HOUR FROM signal_time) >= 18 THEN '18-24'
            ELSE 'NULL'
        END as time_bucket,
        COUNT(*) as n,
        COUNT(DISTINCT symbol) as symbols,
        COUNT(CASE WHEN o.mfe_60m IS NOT NULL THEN 1 END) as evaluated,
        ROUND(AVG(CASE WHEN o.mfe_60m IS NOT NULL THEN o.mfe_60m END)::numeric, 4) as avg_mfe_60m,
        ROUND(AVG(CASE WHEN o.mae_60m IS NOT NULL THEN o.mae_60m END)::numeric, 4) as avg_mae_60m,
        COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END) as good,
        COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END) as bad,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              NULLIF(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END), 0), 2) as good_bad_ratio,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as good_pct,
        ROUND(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as bad_pct
    FROM dds.v2d_signal s
    LEFT JOIN dds.v2d_outcome o ON s.signal_id = o.signal_id
    WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY 
        CASE 
            WHEN EXTRACT(HOUR FROM signal_time) >= 0 AND EXTRACT(HOUR FROM signal_time) < 6 THEN '00-06'
            WHEN EXTRACT(HOUR FROM signal_time) >= 6 AND EXTRACT(HOUR FROM signal_time) < 12 THEN '06-12'
            WHEN EXTRACT(HOUR FROM signal_time) >= 12 AND EXTRACT(HOUR FROM signal_time) < 18 THEN '12-18'
            WHEN EXTRACT(HOUR FROM signal_time) >= 18 THEN '18-24'
            ELSE 'NULL'
        END
    ORDER BY time_bucket;
    """
    
    df_time = execute_query(conn, query_time)
    print("\n7. TIME-OF-DAY ANALYSIS:")
    print(df_time.to_string(index=False))
    
    # 8. Temporal stability (by day)
    query_temporal = """
    SELECT 
        DATE(signal_time) as signal_date,
        COUNT(*) as n,
        COUNT(DISTINCT symbol) as symbols,
        COUNT(CASE WHEN o.mfe_60m IS NOT NULL THEN 1 END) as evaluated,
        ROUND(AVG(CASE WHEN o.mfe_60m IS NOT NULL THEN o.mfe_60m END)::numeric, 4) as avg_mfe_60m,
        ROUND(AVG(CASE WHEN o.mae_60m IS NOT NULL THEN o.mae_60m END)::numeric, 4) as avg_mae_60m,
        COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END) as good,
        COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END) as bad,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              NULLIF(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END), 0), 2) as good_bad_ratio,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as good_pct,
        ROUND(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as bad_pct
    FROM dds.v2d_signal s
    LEFT JOIN dds.v2d_outcome o ON s.signal_id = o.signal_id
    WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY DATE(signal_time)
    ORDER BY signal_date;
    """
    
    df_temporal = execute_query(conn, query_temporal)
    print("\n8. TEMPORAL STABILITY (BY DAY):")
    print(df_temporal.to_string(index=False))
    
    # 9. Interaction effects - Wick × ATR
    query_wick_atr = """
    WITH combined_stats AS (
        SELECT 
            s.signal_id,
            s.wick_size / NULLIF(s.high - s.low, 0) as wick_ratio,
            s.atr_pct,
            PERCENT_RANK() OVER (ORDER BY s.atr_pct) as atr_percentile
        FROM dds.v2d_signal s
        WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    )
    SELECT 
        CASE 
            WHEN cs.wick_ratio < 0.3 THEN 'LOW_WICK'
            WHEN cs.wick_ratio >= 0.3 AND cs.wick_ratio < 0.6 THEN 'MID_WICK'
            WHEN cs.wick_ratio >= 0.6 THEN 'HIGH_WICK'
            ELSE 'NULL'
        END as wick_bucket,
        CASE 
            WHEN cs.atr_percentile < 0.33 THEN 'LOW_ATR'
            WHEN cs.atr_percentile >= 0.33 AND cs.atr_percentile < 0.66 THEN 'MID_ATR'
            WHEN cs.atr_percentile >= 0.66 THEN 'HIGH_ATR'
            ELSE 'NULL'
        END as atr_regime,
        COUNT(*) as n,
        COUNT(DISTINCT s.symbol) as symbols,
        COUNT(CASE WHEN o.mfe_60m IS NOT NULL THEN 1 END) as evaluated,
        ROUND(AVG(CASE WHEN o.mfe_60m IS NOT NULL THEN o.mfe_60m END)::numeric, 4) as avg_mfe_60m,
        ROUND(AVG(CASE WHEN o.mae_60m IS NOT NULL THEN o.mae_60m END)::numeric, 4) as avg_mae_60m,
        COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END) as good,
        COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END) as bad,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              NULLIF(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END), 0), 2) as good_bad_ratio,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as good_pct,
        ROUND(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as bad_pct
    FROM dds.v2d_signal s
    LEFT JOIN dds.v2d_outcome o ON s.signal_id = o.signal_id
    JOIN combined_stats cs ON s.signal_id = cs.signal_id
    WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY wick_bucket, atr_regime
    ORDER BY wick_bucket, atr_regime;
    """
    
    df_wick_atr = execute_query(conn, query_wick_atr)
    print("\n9. INTERACTION EFFECTS - WICK × ATR:")
    print(df_wick_atr.to_string(index=False))
    
    # 10. Interaction effects - StochRSI × ATR
    query_stoch_atr = """
    WITH combined_stats AS (
        SELECT 
            s.signal_id,
            s.stoch_rsi,
            s.atr_pct,
            PERCENT_RANK() OVER (ORDER BY s.atr_pct) as atr_percentile
        FROM dds.v2d_signal s
        WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    )
    SELECT 
        CASE 
            WHEN cs.stoch_rsi < 0.20 THEN '[0.00, 0.20)'
            WHEN cs.stoch_rsi >= 0.20 AND cs.stoch_rsi < 0.40 THEN '[0.20, 0.40)'
            WHEN cs.stoch_rsi >= 0.40 AND cs.stoch_rsi < 0.60 THEN '[0.40, 0.60)'
            WHEN cs.stoch_rsi >= 0.60 AND cs.stoch_rsi < 0.80 THEN '[0.60, 0.80)'
            WHEN cs.stoch_rsi >= 0.80 THEN '[0.80, 1.00]'
            ELSE 'NULL'
        END as stoch_rsi_bucket,
        CASE 
            WHEN cs.atr_percentile < 0.33 THEN 'LOW_ATR'
            WHEN cs.atr_percentile >= 0.33 AND cs.atr_percentile < 0.66 THEN 'MID_ATR'
            WHEN cs.atr_percentile >= 0.66 THEN 'HIGH_ATR'
            ELSE 'NULL'
        END as atr_regime,
        COUNT(*) as n,
        COUNT(DISTINCT s.symbol) as symbols,
        COUNT(CASE WHEN o.mfe_60m IS NOT NULL THEN 1 END) as evaluated,
        ROUND(AVG(CASE WHEN o.mfe_60m IS NOT NULL THEN o.mfe_60m END)::numeric, 4) as avg_mfe_60m,
        ROUND(AVG(CASE WHEN o.mae_60m IS NOT NULL THEN o.mae_60m END)::numeric, 4) as avg_mae_60m,
        COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END) as good,
        COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END) as bad,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              NULLIF(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END), 0), 2) as good_bad_ratio,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as good_pct,
        ROUND(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as bad_pct
    FROM dds.v2d_signal s
    LEFT JOIN dds.v2d_outcome o ON s.signal_id = o.signal_id
    JOIN combined_stats cs ON s.signal_id = cs.signal_id
    WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY stoch_rsi_bucket, atr_regime
    ORDER BY stoch_rsi_bucket, atr_regime;
    """
    
    df_stoch_atr = execute_query(conn, query_stoch_atr)
    print("\n10. INTERACTION EFFECTS - STOCHRSI × ATR:")
    print(df_stoch_atr.to_string(index=False))
    
    # 11. Interaction effects - Wick × Volume
    query_wick_vol = """
    WITH combined_stats AS (
        SELECT 
            s.signal_id,
            s.wick_size / NULLIF(s.high - s.low, 0) as wick_ratio,
            s.volume_ratio,
            PERCENT_RANK() OVER (ORDER BY s.volume_ratio) as volume_percentile
        FROM dds.v2d_signal s
        WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    )
    SELECT 
        CASE 
            WHEN cs.wick_ratio < 0.3 THEN 'LOW_WICK'
            WHEN cs.wick_ratio >= 0.3 AND cs.wick_ratio < 0.6 THEN 'MID_WICK'
            WHEN cs.wick_ratio >= 0.6 THEN 'HIGH_WICK'
            ELSE 'NULL'
        END as wick_bucket,
        CASE 
            WHEN cs.volume_percentile < 0.33 THEN 'LOW_VOL'
            WHEN cs.volume_percentile >= 0.33 AND cs.volume_percentile < 0.66 THEN 'MID_VOL'
            WHEN cs.volume_percentile >= 0.66 THEN 'HIGH_VOL'
            ELSE 'NULL'
        END as volume_regime,
        COUNT(*) as n,
        COUNT(DISTINCT s.symbol) as symbols,
        COUNT(CASE WHEN o.mfe_60m IS NOT NULL THEN 1 END) as evaluated,
        ROUND(AVG(CASE WHEN o.mfe_60m IS NOT NULL THEN o.mfe_60m END)::numeric, 4) as avg_mfe_60m,
        ROUND(AVG(CASE WHEN o.mae_60m IS NOT NULL THEN o.mae_60m END)::numeric, 4) as avg_mae_60m,
        COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END) as good,
        COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END) as bad,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              NULLIF(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END), 0), 2) as good_bad_ratio,
        ROUND(COUNT(CASE WHEN o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as good_pct,
        ROUND(COUNT(CASE WHEN o.mae_60m >= 1.0 AND o.mfe_60m < 1.0 THEN 1 END)::numeric / 
              COUNT(*) * 100, 2) as bad_pct
    FROM dds.v2d_signal s
    LEFT JOIN dds.v2d_outcome o ON s.signal_id = o.signal_id
    JOIN combined_stats cs ON s.signal_id = cs.signal_id
    WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    GROUP BY wick_bucket, volume_regime
    ORDER BY wick_bucket, volume_regime;
    """
    
    df_wick_vol = execute_query(conn, query_wick_vol)
    print("\n11. INTERACTION EFFECTS - WICK × VOLUME:")
    print(df_wick_vol.to_string(index=False))
    
    conn.close()
    
    return {
        'basic': df_basic,
        'stoch_rsi': df_stoch_rsi,
        'wick': df_wick,
        'atr': df_atr,
        'volume': df_vol,
        'trend': df_trend,
        'time': df_time,
        'temporal': df_temporal,
        'wick_atr': df_wick_atr,
        'stoch_atr': df_stoch_atr,
        'wick_vol': df_wick_vol
    }

if __name__ == "__main__":
    results = analyze_post_mortem()
    print("\n" + "=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)