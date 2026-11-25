"""Issue #298 / H2 — Wilder RSI(14) math and causal RSI-confirmation rules.

Research-only, pure: no I/O, no network, no database, no clock reads.
All functions are deterministic pure functions of their inputs.

RSI math (parity contract)
--------------------------
:func:`rsi_wilder_last` implements EXACTLY the same Wilder RSI semantics
as the original detector's ``app.indicators.technical.rsi_wilder``:

* initial average = SMA of the first ``period`` close-to-close changes;
* recursive Wilder smoothing ``(avg * (period - 1) + x) / period``;
* ``avg_loss == 0`` -> ``100.0`` when ``avg_gain > 0`` else ``50.0``;
* at least ``period + 1`` closes are required.

The production module is NEVER imported into this runtime path (package
purity); parity against ``app.indicators.technical.rsi_wilder`` is enforced
offline by ``tests/test_me_entry_quality_h2_rsi_parity_v1.py`` on identical
close sequences, and any mismatch must fail closed.

:func:`rsi_wilder_series` computes the RSI of every prefix
``closes[:i + 1]`` (``i >= period``) with the identical operation order, so
``series[i] == rsi_wilder_last(closes[:i + 1])`` holds bit-for-bit.

Causal confirmation rules
-------------------------
The confirmation functions consume ONLY a sequence of already-CLOSED RSI
observations ``obs_rsi = [RSI(O_0), RSI(O_1), ...]`` where

* ``O_0`` — the last 5m candle that CLOSED at or before the original
  signal instant (level reference only — pre-signal declines can never be
  counted as confirmations because confirmations are selected at indices
  ``k >= 1``);
* ``O_1, O_2, ...`` — the 5m candles closing STRICTLY AFTER the original
  signal instant, in grid order.

Every rule is evaluated left-to-right on closed observations only: no
future-centered pivots, no repainting.  Appending additional observations
NEVER changes which ``k`` is returned for an earlier prefix.
"""
from __future__ import annotations

from collections.abc import Sequence

#: Wilder RSI period used by the original detector (``rsi_period = 14``).
RSI_PERIOD = 14

#: Minimum number of CLOSED closes required for one defined RSI value.
RSI_SEED_MIN_CLOSES = RSI_PERIOD + 1

#: Frozen-candidate peak rule for ``H2_RSI_PEAK_REVERSAL`` (NOT an approved
#: frozen policy — see docs/research/298_h2_rsi_only_v1_h2_implementation_v1.md).
#:
#: Definition (parameter-free, causal, lag-1 recognition):
#:
#: * ``O_j`` is a STRICT local peak iff ``RSI(O_j) > RSI(O_{j-1})`` and
#:   ``RSI(O_j) > RSI(O_{j+1})`` on CLOSED candles;
#: * the peak is recognized only at the CLOSE of ``O_{j+1}`` (explicit
#:   recognition lag 1 — the right neighbour must be closed; the peak is
#:   never re-evaluated or repainted afterwards);
#: * the reversal is confirmed at the close of ``O_{j+1}`` itself: the
#:   first strictly declining closed observation after ``O_1`` whose
#:   predecessor is a recognized strict local peak.
PEAK_RULE_V1 = "CAUSAL_STRICT_LOCAL_MAX_LAG1_CONFIRM_FIRST_DECLINE_V1"

#: The complete set of prespecified peak rules.  An unknown rule is never
#: silently accepted — the caller must mark the variant ``NOT_READY``.
PEAK_RULES: tuple[str, ...] = (PEAK_RULE_V1,)


