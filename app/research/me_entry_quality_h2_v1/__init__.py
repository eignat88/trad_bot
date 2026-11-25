"""Issue #298 / H2_RSI_ONLY — isolated causal RSI-confirmation research.

``me_entry_quality_h2_v1`` is a SELF-CONTAINED, PURE research package that
tests one hypothesis against the independent #305 source cohort
(``research_me_upstream_coverage.source_event``):

    Does ME Inverse SHORT V3 improve if the SHORT is opened not immediately
    after the original ``RSI >= 65`` detector signal, but only after a
    CONFIRMED weakening of RSI on the subsequently CLOSED 5m candles?

Variants (frozen-candidates, all preliminary):

* ``H2_RSI_TURN_1``  — first strictly decreasing closed post-signal RSI;
* ``H2_RSI_TURN_2``  — two consecutive strictly decreasing closed
  post-signal RSI observations;
* ``H2_RSI_PEAK_REVERSAL`` — causal strict-local-peak recognition with an
  explicit one-candle lag and confirmed reversal (or ``NOT_READY`` when no
  peak rule is prespecified).

Hard guarantees:

* RESEARCH_ONLY — no trading, no scoring, no gates, no paper/live, no
  systemd, no SQL migrations, no service interaction of any kind;
* PURE — no database, network, filesystem or clock access; every input is
  passed in by the caller;
* CAUSAL — only candles fully closed at ``evaluation_asof_ms`` are used,
  post-signal observations close STRICTLY AFTER the original ``signal_time``
  (microsecond precision preserved), and appended future data never changes
  an earlier-ASOF result;
* IMMUTABLE LINEAGE — ``source_event_key`` / ``source_identity`` /
  ``signal_time`` / ``activation_freeze_ts`` / ``lineage_hash`` and the
  original feature snapshot are carried VERBATIM (identity logic is REUSED
  unchanged from H1-C); the recomputed closed-candle RSI never replaces the
  original ``rsi_14``;
* NO FICTIONAL EXECUTION — a confirmed delayed entry records the earliest
  causally executable instant but keeps the entry price explicitly
  UNDETERMINED; the original source price is never an execution price.

DEPLOY HOLD (unchanged): no prospective accumulation or deployment until
#305 demonstrates source coverage, a bounded candle source is approved,
and the outcome policy and parameters are frozen separately.
"""
from __future__ import annotations

H2_PACKAGE_VERSION = "H2_RSI_ONLY_1.0.0"
