"""Best-effort: update PR #313's body from reports/issue312_pr_body.md.

Falls back to printing the body for manual paste when the token lacks
pulls:write (the same 403 seen at PR creation).
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "eignat88/trad_bot"
PR_NUMBER = 313
BODY_FILE = Path(__file__).resolve().parent / "issue312_pr_body.md"


def github_token() -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise RuntimeError("no token in git credential store")


def main() -> int:
    body = BODY_FILE.read_text(encoding="utf-8")
    token = github_token()
    payload = json.dumps({"body": body}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}",
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "issue312-update-pr",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("updated PR body:", resp.status)
            return 0
    except urllib.error.HTTPError as exc:
        print("HTTP", exc.code, exc.read().decode("utf-8", "replace")[:500])
        print("fallback: paste reports/issue312_pr_body.md into PR #313 manually")
        return 1


if __name__ == "__main__":
    sys.exit(main())