def rsi_wilder_last(closes: Sequence[float], period: int = RSI_PERIOD) -> float:
    """RSI with Wilder's initial SMA and recursive smoothing.

    Bit-for-bit parity with ``app.indicators.technical.rsi_wilder``: same
    validation, same operation order, same degenerate-case handling.
    """
    if period <= 0 or len(closes) <= period:
        raise ValueError("RSI requires period + 1 closes")
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_wilder_series(
    closes: Sequence[float], period: int = RSI_PERIOD
) -> tuple[float | None, ...]:
    """RSI of every prefix ``closes[:i + 1]``, aligned with ``closes``.

    ``series[i]`` is the Wilder RSI(``period``) of closes ``0..i`` and is
    ``None`` while ``i < period`` (warmup — not enough closes yet).  The
    computation applies the same recursion with the same operation order as
    :func:`rsi_wilder_last`, so ``series[i] == rsi_wilder_last(closes[:i+1])``
    exactly (verified by the parity tests).

    FAIL-CLOSED: ``period <= 0`` raises ``ValueError``; a series without any
    defined value returns all-``None`` rather than a fabricated number.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) <= period:
        return tuple(None for _ in closes)

    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _value(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0:
            return 100.0 if avg_gain else 50.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    out: list[float | None] = [None] * period
    out.append(_value(avg_gain, avg_loss))  # RSI at index `period` (seed only)
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out.append(_value(avg_gain, avg_loss))
    return tuple(out)


# ─── causal confirmation rules ───────────────────────────────────────────────
#
# ``obs_rsi[k]`` is the recomputed RSI of the k-th CLOSED 5m candle closing
# strictly after the original signal (``obs_rsi[0]`` is the level reference
# ``O_0``).  Every function returns the 1-based index ``k`` of the first
# CONFIRMING observation, or ``None`` when no confirmation exists in the
# given prefix.  Pure functions of ``obs_rsi`` — appending more observations
# never changes the earlier answer.


def turn_1_confirmation(obs_rsi: Sequence[float]) -> int | None:
    """``H2_RSI_TURN_1`` — first strictly decreasing post-signal RSI.

    Confirms at the first ``k >= 1`` with ``RSI(O_k) < RSI(O_{k-1})``
    (both observations closed AFTER the original signal for ``O_k``; the
    comparison base is the immediately preceding closed candle).
    """
    for k in range(1, len(obs_rsi)):
        if obs_rsi[k] < obs_rsi[k - 1]:
            return k
    return None


def turn_2_confirmation(obs_rsi: Sequence[float]) -> int | None:
    """``H2_RSI_TURN_2`` — two consecutive strictly decreasing observations.

    Confirms at the first ``k >= 2`` with ``RSI(O_k) < RSI(O_{k-1})`` AND
    ``RSI(O_{k-1}) < RSI(O_{k-2})``.  Both declines are observed on
    post-signal closed candles (``k-1 >= 1``) — a decline BEFORE the
    original signal can never be part of the confirmation.
    """
    for k in range(2, len(obs_rsi)):
        if obs_rsi[k] < obs_rsi[k - 1] < obs_rsi[k - 2]:
            return k
    return None


def peak_reversal_confirmation(
    obs_rsi: Sequence[float], *, peak_rule: str | None = PEAK_RULE_V1
) -> int | None:
    """``H2_RSI_PEAK_REVERSAL`` — causal strict-local-peak + first decline.

    With the prespecified :data:`PEAK_RULE_V1`: confirms at the first
    ``k >= 2`` such that ``O_{k-1}`` is a STRICT local peak
    (``RSI(O_{k-1}) > RSI(O_{k-2})`` and ``RSI(O_{k-1}) > RSI(O_k)``) and
    ``O_k`` strictly declines (``RSI(O_k) < RSI(O_{k-1})``).

    Everything needed for the decision is closed at the close of ``O_k``
    (``O_{k-2}``, ``O_{k-1}``, ``O_k``) — recognition lag is exactly one
    candle after the peak and there is NO future-centered pivot and NO
    repainting: the returned ``k`` never changes when later observations
    arrive.

    ``peak_rule=None`` or an unknown rule raises ``ValueError`` — an
    undefined peak rule must be surfaced as ``NOT_READY`` by the caller,
    never silently approximated.
    """
    if peak_rule is None:
        raise ValueError("peak rule is not prespecified (NOT_READY)")
    if peak_rule != PEAK_RULE_V1:
        raise ValueError(f"unknown peak rule {peak_rule!r}")
    for k in range(2, len(obs_rsi)):
        if (
            obs_rsi[k] < obs_rsi[k - 1]
            and obs_rsi[k - 1] > obs_rsi[k - 2]
        ):
            return k
    return None
