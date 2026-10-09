"""Replay durable ME clean OOS observations into dedicated research storage."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.shadow.me_r_close_location_capture_spool import (
    MECleanCaptureSpool,
    ME_NAME,
    CLEAN_VERSION,
)
from app.shadow.me_r_long_close_location_oos_runner import (
    MERLongCLoOosAck,
    observe_oos_candidate,
)


def replay_me_clean_capture(
    spool: MECleanCaptureSpool,
    repo,
    limit: int = 100,
) -> dict[str, int]:
    """Retry previously captured research-only candidates."""

    results = {
        "processed": 0,
        "delivered": 0,
        "pending": 0,
        "quarantined": 0,
    }

    for key, payload, _attempts in spool.pending(limit=limit):
        results["processed"] += 1

        try:
            if (
                payload["scanner_name"] != ME_NAME
                or payload["scanner_version"] != CLEAN_VERSION
            ):
                ack = MERLongCLoOosAck.SKIPPED_VERSION
            else:
                candidate = SimpleNamespace(
                    scanner_name=payload["scanner_name"],
                    scanner_version=payload["scanner_version"],
                    symbol=payload["symbol"],
                    direction=payload["direction"],
                    detected_at=datetime.fromisoformat(
                        payload["detected_at"]
                    ),
                    features=payload["features"],
                )
                ack = observe_oos_candidate(repo, candidate)

        except Exception:
            ack = MERLongCLoOosAck.ERROR

        spool.acknowledge(key, ack)

        if ack in (
            MERLongCLoOosAck.INSERTED,
            MERLongCLoOosAck.DUPLICATE,
        ):
            results["delivered"] += 1
        elif ack in (
            MERLongCLoOosAck.INVALID_SOURCE,
            MERLongCLoOosAck.SKIPPED_VERSION,
        ):
            results["quarantined"] += 1
        else:
            results["pending"] += 1

    return results
