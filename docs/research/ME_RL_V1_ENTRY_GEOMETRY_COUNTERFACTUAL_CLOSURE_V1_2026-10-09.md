# ME_RL_V1 Entry Geometry Counterfactual Closure

Date: 2026-10-09

## Research verdict

**NO_POSITIVE_EXECUTABLE_EDGE_DEMONSTRATED**

Closure scope: frozen 802-observation historical diagnostic dataset.

No production deployment, paper enablement, or shadow promotion is authorized.

## Models

| Model | Finalized | Net E[R] | PF | WR |
|---|---:|---:|---:|---:|
| V1_AS_RECORDED | 713 | -0.313386 | 0.2901 | 33.94% |
| V1_CLOSE_ANCHORED_CF | 797 | -0.154810 | 0.6586 | 37.52% |
| V1_SOURCE_PRICE_ANCHORED_CF | 797 | -0.125391 | 0.7138 | 39.52% |
| V1_NEXT_BAR_OPEN_CF | 797 | -0.154810 | 0.6586 | 37.52% |

## Statistical robustness

- Model C day-cluster 95% CI: [-0.2964299149180475, 0.018733771074251334]
- Paired C minus A: N=713, mean=0.189972 R, CI=[0.1382912599297989, 0.24967433953169443]
- Paired D minus C: N=797, mean=-0.029420 R, CI=[-0.05896442235782899, -0.002901199983226829]
- B versus D mismatches: 0

The paired improvement C versus A does not establish positive absolute expectancy.

## Source integrity

- Frozen observations: 802
- Historical 5m candles: 10426
- Complete 240m paths: 797
- Incomplete XAGUSDT observations: 30412, 30419, 30430, 30435, 30446
- XRPUSDT #20839: source-price range mismatch
- Original production candle response and fill timing: unverified

## Methodological limitations

- All models are diagnostic counterfactuals.
- Next-bar open is a fill proxy, not an actual fill.
- 240m path starts after the signal's current 5m bucket.
- Five XAGUSDT observations have incomplete candle paths.
- One XRPUSDT source price lies slightly outside the candle range.
- The 802 observations are historical and not an independent prospective OOS.
- Bootstrap clusters by UTC day, not jointly by day and symbol.
- MFE/MAE are not validated as to-exit excursion metrics.
- A confidence interval including zero does not prove negative true expectancy.

## Operational decision

- Shadow-control pipeline deployment: BLOCKED
- Paper/live: NO_DEPLOY
- Registry: no changes
- Current frozen V1 hypothesis: no new OOS authorization
- No claim that true expectancy is statistically proven negative

## Reproducibility

SHA256 hashes are recorded in the companion JSON file.

These artifacts document an **offline research closure**, not a production experiment status transition.
