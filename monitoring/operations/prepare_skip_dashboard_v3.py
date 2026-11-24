#!/usr/bin/env python3
"""Prepare an offline Operations V3 dashboard candidate; never mutate source.

The deployed SHA is pinned to the 2026-11-23 verified VPS JSON. The original
file is only read; output must not exist. Panels 12, 16, 18, 19 only.
"""
import argparse
import hashlib
import json
from pathlib import Path

SOURCE_SHA256 = "a95f84ce8fa539610bbd765c747891bee2bee6ab79ec8b97c7ca6e6ddb819f1e"
DASHBOARD_UID = "trad-bot-operations-v1"
PANEL_IDS = {12, 16, 18, 19}
OLD = "event_kind='CALL'"
NEW = "event_kind='CALL' AND result IS DISTINCT FROM 'BYBIT_API_SKIP'"


def patch_dashboard(data):
    dashboard = data.get("dashboard", data)
    if dashboard.get("uid") != DASHBOARD_UID or len(dashboard.get("panels", [])) != 21:
        raise ValueError("Unexpected dashboard UID or panel count")
    updated = set()
    for panel in dashboard["panels"]:
        if panel.get("id") not in PANEL_IDS:
            continue
        pid = panel["id"]
        if pid in updated:
            raise ValueError("Duplicate panel id")
        targets = panel.get("targets", [])
        matches = [t for t in targets if OLD in t.get("rawSql", "")]
        if len(matches) != 1:
            raise ValueError(f"Panel {pid} must have exactly one CALL query")
        target = matches[0]
        target["rawSql"] = target["rawSql"].replace(OLD, NEW)
        updated.add(pid)
    if updated != PANEL_IDS:
        raise ValueError(f"Panels missing: {sorted(PANEL_IDS - updated)}")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    src = args.source.resolve()
    out = args.output.resolve()
    if src == out or out.exists():
        parser.error("Output must be a new, separate file")
    raw = src.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        parser.error("Source SHA256 differs from verified Operations V3 baseline")
    data = json.loads(raw.decode("utf-8"))
    patch_dashboard(data)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print("DASHBOARD_CANDIDATE_CREATED", out)
    print("PANEL_IDS_CHANGED", ",".join(str(i) for i in sorted(PANEL_IDS)))
    print("CANDIDATE_SHA256", hashlib.sha256(out.read_bytes()).hexdigest())
    print("PRODUCTION_SOURCE_UNCHANGED", src)


if __name__ == "__main__":
    main()
