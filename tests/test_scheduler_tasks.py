from pathlib import Path

from app.scheduler.windows_tasks import (
    START_TASK_NAME,
    STOP_TASK_NAME,
    STOP_TIME,
    WEEKDAYS_SHORT,
    build_start_task_command,
    build_stop_task_command,
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
