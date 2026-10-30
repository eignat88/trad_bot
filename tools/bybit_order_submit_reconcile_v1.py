"""BYBIT_ORDER_SUBMIT_RECONCILE_V1 — explicit reconciliation for UNKNOWN
order submissions (code reviews #5709376849 / #5709889450).

This tool NEVER sends orders. It manages the persistent order-submit journal
of the fail-closed double-submit guard (``app/exchange/retry_budget.py``).

Usage:
    python tools/bybit_order_submit_reconcile_v1.py --list
    python tools/bybit_order_submit_reconcile_v1.py --reconcile
    python tools/bybit_order_submit_reconcile_v1.py \\
        --manual-resolution --order-link-id <orderLinkId> --symbol BTCUSDT \\
        --operator-id <id> --reference <confirmation-ref> \\
        --status-check "order filled in Bybit UI at 2026-10-30 12:00 UTC" \\
        --reason "exchange history purged; verified manually"

* ``--list`` (offline): print every blocking journal entry;
* ``--reconcile``: read-only exchange queries with STRICT identity
  verification (exact ``orderLinkId`` + symbol + category + accepted order
  status). A verified confirmation lifts the block WITH a verifiable
  confirmation record. EVERYTHING else (no record, foreign rows, empty or
  different ``orderLinkId``, unknown status, malformed response, API error,
  timeout, order not found) is an explicit UNRESOLVED BLOCKER: exit code 2,
  the submission stays fail-closed. A timer may NEVER substitute for
  order-state verification;
* ``--manual-resolution``: manual operator resolution is
  ``MANUAL_RESOLUTION_NOT_IMPLEMENTED`` (exit code 3) — review #5709889450
  requires verified operator authorization plus a verifiable confirmation
  record (operator identity, confirmation reference, orderLinkId, symbol,
  status-check result, UTC timestamp, reason) and neither exists yet. The
  block is deliberately KEPT instead of trusting free-form strings; no
  fake authorization system is introduced.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root


def _load_settings(config: str):
    from app.config import Settings, load_settings

    path = Path(config)
    if path.exists():
        return load_settings(path)
    return Settings()


def _guard(settings, journal: str | None):
    from app.exchange import retry_budget

    guard = retry_budget.get_order_guard()
    guard.configure(
        journal
        or retry_budget.str_setting(
            settings, "bybit_order_submit_journal_path", "")
        or retry_budget.ORDER_JOURNAL_DEFAULT_PATH)
    return guard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--journal", default=None,
                        help="override the order-submit journal path")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true",
                       help="list blocking journal entries (offline)")
    group.add_argument("--reconcile", action="store_true",
                       help="query the exchange (read-only, strict identity) "
                            "and resolve verified submissions")
    group.add_argument("--manual-resolution", action="store_true",
                       help="attempt a manual operator resolution — "
                            "NOT IMPLEMENTED, the block is kept")
    parser.add_argument("--operator-id", default="",
                        help="manual resolution requisite: operator identity")
    parser.add_argument("--reference", default="",
                        help="manual resolution requisite: confirmation "
                             "reference")
    parser.add_argument("--order-link-id", default="",
                        help="manual resolution requisite: orderLinkId")
    parser.add_argument("--symbol", default="",
                        help="manual resolution requisite: symbol")
    parser.add_argument("--status-check", default="",
                        help="manual resolution requisite: order status "
                             "check result")
    parser.add_argument("--reason", default="",
                        help="manual resolution requisite: reason for the "
                             "manual decision")
    args = parser.parse_args(argv)

    settings = _load_settings(args.config)
    guard = _guard(settings, args.journal)

    if args.list:
        print(json.dumps(guard.active_unknown(), indent=1))
        return 0

    if args.reconcile:
        from app.exchange.bybit_client import BybitClient

        results = BybitClient(settings).reconcile_order_submissions()
        print(json.dumps(results, indent=1))
        unresolved = [r for r in results if r.get("resolution") != "resolved"]
        if unresolved:
            print(f"UNRESOLVED BLOCKERS: {len(unresolved)} — do NOT resubmit; "
                  "manual resolution is MANUAL_RESOLUTION_NOT_IMPLEMENTED",
                  file=sys.stderr)
            return 2
        return 0

    # ------------------------------------------------- manual resolution
    # Review #5709889450 (P2-B): manual unlock requires verified operator
    # authorization AND a verifiable confirmation record. Without a real
    # authorization mechanism this stays fail-closed: the journal is NEVER
    # mutated here and the UNKNOWN block is kept.
    record = {
        "attempted_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds"),
        "operator_id": args.operator_id,
        "reference": args.reference,
        "order_link_id": args.order_link_id,
        "symbol": args.symbol,
        "status_check": args.status_check,
        "reason": args.reason,
    }
    missing = [name for name in ("operator_id", "reference", "order_link_id",
                                 "symbol", "status_check", "reason")
               if not record.get(name)]
    print(json.dumps({"manual_resolution_record": record,
                      "missing_requisites": missing}, indent=1))
    print("MANUAL_RESOLUTION_NOT_IMPLEMENTED — manual unlock of an UNKNOWN "
          "order submission requires a verified operator authorization "
          "mechanism, which is deliberately not implemented; the block is "
          "kept (fail-closed). Do NOT resubmit.", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
