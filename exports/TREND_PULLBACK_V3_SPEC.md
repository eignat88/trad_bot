# TREND_PULLBACK_V3 Scanner Specification

## Overview

TREND_PULLBACK_V3 is an edge-optimized variant of the TREND_PULLBACK scanner family. It incorporates findings from extensive data enrichment and edge analysis performed on VPS with 15,528 funding rate records, 551,468 candles (15m + 1h), 8 sectors, and 13 instrument mappings.

## Edge Analysis Results

The V3 parameters were derived from a comprehensive edge space analysis of 3 scanners × 13 symbols × 50 features:

```
Win Rate:    85.3% (1169/1371)
Avg R:       +0.378R per trade
Kelly:       1.12 (half-Kelly = 0.56)
Hit 0.5R:    94.2%
Hit 1.0R:    83.9%
Hit 2.0R:    66.2%

Q1 2025:     87.1% WR, +0.454R
Q2 2025:     76.2% WR, +0.200R

All 6 months profitable:
Jan: 87.1% | Feb: 87.3% | Mar: 94.4%
Apr: 87.2% | May: 74.0% | Jun: 69.2%
```

## Key Differences from V2

| Feature | V2 | V3 |
|---------|----|----|
| RSI Filter | RSI < 55 (cooled) | RSI > 60 (momentum confirmation) |
| ADX Filter | None | ADX > 35 (strong trend) |
| EMA50 Slope | None | slope > 0 (rising trend) |
| Hour Filter | None | hour 6-23, excl 10,15,19 |
| Symbol Exclusions | None | ONDOUSDT, BNBUSDT, SOLUSDT |
| pullback_tolerance | 0.012 | 0.012 |
| target_r | 0.50 | 0.50 |
| expire_at_breakeven | True | True |

## Scanner Parameters

### Default Configuration

```python
TrendPullbackScannerV3(
    pullback_tolerance=0.012,      # Price proximity to EMA
    rsi_threshold=60.0,            # Minimum RSI for momentum
    adx_threshold=35.0,            # Minimum ADX for trend strength
    ema50_slope_min=0.0,           # Minimum EMA50 slope (must be > 0)
    enabled_directions=("LONG",),  # LONG only by default
    allowed_regimes=("TREND_UP",), # Trend-up regime only
    signal_timeframe="15m",        # Signal timeframe
    max_pullback_quality=0.75,     # Maximum pullback quality
    target_r=0.50,                 # Risk/reward target
    stop_buffer=0.002,             # Stop loss buffer
    hour_start=6,                  # Trading window start (UTC)
    hour_end=23,                   # Trading window end (UTC)
    excluded_hours=frozenset({10, 15, 19}),  # Excluded hours
    excluded_symbols=frozenset({"ONDOUSDT", "BNBUSDT", "SOLUSDT"}),
)
```

## Signal Logic (LONG)

### Pre-filters (in order)

1. **Symbol Exclusion**: Reject if symbol in excluded list
2. **Hour Filter**: Reject if hour < 6, hour > 23, or hour in {10, 15, 19}
3. **Data Requirements**: Need 50+ 1h candles, 30+ 15m candles, 20+ 5m candles
4. **EMA Values**: All EMAs must be positive
5. **Trend Alignment**: EMA20 > EMA50 and 1h close > EMA200
6. **ADX Filter**: ADX >= 35 (strong trend)
7. **EMA50 Slope**: slope > 0 (rising trend)
8. **Pullback Detection**: Price within 1.2% of EMA20 or EMA50
9. **RSI Momentum**: RSI >= 60 (momentum confirmation)
10. **Signal Candle**: Must be bullish (close > open)

### Entry/Exit

- **Entry Zone**: Around EMA20/EMA50 (±0.2%)
- **Invalidation**: Low of last 3 candles × (1 - 0.002)
- **Target**: Entry + risk × 0.50
- **Expiry**: BREAKEVEN policy (close at 0R on expiry)

## Indicators Added

### ADX (Average Directional Index)

- Period: 14
- Wilder's smoothing
- Range: 0-100
- Interpretation: > 25 = trending, > 35 = strong trend, > 50 = very strong

### EMA50 Slope

- Computed as percentage change over 5 bars
- Positive = rising trend
- Negative = falling trend

## Implementation Plan

### Phase 1: Indicators (Completed)
- [x] Add `adx()` function to `app/indicators/technical.py`
- [x] Add `ema_slope()` function to `app/indicators/technical.py`
- [x] Update `IndicatorSnapshot` model with `adx` and `ema50_slope`
- [x] Update `context_builder.py` to compute new indicators

### Phase 2: Scanner (Completed)
- [x] Create `app/scanners/trend_pullback_v3.py`
- [x] Implement all filters (RSI, ADX, EMA50 slope, hour, symbol)
- [x] Implement scoring logic
- [x] Register in `orchestrator.py`

### Phase 3: Testing (Completed)
- [x] Unit tests for all filters
- [x] Unit tests for edge cases
- [x] Orchestrator integration tests
- [x] Update existing test counts

### Phase 4: Deployment
- [ ] Deploy to VPS
- [ ] Verify scanner runs correctly
- [ ] Monitor signal generation
- [ ] Compare V2 vs V3 performance

## Risk Considerations

1. **Over-optimization**: Parameters derived from historical data; may not generalize
2. **Market regime change**: V3 is optimized for TREND_UP; may miss opportunities in RANGE
3. **Symbol exclusions**: ONDO, BNB, SOL excluded based on edge analysis; may be re-added later
4. **Hour filter**: Excludes hours 10, 15, 19; may miss signals during these periods

## Monitoring

After deployment, monitor:
- Signal generation rate vs V2
- Win rate in live trading
- Average R per trade
- ADX distribution of signals
- EMA50 slope distribution
- Hour distribution of signals

## Future Enhancements

1. SHORT support (currently LONG-only)
2. Dynamic hour filter based on volatility
3. Adaptive ADX threshold based on market regime
4. Integration with funding rate data
5. Sector-based filtering
