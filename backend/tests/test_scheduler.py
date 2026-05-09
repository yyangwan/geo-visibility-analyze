"""Tests for scheduler cron parsing and timezone handling."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.scheduler import _parse_cron_field, should_run_now


class TestCronParsing:
    def test_wildcard(self):
        result = _parse_cron_field("*", range(0, 60))
        assert len(result) == 60
        assert result == list(range(0, 60))

    def test_single_value(self):
        result = _parse_cron_field("30", range(0, 60))
        assert result == [30]

    def test_list(self):
        result = _parse_cron_field("0,15,30,45", range(0, 60))
        assert result == [0, 15, 30, 45]

    def test_range(self):
        result = _parse_cron_field("1-5", range(0, 24))
        assert result == [1, 2, 3, 4, 5]

    def test_step(self):
        result = _parse_cron_field("*/15", range(0, 60))
        assert result == [0, 15, 30, 45]

    def test_step_with_start(self):
        result = _parse_cron_field("10/20", range(0, 60))
        assert result == [10, 30, 50]


class TestWeekdayConversion:
    """Verify cron weekdays (0=Sun) convert correctly to Python weekdays (0=Mon)."""

    def test_sunday_cron_0_matches_python_6(self):
        # Cron: 0 = Sunday -> Python: 6 = Sunday
        # "0 22 * * 0" should match Sunday
        sunday = datetime(2026, 5, 10, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        # 2026-05-10 is a Sunday
        assert sunday.weekday() == 6  # Python: Sunday = 6
        assert should_run_now("0 22 * * 0", sunday) is True

    def test_monday_cron_1_matches_python_0(self):
        monday = datetime(2026, 5, 11, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        # 2026-05-11 is a Monday
        assert monday.weekday() == 0  # Python: Monday = 0
        assert should_run_now("0 22 * * 1", monday) is True

    def test_saturday_cron_6_matches_python_5(self):
        saturday = datetime(2026, 5, 9, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        # 2026-05-09 is a Saturday
        assert saturday.weekday() == 5  # Python: Saturday = 5
        assert should_run_now("0 22 * * 6", saturday) is True

    def test_wrong_weekday_does_not_match(self):
        sunday = datetime(2026, 5, 10, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        # Sunday should NOT match cron weekday 1 (Monday)
        assert should_run_now("0 22 * * 1", sunday) is False


class TestShouldRunNow:
    def test_basic_match(self):
        dt = datetime(2026, 5, 9, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert should_run_now("0 22 * * *", dt) is True

    def test_wrong_minute(self):
        dt = datetime(2026, 5, 9, 22, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert should_run_now("0 22 * * *", dt) is False

    def test_wrong_hour(self):
        dt = datetime(2026, 5, 9, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert should_run_now("0 22 * * *", dt) is False

    def test_every_5_minutes(self):
        dt = datetime(2026, 5, 9, 22, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert should_run_now("*/5 * * * *", dt) is True

    def test_invalid_expression(self):
        dt = datetime(2026, 5, 9, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert should_run_now("invalid", dt) is False
        assert should_run_now("0 22 *", dt) is False


class TestTimezone:
    def test_default_timezone_is_shanghai(self):
        """Default timezone should be Asia/Shanghai."""
        from app.config import settings
        assert settings.tz == "Asia/Shanghai"

    def test_cron_evaluates_in_configured_tz(self):
        """Verify the scheduler uses the configured timezone, not UTC."""
        tz = ZoneInfo("Asia/Shanghai")
        # 22:00 Shanghai = 14:00 UTC
        dt_shanghai = datetime(2026, 5, 9, 22, 0, tzinfo=tz)
        dt_utc = datetime(2026, 5, 9, 14, 0, tzinfo=ZoneInfo("UTC"))

        # Should match in Shanghai timezone
        assert should_run_now("0 22 * * *", dt_shanghai) is True
        # Should NOT match if evaluated in UTC (14 != 22)
        assert should_run_now("0 22 * * *", dt_utc) is False
