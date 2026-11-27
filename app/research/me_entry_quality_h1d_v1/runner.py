"""Issue #314 / H1-D — read-only CLI runner (research-only, NO_DEPLOY).

Two modes:

``--replay <snapshot>``  (DEFAULT — offline)
    Load an immutable provenance snapshot, reproduce the classification
    BYTE-IDENTICALLY and write canonical JSON output.  This mode performs
    ZERO HTTP and never even constructs a client.

``--fetch``
    Bounded research fetch of the canonical 4H (``interval=240``) and 1D
    (native ``interval=D``) windows for ONE #305 source event through
    ``BybitClient._public_get()`` (shared limiter / retry budget / breaker),
    then snapshot + classify.  **REFUSES TO RUN** unless BOTH

    1. the explicit authorization flag ``--fetch-authorized`` is passed, AND
    2. an owner-approval SENTINEL file exists (``--sentinel``, default
       ``./H1D_REAL_FETCH_APPROVED.sentinel``);

    a real Bybit fetch requires SEPARATE OWNER APPROVAL AFTER REVIEW of this
    code — offline tests and the #305 COVERAGE_PASS do NOT grant it.  Even
    in fetch mode every limiter / breaker / 429 / 10006 denial FAILS CLOSED
    (explicit error, non-zero exit, NO snapshot written) and rows are never
    fabricated.

READ_ONLY_SOURCE / NO_PRODUCTION_SIDE_EFFECTS: the runner never writes to
the database, never touches services or units, never activates OOS and
never writes any file except the explicitly passed ``--out-snapshot`` /
``--out`` paths.  ``get_klines_range()`` is never called (native ``D``
lives only in this research adapter).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from app.research.me_entry_quality_h1c_v1.event_adapter import (
    EventBatch,
    adapt_source_events,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    SOURCE_KIND_BYBIT_KLINE_V5,
)
from app.research.me_entry_quality_h1d_v1.coverage import (
    TIMEFRAME_1D,
    TIMEFRAME_4H,
    canonical_window_bounds,
)
from app.research.me_entry_quality_h1d_v1.htf_fetch import (
    HtfFetchError,
    fetch_htf_history,
)
from app.research.me_entry_quality_h1d_v1.provenance import (
    FIXTURE_LABEL_REAL,
    ProvenanceError,
    canonical_output_bytes,
    build_snapshot,
    load_snapshot,
    replay_classification,
    save_snapshot,
)

H1D_RUNNER_VERSION = "H1D_RUNNER_1.0.0"

DEFAULT_SENTINEL = "H1D_REAL_FETCH_APPROVED.sentinel"

REFUSAL_MESSAGE = (
    "REFUSED: real Bybit fetch requires SEPARATE OWNER APPROVAL AFTER "
    "REVIEW. Both the explicit --fetch-authorized flag AND an owner-approval "
    "sentinel file (--sentinel, default ./{default_sentinel}) are required. "
    "Offline tests and #305 COVERAGE_PASS do NOT authorize real API calls."
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 2


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="h1d_runner",
        description=(
            "H1-D research runner: offline snapshot replay (default) or "
            "guarded bounded real fetch (owner approval required)."),
    )
    parser.add_argument(
        "--replay", metavar="SNAPSHOT", default=None,
        help="offline mode: replay this snapshot (zero HTTP, default mode)")
    parser.add_argument(
        "--fetch", action="store_true",
        help="guarded real-fetch mode (requires --fetch-authorized AND "
             "sentinel file; separate owner approval after review)")
    parser.add_argument(
        "--fetch-authorized", action="store_true",
        help="explicit authorization flag for real fetch (still requires "
             "the owner-approval sentinel file)")
    parser.add_argument(
        "--sentinel", metavar="PATH", default=DEFAULT_SENTINEL,
        help=f"owner-approval sentinel file (default: ./{DEFAULT_SENTINEL})")
    parser.add_argument(
        "--source-event", metavar="EVENT_JSON", default=None,
        help="fetch mode: JSON file with one #305 source_event row")
    parser.add_argument(
        "--out-snapshot", metavar="PATH", default=None,
        help="fetch mode: where to write the provenance snapshot")
    parser.add_argument(
        "--out", metavar="PATH", default=None,
        help="write canonical classification JSON here (default: stdout)")
    parser.add_argument(
        "--version", action="version", version=H1D_RUNNER_VERSION)
    return parser


def _emit(output_bytes: bytes, out_path: str | None) -> None:
    if out_path is None:
        sys.stdout.buffer.write(output_bytes + b"\n")
    else:
        Path(out_path).write_bytes(output_bytes)


def _default_client_factory() -> Any:
    """Construct the SHARED Bybit client (lazy import; fetch mode only)."""
    from app.config import Settings
    from app.exchange.bybit_client import BybitClient
    return BybitClient(Settings())


def _load_source_event_row(path: str) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"source event file is not valid JSON: {exc}") from exc
    if not isinstance(row, dict):
        raise ValueError("source event file must contain one JSON object")
    return row


def run_replay(snapshot_path: str, out_path: str | None) -> int:
    """Offline replay: ZERO HTTP, byte-identical output."""
    snapshot = load_snapshot(snapshot_path)
    classification = replay_classification(snapshot)
    _emit(canonical_output_bytes(classification), out_path)
    print(
        f"replay ok: event={snapshot.source_event_key} "
        f"fixture_label={snapshot.fixture_label} "
        f"eligible={classification.eligible} "
        f"reason={classification.eligibility_reason}",
        file=sys.stderr,
    )
    return EXIT_OK


def run_fetch(
    args: argparse.Namespace,
    *,
    client_factory: Callable[[], Any],
) -> int:
    """Guarded real fetch — refuses without BOTH authorizations."""
    if not args.fetch_authorized:
        print(REFUSAL_MESSAGE.format(default_sentinel=DEFAULT_SENTINEL),
              file=sys.stderr)
        return EXIT_REFUSED
    if not Path(args.sentinel).is_file():
        print(
            f"REFUSED: owner-approval sentinel file not found: "
            f"{args.sentinel}. " +
            REFUSAL_MESSAGE.format(default_sentinel=DEFAULT_SENTINEL),
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not args.source_event:
        print("--fetch requires --source-event EVENT_JSON", file=sys.stderr)
        return EXIT_FAILURE
    if not args.out_snapshot:
        print("--fetch requires --out-snapshot PATH", file=sys.stderr)
        return EXIT_FAILURE

    try:
        row = _load_source_event_row(args.source_event)
    except (OSError, ValueError) as exc:
        print(f"source event load failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    batch: EventBatch = adapt_source_events([row])
    if batch.rejections or len(batch.records) != 1:
        detail = batch.rejections[0].to_dict() if batch.rejections else "no record"
        print(f"source event rejected: {detail}", file=sys.stderr)
        return EXIT_FAILURE
    record = batch.records[0]

    try:
        client = client_factory()
        fetched: dict[str, Any] = {}
        for timeframe in (TIMEFRAME_4H, TIMEFRAME_1D):
            window_open, window_close = canonical_window_bounds(
                timeframe, record.signal_time_us)
            result = fetch_htf_history(
                client,
                symbol=record.symbol,
                timeframe=timeframe,
                start_ms=window_open,
                end_ms=window_close - 1,
            )
            if not result.rows:
                print(
                    f"fail-closed: {timeframe} fetch returned 0 rows for "
                    f"[{window_open}, {window_close - 1}] — no snapshot "
                    "written", file=sys.stderr)
                return EXIT_FAILURE
            fetched[timeframe] = result
        snapshot = build_snapshot(
            record=record,
            rows_4h=fetched[TIMEFRAME_4H].rows,
            rows_1d=fetched[TIMEFRAME_1D].rows,
            interval_4h=fetched[TIMEFRAME_4H].interval,
            interval_1d=fetched[TIMEFRAME_1D].interval,
            source_kind=SOURCE_KIND_BYBIT_KLINE_V5,
            source_label=f"bybit-kline-v5:{record.symbol}:historical-htf",
            fixture_label=FIXTURE_LABEL_REAL,
            fetched_at_ms=int(time.time() * 1000),
        )
    except (HtfFetchError, ProvenanceError, ValueError) as exc:
        # fail closed: NO snapshot, NO partial rows, NO retries at this layer
        print(f"fetch failed (fail-closed, no snapshot written): {exc}",
              file=sys.stderr)
        return EXIT_FAILURE

    save_snapshot(snapshot, args.out_snapshot)
    classification = replay_classification(snapshot)
    _emit(canonical_output_bytes(classification), args.out)
    print(
        f"fetch ok: event={record.source_event_key} "
        f"4h_rows={len(snapshot.payload_4h.rows)} "
        f"1d_rows={len(snapshot.payload_1d.rows)} "
        f"eligible={classification.eligible} "
        f"reason={classification.eligibility_reason}",
        file=sys.stderr,
    )
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], Any] | None = None,
) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    if args.fetch:
        factory = client_factory or _default_client_factory
        return run_fetch(args, client_factory=factory)
    if not args.replay:
        build_arg_parser().print_usage(sys.stderr)
        print("no mode selected: pass --replay SNAPSHOT (default, offline) "
              "or --fetch (guarded)", file=sys.stderr)
        return EXIT_FAILURE
    try:
        return run_replay(args.replay, args.out)
    except (ProvenanceError, OSError, ValueError) as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
