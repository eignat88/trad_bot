"""Standalone opt-in runner; never starts without explicit approved config."""
from __future__ import annotations

import argparse

from .pipeline import MeEarlyMaeV3Pipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        "ME_R_EARLY_MAE_V3_CAPTURE_DISABLED: deployment, migration, freeze "
        "and activation remain separately unapproved"
    )


if __name__ == "__main__":
    main()
