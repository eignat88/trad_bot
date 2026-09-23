-- 03_r_distribution.sql
-- R-distribution buckets for V1 and V2
-- NOTE: All ROUND() calls use ::numeric cast to avoid
--       PostgreSQL "function round(double precision, integer) does not exist"

-- V1 R distribution
SELECT
    'V1' AS version,
    CASE
        WHEN pnl_r <= -1.0 THEN '<= -1R'
        WHEN pnl_r > -1.0 AND pnl_r < 0 THEN '(-1R, 0)'
        WHEN pnl_r >= 0 AND pnl_r < 0.5 THEN '[0, +0.5R)'
        WHEN pnl_r >= 0.5 AND pnl_r < 1.0 THEN '[+0.5R, +1R)'
        WHEN pnl_r >= 1.0 THEN '>= +1R'
    END AS r_bucket,
    COUNT(*) AS count,
    ROUND((COUNT(*)::numeric / (SELECT COUNT(*) FROM dds.paper_trade WHERE scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1' AND status = 'CLOSED') * 100)::numeric, 1) AS pct
FROM dds.paper_trade pt
WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
  AND pt.direction = 'LONG'
  AND pt.status = 'CLOSED'
GROUP BY r_bucket
ORDER BY r_bucket

UNION ALL

-- V2 R distribution
SELECT
    'V2' AS version,
    CASE
        WHEN pnl_r <= -1.0 THEN '<= -1R'
        WHEN pnl_r > -1.0 AND pnl_r < 0 THEN '(-1R, 0)'
        WHEN pnl_r >= 0 AND pnl_r < 0.5 THEN '[0, +0.5R)'
        WHEN pnl_r >= 0.5 AND pnl_r < 1.0 THEN '[+0.5R, +1R)'
        WHEN pnl_r >= 1.0 THEN '>= +1R'
    END AS r_bucket,
    COUNT(*) AS count,
    ROUND((COUNT(*)::numeric / (SELECT COUNT(*) FROM dds.paper_trade WHERE scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2' AND status = 'CLOSED') * 100)::numeric, 1) AS pct
FROM dds.paper_trade pt
WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
  AND pt.direction = 'LONG'
  AND pt.status = 'CLOSED'
GROUP BY r_bucket
ORDER BY r_bucket;
