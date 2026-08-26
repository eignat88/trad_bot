"""Install Windows scheduled tasks for scanner auto-start and weekday stop."""
from __future__ import annotations

from app.scheduler.windows_tasks import main


if __name__ == "__main__":
    main()
