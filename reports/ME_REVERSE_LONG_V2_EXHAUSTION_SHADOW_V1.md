# ME_REVERSE_LONG_V2_EXHAUSTION_SHADOW_V1

> Experiment in progress — collecting data on VPS.
> Threshold: `exhaustion_magnitude <= 0.4` (fixed, no tuning)

## 1. Experiment Definition

**Hypothesis**: exhaustion_magnitude <= 0.4 filters out losing V2 entries
without removing too many winners.

**Source**: V1 historical analysis showed:
- V1 ALL: E[R] = -0.2632, PF = 0.5902
- V1 exhaustion <= 0.4: E[R] = +0.2420, PF = 1.6624

**This experiment**: Apply the same fixed threshold to V2 signals
(out-of-sample) to validate on new data.

## 2. Fixed Hypothesis

| Parameter | Value |
|-----------|-------|
| Scanner | MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 |
| Direction | LONG |
| Feature | exhaustion_magnitude |
| Threshold | <= 0.4 |
| PASS | exhaustion_magnitude <= 0.4 |
| REJECT | exhaustion_magnitude > 0.4 |
| Threshold tuning | NONE — fixed before experiment start |

## 3. Sample Size

| Metric | Value |
|--------|-------|
| Total observations | - |
| Closed outcomes | - |
| Open trades | - |
| PASS signals | - |
| REJECT signals | - |
| MISSING features | - |

**Minimum checkpoint**: 20 closed outcomes
**Preferred**: 30 closed outcomes
**Reliable**: 50+ outcomes

## 4. V2 ALL Metrics

| Metric | Value |
|--------|-------|
| Signals | - |
| Closed outcomes | - |
| Wins | - |
| Losses | - |
| Hard losses (<= -0.9R) | - |
| Win rate | - |
| Total R | - |
| E[R] | - |
| PF | - |
| Avg win R | - |
| Avg loss R | - |

## 5. EXHAUST_PASS Metrics

| Metric | Value |
|--------|-------|
| Signals | - |
| Closed outcomes | - |
| Wins | - |
| Losses | - |
| Hard losses (<= -0.9R) | - |
| Win rate | - |
| Total R | - |
| E[R] | - |
| PF | - |
| Avg win R | - |
| Avg loss R | - |

## 6. EXHAUST_REJECT Metrics

| Metric | Value |
|--------|-------|
| Signals | - |
| Closed outcomes | - |
| Wins | - |
| Losses | - |
| Hard losses (<= -0.9R) | - |
| Win rate | - |
| Total R | - |
| E[R] | - |
| PF | - |
| Avg win R | - |
| Avg loss R | - |

## 7. Hard-Loss Analysis

| Group | Hard Losses | Winners | Hard Losses / Winner |
|-------|------------|---------|---------------------|
| PASS | - | - | - |
| REJECT | - | - | - |

## 8. Winner Retention

| Metric | Value |
|--------|-------|
| Total V2 winners | - |
| Winners in PASS | - |
| Winners in REJECT | - |
| Winner retention rate | - |

## 9. PASS vs REJECT

| Metric | PASS | REJECT | Delta |
|--------|-----:|-------:|------:|
| E[R] | - | - | - |
| PF | - | - | - |
| Hard-loss rate | - | - | - |
| Win rate | - | - | - |

## 10. Conclusions

*To be filled after sufficient data collection.*

## 11. Continue / Reject Criteria

| Criterion | Threshold |
|-----------|-----------|
| E[R] PASS > E[R] ALL | Required |
| PF PASS > PF ALL | Required |
| Hard-loss rate lower in PASS | Required |
| Winner retention > 50% | Required |
| Min closed outcomes | ≥ 20 for provisional, ≥ 30 for moderate, ≥ 50 for strong |

**If any "Required" criterion fails after 30+ outcomes**: REJECT hypothesis.
**If all pass but sample < 30**: PROVISIONAL — continue collecting.
**If all pass and sample ≥ 30**: PROMISING — consider next experiment.
