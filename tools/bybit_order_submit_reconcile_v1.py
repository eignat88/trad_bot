"""BYBIT_ORDER_SUBMIT_RECONCILE_V1 — explicit reconciliation for UNKNOWN
order submissions (code review #5709376849, P1).

This tool NEVER sends orders. It manages the persistent order-submit journal
of the fail-closed double-submit guard (``app/exchange/retry_budget.py``).

Usage:
    python tools/bybit_order_submit_reconcile_v1.py --list
    python tools/bybit_order_submit_reconcile_v1.py --reconcile
    python tools/bybit_order_submit_reconcile_v1.py \\
        --mark-resolved <orderLinkId> \\
        --confirm-manual "checked in Bybit UI: order filled 2026-10-30 12:00 UTC"

* ``--list`` (offline): print every blocking journal entry;
* ``--reconcile``: read-only queries of the exchange
  (``GET /v5/order/realtime`` + ``GET /v5/order/history``) by the recorded
  ``orderLinkId``. A definitive exchange record (open / filled / cancelled /
  rejected) lifts the block WITH evidence. NO record is an explicit
  UNRESOLVED BLOCKER: exit code 2, the submission stays fail-closed and
  requires operator confirmation — a timer may NEVER substitute for
  order-state verification;
* ``--mark-resolved``: documented operator override; REQUIRES
  ``--confirm-manual`` with the manual confirmation evidence, which is
  recorded in the journal.
"""
from __future__ import annotations

import argparse
import json
import sys
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
                       help="query the exchange (read-only) and resolve "
                            "confirmed submissions")
    group.add_argument("--mark-resolved", metavar="ORDER_LINK_ID",
                       help="operator override: lift the block with evidence")
    parser.add_argument("--confirm-manual", metavar="EVIDENCE",
                        help="required with --mark-resolved: the manual "
                             "confirmation evidence")
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
                  "operator confirmation required", file=sys.stderr)
            return 2
        return 0

    if not args.confirm_manual:
        parser.error("--mark-resolved requires --confirm-manual evidence")
    ok = guard.resolve_reconciled(
        args.mark_resolved,
        evidence=f"manual operator resolution: {args.confirm_manual}")
    print(f"mark-resolved {args.mark_resolved}: "
          f"{'done' if ok else 'no active entry'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
