"""Windows Task Scheduler helpers for trad_bot."""

from app.scheduler.windows_tasks import (
    OUTCOME_TASK_NAME,
    START_TASK_NAME,
    STOP_TASK_NAME,
    build_outcome_task_command,
    build_start_task_command,
    build_stop_task_command,
    install_tasks,
    outcome_launcher,
    scheduled_launcher,
    stop_launcher,
    uninstall_tasks,
)

__all__ = [
    "OUTCOME_TASK_NAME",
    "START_TASK_NAME",
    "STOP_TASK_NAME",
    "build_outcome_task_command",
    "build_start_task_command",
    "build_stop_task_command",
    "install_tasks",
    "outcome_launcher",
    "scheduled_launcher",
    "stop_launcher",
    "uninstall_tasks",
]
