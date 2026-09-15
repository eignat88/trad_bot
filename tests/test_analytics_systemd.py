"""Unit tests for analytics systemd configuration."""
from __future__ import annotations

import pytest
from pathlib import Path


class TestAnalyticsSystemd:
    """Tests for analytics systemd configuration."""

    def test_service_file_exists(self):
        """Test that service file exists."""
        service_file = Path("deploy/systemd/trad-bot-analytics.service")
        assert service_file.exists()

    def test_timer_file_exists(self):
        """Test that timer file exists."""
        timer_file = Path("deploy/systemd/trad-bot-analytics.timer")
        assert timer_file.exists()

    def test_finalize_service_file_exists(self):
        """Test that finalize service file exists."""
        service_file = Path("deploy/systemd/trad-bot-analytics-finalize.service")
        assert service_file.exists()

    def test_finalize_timer_file_exists(self):
        """Test that finalize timer file exists."""
        timer_file = Path("deploy/systemd/trad-bot-analytics-finalize.timer")
        assert timer_file.exists()

    def test_service_file_content(self):
        """Test service file content."""
        service_file = Path("deploy/systemd/trad-bot-analytics.service")
        content = service_file.read_text()
        
        assert "Type=oneshot" in content
        assert "User=tradbot-analytics" in content
        assert "Group=tradbot-analytics" in content
        assert "EnvironmentFile=/etc/trad-bot/analytics.env" in content
        assert "WorkingDirectory=/opt/trad_bot" in content
        assert "TimeoutStartSec=45min" in content
        assert "Nice=10" in content
        assert "CPUQuota=60%" in content
        assert "MemoryMax=1G" in content
        assert "NoNewPrivileges=true" in content

    def test_timer_file_content(self):
        """Test timer file content with Europe/Sofia timezone."""
        timer_file = Path("deploy/systemd/trad-bot-analytics.timer")
        content = timer_file.read_text()
        
        # Verify Europe/Sofia timezone is specified
        assert "OnCalendar=*-*-* 06:00:00 Europe/Sofia" in content
        assert "Persistent=true" in content
        assert "RandomizedDelaySec=0" in content
        assert "AccuracySec=1min" in content
        
        # Verify DST-safe calendar expression
        # The OnCalendar format handles DST automatically when timezone is specified
        assert "Europe/Sofia" in content

    def test_finalize_service_file_content(self):
        """Test finalize service file content."""
        service_file = Path("deploy/systemd/trad-bot-analytics-finalize.service")
        content = service_file.read_text()
        
        assert "Type=oneshot" in content
        assert "User=tradbot-analytics" in content
        assert "Group=tradbot-analytics" in content
        assert "EnvironmentFile=/etc/trad-bot/analytics.env" in content
        assert "WorkingDirectory=/opt/trad_bot" in content
        assert "TimeoutStartSec=45min" in content
        assert "Nice=10" in content
        assert "CPUQuota=60%" in content
        assert "MemoryMax=1G" in content
        assert "NoNewPrivileges=true" in content

    def test_finalize_timer_file_content(self):
        """Test finalize timer file content with Europe/Sofia timezone."""
        timer_file = Path("deploy/systemd/trad-bot-analytics-finalize.timer")
        content = timer_file.read_text()
        
        # Verify Europe/Sofia timezone is specified
        assert "OnCalendar=*-*-* 10:05:00 Europe/Sofia" in content
        assert "Persistent=true" in content
        assert "RandomizedDelaySec=0" in content
        assert "AccuracySec=1min" in content
        
        # Verify DST-safe calendar expression
        assert "Europe/Sofia" in content

    def test_service_file_has_correct_exec_start(self):
        """Test that service file has correct ExecStart."""
        service_file = Path("deploy/systemd/trad-bot-analytics.service")
        content = service_file.read_text()
        
        assert "ExecStart=/opt/trad_bot/.venv/bin/python -m app.analytics.cli run" in content

    def test_finalize_service_file_has_correct_exec_start(self):
        """Test that finalize service file has correct ExecStart."""
        service_file = Path("deploy/systemd/trad-bot-analytics-finalize.service")
        content = service_file.read_text()
        
        assert "ExecStart=/opt/trad_bot/.venv/bin/python -m app.analytics.cli finalize" in content

    def test_service_file_has_correct_dependencies(self):
        """Test that service file has correct dependencies."""
        service_file = Path("deploy/systemd/trad-bot-analytics.service")
        content = service_file.read_text()
        
        assert "After=network.target postgresql.service" in content
        assert "Wants=postgresql.service" in content

    def test_timer_file_has_correct_service_dependency(self):
        """Test that timer file has correct service dependency."""
        timer_file = Path("deploy/systemd/trad-bot-analytics.timer")
        content = timer_file.read_text()
        
        assert "Requires=trad-bot-analytics.service" in content

    def test_timer_dst_safe_calendar_expression(self):
        """Test that timer uses DST-safe calendar expression."""
        from zoneinfo import ZoneInfo
        from datetime import datetime, timezone
        
        # Verify Europe/Sofia timezone exists and handles DST
        tz_sofia = ZoneInfo("Europe/Sofia")
        
        # Test winter time (EET - Eastern European Time)
        winter_time = datetime(2026, 1, 15, 6, 0, tzinfo=tz_sofia)
        # Europe/Sofia is UTC+2 in winter (EET)
        assert winter_time.utcoffset().total_seconds() == 7200  # UTC+2
        
        # Test summer time (EEST - Eastern European Summer Time)
        summer_time = datetime(2026, 7, 15, 6, 0, tzinfo=tz_sofia)
        # Europe/Sofia is UTC+3 in summer (EEST)
        assert summer_time.utcoffset().total_seconds() == 10800  # UTC+3
        
        # Verify timer file specifies Europe/Sofia
        timer_file = Path("deploy/systemd/trad-bot-analytics.timer")
        content = timer_file.read_text()
        assert "Europe/Sofia" in content
        
        # Verify finalize timer also specifies Europe/Sofia
        finalize_timer_file = Path("deploy/systemd/trad-bot-analytics-finalize.timer")
        finalize_content = finalize_timer_file.read_text()
        assert "Europe/Sofia" in finalize_content