#!/usr/bin/env python3
"""Issue #304: offline, read-only analysis of saved journald text.

Input consists of files previously exported with:
    journalctl -u SERVICE --since ... --until ... -o short-iso --no-pager > FILE

No VPS access, network calls, database reads, subprocesses, or runtime writes.
Event timestamps are log-completion timestamps, NOT HTTP send/grant times.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Iterable

_FIELDS = re.compile(r"(?<!\S)([A-Za-z_][A-Za-z_0-9]*)=([^\s]+)")
_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_CYCLE = re.compile(r"cycle #(?P<cycle>\d+) incomplete:.*?completion_status=(?P<status>\w+).*?expected=(?P<expected>\d+) processed=(?P<processed>\d+).*?skipped=(?P<skipped>\d+)")
_SENT_CONFIRMED = {"OK", "RATE_LIMIT"}
_PRE_SEND = {"BYBIT_API_SKIP"}


def parse_line(service: str, line: str) -> dict | None:
    """Parse one log event; never store the whole line (potential secrets)."""
    match = _TIME.match(line)
    if not match:
        return None
    timestamp = match.group(1)
    if "BYBIT_API_SKIP" in line:
        payload = line.split("BYBIT_API_SKIP", 1)[1]
        fields = dict(_FIELDS.findall(payload))
        return {
            "kind": "api",
            "service": service,
            "second": timestamp,
            "minute": timestamp[:16],
            "result": "BYBIT_API_SKIP",
            "endpoint": fields.get("endpoint", "?"),
            "symbol": fields.get("symbol", "?"),
            "interval": fields.get("interval", "?"),
            "limit": fields.get("limit", "?"),
            "attempt": fields.get("attempt_index", "?"),
            "request_id": fields.get("logical_request_id", "?"),
        }
    if "BYBIT_API_CALL" in line:
        payload = line.split("BYBIT_API_CALL", 1)[1]
        fields = dict(_FIELDS.findall(payload))
        result = fields.get("result")
        if not result:
            return None
        return {
            "kind": "api", "service": service, "second": timestamp,
            "minute": timestamp[:16], "result": result,
            "endpoint": fields.get("endpoint", "?"),
            "symbol": fields.get("symbol", "?"),
            "interval": fields.get("interval", "?"),
            "limit": fields.get("limit", "?"),
            "attempt": fields.get("attempt_index", "?"),
            "request_id": fields.get("logical_request_id", "?"),
        }
    if service == "scanner" and "cycle #" in line and "incomplete:" in line:
        c = _CYCLE.search(line)
        if c:
            return {"kind": "cycle", "second": timestamp, "service": service,
                    "cycle": int(c["cycle"]), "status": c["status"],
                    "expected": int(c["expected"]),
                    "processed": int(c["processed"]),
                    "skipped": int(c["skipped"])}
    return None


def analyze(inputs: dict[str, Iterable[str]]) -> dict:
    """Use explicit result classes: unknown outcomes are NOT 'sent'."""
    endpoints: dict[str, Counter] = defaultdict(Counter)
    results: dict[str, Counter] = defaultdict(Counter)
    seconds: dict[str, Counter] = defaultdict(Counter)
    shapes: dict[tuple[str, str, str, str, str], Counter] = defaultdict(Counter)
    attempts: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    cycles: list[dict] = []

    for service, lines in inputs.items():
        for line in lines:
            e = parse_line(service, line)
            if e is None:
                continue
            if e["kind"] == "cycle":
                cycles.append(e)
                continue
            result = e["result"]
            results[service][result] += 1
            endpoints[service][(e["endpoint"], result)] += 1
            seconds[e["second"]][service] += 1
            seconds[e["second"]][result] += 1
            if e["request_id"] != "?":
                attempts[service][e["request_id"]].add(e["attempt"])
            if result == "OK" and service in {"v3", "oos2"}:
                # This is a *shape overlap*, NOT an identical HTTP query.
                minute = datetime.fromisoformat(e["second"]).minute
                bucket = e["second"][:14] + f"{minute // 5 * 5:02d}"
                key = (bucket, e["endpoint"], e["symbol"],
                       e["interval"], e["limit"])
                shapes[key][service] += 1

    all_results = sum((c for c in results.values()), Counter())
    overlaps = Counter()
    for (_bucket, endpoint, _sym, _interval, _limit), c in shapes.items():
        overlaps[endpoint] += min(c["v3"], c["oos2"])
    peaks = sorted(
        (
            {"second": second,
             "total_events": sum(s.get(service, 0)
                                 for service in inputs),
             "by_service": {service: s.get(service, 0) for service in inputs},
             "rate_limits": s.get("RATE_LIMIT", 0),
             "pre_send_skips": s.get("BYBIT_API_SKIP", 0)}
            for second, s in seconds.items()
        ),
        key=lambda row: (-row["total_events"], row["second"]),
    )[:20]
    return {
        "methodology": (
            "Log completion timestamps; sent_confirmed counts only OK and "
            "RATE_LIMIT. Other outcomes may have uncertain send status; "
            "overlap compares logged request shapes, not complete query "
            "parameters or response identity."
        ),
        "by_service": {
            service: {
                "result_counts": dict(sorted(results[service].items())),
                "sent_confirmed": sum(results[service][r]
                                      for r in _SENT_CONFIRMED),
                "pre_send_skip": sum(results[service][r] for r in _PRE_SEND),
                "endpoint_result_counts": [
                    {"endpoint": ep, "result": outcome, "count": count}
                    for (ep, outcome), count in sorted(endpoints[service].items())
                ],
                "multi_attempt_logical_ids": sum(
                    len(kinds) > 1 for kinds in attempts[service].values()
                ),
            }
            for service in inputs
        },
        "overall_result_counts": dict(sorted(all_results.items())),
        "top_completion_second_peaks": peaks,
        "five_minute_request_shape_overlap": dict(sorted(overlaps.items())),
        "scanner_incomplete_cycles": sorted(
            cycles, key=lambda row: (row["second"], row["cycle"])
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--journal", action="append", required=True, metavar="SERVICE=FILE",
        help="service is scanner, v3, or oos2; input must be saved short-iso journal",
    )
    args = parser.parse_args()
    paths: dict[str, Path] = {}
    for item in args.journal:
        if "=" not in item:
            parser.error("expected SERVICE=FILE")
        name, filename = item.split("=", 1)
        if name not in {"scanner", "v3", "oos2"} or name in paths:
            parser.error("services must be unique scanner/v3/oos2")
        paths[name] = Path(filename)
    # Files are read only; no writes even to stdout targets.
    with_files = {name: path.read_text(encoding="utf-8").splitlines()
                  for name, path in paths.items()}
    print(json.dumps(analyze(with_files), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
