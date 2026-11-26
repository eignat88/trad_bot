"""Extract clean failing/errored TEST IDs from pytest text logs (issue #312 §3).

``issue312_compare_failures.py`` diffs raw ``^(FAILED|ERROR)`` lines, which can
false-match logging records like ``ERROR    app.x:y.py:1``.  This extractor
keeps only real pytest node ids (starting with ``tests/``) and writes a clean
list per side, so the review report can cite concrete failing test IDs.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")


def read_log(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # PowerShell '>' writes UTF-16
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def failures(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in read_log(path).splitlines():
        match = LINE.match(raw.strip())
        if not match:
            continue
        node = match.group(2)
        if not node.startswith("tests/"):  # log-record artifact, not a test
            continue
        out[node] = match.group(1)
    return out


def main() -> int:
    side = sys.argv[1] if len(sys.argv) > 1 else "head"
    log = ROOT / "reports" / f"issue312_full_run_{side}.txt"
    rows = failures(log)
    out = ROOT / "reports" / f"issue312_full_run_failures_{side}.txt"
    with out.open("w", encoding="utf-8") as fh:
        for node in sorted(rows):
            fh.write(f"{rows[node]}\t{node}\n")
    print(f"{side}: {len(rows)} failing/errored test ids -> {out.name}")
    for node in sorted(rows):
        print(f"  {rows[node]:6s} {node}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
