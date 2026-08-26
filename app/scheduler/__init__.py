"""Windows Task Scheduler helpers for trad_bot."""

from app.scheduler.windows_tasks import (
    START_TASK_NAME,
    STOP_TASK_NAME,
    build_start_task_command,
    build_stop_task_command,
    install_tasks,
    scheduled_launcher,
    stop_launcher,
    uninstall_tasks,
)

__all__ = [
    "START_TASK_NAME",
    "STOP_TASK_NAME",
    "build_start_task_command",
    "build_stop_task_command",
    "install_tasks",
    "scheduled_launcher",
    "stop_launcher",
    "uninstall_tasks",
]
