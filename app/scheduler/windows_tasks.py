from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

START_TASK_NAME = "BybitScanner"
STOP_TASK_NAME = "BybitScannerStop"
OUTCOME_TASK_NAME = "BybitScannerOutcomeBackfill"
STOP_TIME = "18:00"
OUTCOME_START_TIME = "09:05"
OUTCOME_INTERVAL_MINUTES = 60
WEEKDAYS_SHORT = "MON,TUE,WED,THU,FRI"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def scheduled_launcher(root: Path | None = None) -> Path:
    return (root or project_root()) / "run_scanner_task.bat"


def stop_launcher(root: Path | None = None) -> Path:
    return (root or project_root()) / "stop_scanner.bat"


def outcome_launcher(root: Path | None = None) -> Path:
    return (root or project_root()) / "run_outcome_backfill.bat"


def build_start_task_command(*, root: Path | None = None) -> list[str]:
    """Return a schtasks command that starts the scanner on computer startup."""
    launcher = scheduled_launcher(root)
    return [
        "schtasks", "/Create",
        "/TN", START_TASK_NAME,
        "/TR", f'cmd.exe /c "{launcher}"',
        "/SC", "ONSTART",
        "/RU", "SYSTEM",
        "/RL", "HIGHEST",
        "/F",
    ]


def build_stop_task_command(*, root: Path | None = None) -> list[str]:
    """Return a schtasks command that stops the scanner at 18:00 on weekdays."""
    launcher = stop_launcher(root)
    return [
        "schtasks", "/Create",
        "/TN", STOP_TASK_NAME,
        "/TR", f'cmd.exe /c "{launcher}"',
        "/SC", "WEEKLY",
        "/D", WEEKDAYS_SHORT,
        "/ST", STOP_TIME,
        "/RU", "SYSTEM",
        "/RL", "HIGHEST",
        "/F",
    ]


def build_outcome_task_command(*, root: Path | None = None) -> list[str]:
    """Return a schtasks command that backfills outcomes hourly on weekdays."""
    launcher = outcome_launcher(root)
    return [
        "schtasks", "/Create",
        "/TN", OUTCOME_TASK_NAME,
        "/TR", f'cmd.exe /c "{launcher}"',
        "/SC", "HOURLY",
        "/MO", str(OUTCOME_INTERVAL_MINUTES // 60),
        "/ST", OUTCOME_START_TIME,
        "/F",
    ]


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def install_tasks(*, root: Path | None = None) -> None:
    root = root or project_root()
    _run(build_start_task_command(root=root))
    _run(build_stop_task_command(root=root))
    _run(build_outcome_task_command(root=root))


def uninstall_tasks() -> None:
    for task_name in (OUTCOME_TASK_NAME, STOP_TASK_NAME, START_TASK_NAME):
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install/uninstall trad_bot Windows scheduled tasks")
    parser.add_argument("action", choices=("install", "uninstall"), nargs="?", default="install")
    parser.add_argument("--root", type=Path, default=project_root())
    args = parser.parse_args()

    if args.action == "install":
        install_tasks(root=args.root)
        print(
            f"Installed tasks: {START_TASK_NAME} on startup, "
            f"{STOP_TASK_NAME} weekdays at {STOP_TIME}, "
            f"{OUTCOME_TASK_NAME} every {OUTCOME_INTERVAL_MINUTES} minutes from {OUTCOME_START_TIME}"
        )
    else:
        uninstall_tasks()
        print(f"Deleted tasks if present: {START_TASK_NAME}, {STOP_TASK_NAME}, {OUTCOME_TASK_NAME}")


if __name__ == "__main__":
    main()
