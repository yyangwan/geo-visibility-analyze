from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return naive UTC datetime (strips tzinfo for MySQL DATETIME compatibility)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_isoformat(value: datetime | None) -> str | None:
    """Serialize a UTC datetime with an explicit timezone marker."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
