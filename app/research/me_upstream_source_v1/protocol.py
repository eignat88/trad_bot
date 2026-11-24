"""Issue #305 — ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1 protocol.

Frozen protocol identity of the independent detector-level upstream source
capture.  Research only: this module never touches trading, scoring, gates
or Bybit API calls.

Independence contract
---------------------
* the capture identity and every event key are derived ONLY from the
  normalized original signal identity — never from the V3 / OOS-2 protocol
  version or protocol fingerprint (those are CONTOUR properties and are
  stored as capture-side metadata, excluded from identity and from lineage
  comparison);
* ``protocol_fingerprint()`` is the fingerprint of THIS capture protocol
  (frozen descriptor below) — it identifies the capture code contract, not
  the V3 contour.
"""
from __future__ import annotations

import hashlib
import json

#: Administrative experiment / capture id.
EXPERIMENT_ID = "ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1"

#: Capture protocol version (this capture only — never part of event identity).
PROTOCOL_VERSION = "1.0.0"

#: Namespace prefix of the deterministic upstream event key.  Guarantees the
#: upstream key can never collide with the V3 / OOS-2 event keys even for the
#: SAME original signal (their payloads embed their own strategy ids).
IDENTITY_NAMESPACE = "ME_UPSTREAM_SOURCE_V1"

#: The one detector whose events are captured (the ME INVERSE SHORT V3
#: upstream source).  Everything else is ignored (INVALID_SCANNER).
SOURCE_SCANNER = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
SOURCE_DIRECTION = "LONG"

#: Capture route: detector-level publication, IMMEDIATELY after
#: ``scanner.scan(ctx)`` — before scoring, dedup, risk geometry, direction
#: gate, regime filter and expectancy filter.
ROUTE_PRE_SCORING_SOURCE = "PRE_SCORING_SOURCE"
CAPTURE_ROUTES = frozenset({ROUTE_PRE_SCORING_SOURCE})

#: Runtime feature flag — FIRST of the two activation gates.  Capture is
#: OFF by default: with the flag unset the hook performs no DB I/O at all.
ACTIVATION_FLAG = "ME_UPSTREAM_SOURCE_CAPTURE_V1_ENABLED"

# ---------------------------------------------------------------------------
# Frozen protocol descriptor.  Changing ANY line below changes
# ``protocol_fingerprint()`` and therefore requires a new activation — the
# stored fingerprint in the activation row must always match the running
#: code byte-for-byte (mirrors the V3 activation contract).
# ---------------------------------------------------------------------------
PROTOCOL_DESCRIPTOR = (
    "ME_UPSTREAM_SOURCE_V1|1.0.0|"
    "source=MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|direction=LONG|"
    "route=PRE_SCORING_SOURCE|"
    "identity=scanner|symbol|direction|entry_timeframe|signal_candle_open_ms|"
    "prospective_only=signal_time>freeze_ts|no_backfill"
)


def protocol_fingerprint() -> str:
    """Deterministic SHA-256 of the frozen protocol descriptor above."""
    payload = json.dumps(
        {"descriptor": PROTOCOL_DESCRIPTOR},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
