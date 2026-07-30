from datetime import datetime, timezone

from app.utils.timezone import utc_isoformat, utcnow


def test_utcnow_returns_naive_utc_for_mysql_datetime():
    value = utcnow()

    assert value.tzinfo is None
    assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - value).total_seconds()) < 1


def test_utc_isoformat_marks_naive_database_values_as_utc():
    assert utc_isoformat(datetime(2026, 7, 29, 23, 50, 25)) == "2026-07-29T23:50:25Z"


def test_utc_isoformat_normalizes_aware_values():
    value = datetime.fromisoformat("2026-07-30T07:50:25+08:00")

    assert utc_isoformat(value) == "2026-07-29T23:50:25Z"
