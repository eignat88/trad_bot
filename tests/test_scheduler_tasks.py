from pathlib import Path

from app.scheduler.windows_tasks import (
    OUTCOME_INTERVAL_MINUTES,
    OUTCOME_START_TIME,
    OUTCOME_TASK_NAME,
    PAPER_TASK_NAME,
    START_TASK_NAME,
    STOP_TASK_NAME,
    STOP_TIME,
    WEEKDAYS_SHORT,
    build_outcome_task_command,
    build_paper_task_command,
    build_start_task_command,
    build_stop_task_command,
    outcome_launcher,
    paper_launcher,
    scheduled_launcher,
    stop_launcher,
)


def test_start_task_runs_scanner_on_boot():
    root = Path(r"D:\py_pro\trad_bot")
    command = build_start_task_command(root=root)

    assert START_TASK_NAME == "BybitScanner"
    assert command == [
        "schtasks", "/Create",
        "/TN", "BybitScanner",
        "/TR", f'cmd.exe /c "{root / "run_scanner_task.bat"}"',
        "/SC", "ONSTART",
        "/RU", "SYSTEM",
        "/RL", "HIGHEST",
        "/F",
    ]
    assert scheduled_launcher(root) == root / "run_scanner_task.bat"


def test_paper_task_runs_paper_gate_on_boot():
    root = Path(r"D:\py_pro\trad_bot")
    command = build_paper_task_command(root=root)

    assert PAPER_TASK_NAME == "BybitPaperRunner"
    assert command == [
        "schtasks", "/Create",
        "/TN", "BybitPaperRunner",
        "/TR", f'cmd.exe /c "{root / "run_paper_task.bat"}"',
        "/SC", "ONSTART",
        "/RU", "SYSTEM",
        "/RL", "HIGHEST",
        "/F",
    ]
    assert paper_launcher(root) == root / "run_paper_task.bat"


def test_stop_task_runs_weekdays_at_1800():
    root = Path(r"D:\py_pro\trad_bot")
    command = build_stop_task_command(root=root)

    assert STOP_TASK_NAME == "BybitScannerStop"
    assert STOP_TIME == "18:00"
    assert WEEKDAYS_SHORT == "MON,TUE,WED,THU,FRI"
    assert command == [
        "schtasks", "/Create",
        "/TN", "BybitScannerStop",
        "/TR", f'cmd.exe /c "{root / "stop_scanner.bat"}"',
        "/SC", "WEEKLY",
        "/D", "MON,TUE,WED,THU,FRI",
        "/ST", "18:00",
        "/RU", "SYSTEM",
        "/RL", "HIGHEST",
        "/F",
    ]
    assert stop_launcher(root) == root / "stop_scanner.bat"


def test_outcome_task_runs_hourly():
    root = Path(r"D:\py_pro\trad_bot")
    command = build_outcome_task_command(root=root)

    assert OUTCOME_TASK_NAME == "BybitScannerOutcomeBackfill"
    assert OUTCOME_START_TIME == "09:05"
    assert OUTCOME_INTERVAL_MINUTES == 60
    assert command == [
        "schtasks", "/Create",
        "/TN", "BybitScannerOutcomeBackfill",
        "/TR", f'cmd.exe /c "{root / "run_outcome_backfill.bat"}"',
        "/SC", "HOURLY",
        "/MO", "1",
        "/ST", "09:05",
        "/F",
    ]
    assert outcome_launcher(root) == root / "run_outcome_backfill.bat"
