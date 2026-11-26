"""Compare head vs base full-suite failure sets (Issue #312 attribution).

Parses the two pytest text logs from the full regression runs and reports
whether any failing/errored test exists at HEAD that does NOT fail
identically at BASE (i.e. potentially introduced by this change).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEAD = ROOT / "reports" / "issue312_full_run_head.txt"
BASE = ROOT / "reports" / "issue312_full_run_base.txt"

LINE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")


def failures(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = LINE.match(raw.strip())
        if match:
            out[match.group(2)] = match.group(1)
    return out


def main() -> int:
    head = failures(HEAD)
    base = failures(BASE)

    print(f"head failing/errored modules: {len(head)}")
    print(f"base failing/errored modules: {len(base)}")

    new_at_head = sorted(set(head) - set(base))
    gone_at_head = sorted(set(base) - set(head))

    print(f"\nfailures present at head but NOT at base: {len(new_at_head)}")
    for node in new_at_head:
        print("  NEW   ", node)

    print(f"\nfailures present at base but NOT at head: {len(gone_at_head)}")
    for node in gone_at_head:
        print("  GONE  ", node)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
