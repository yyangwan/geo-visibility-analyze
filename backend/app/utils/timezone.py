from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return naive UTC datetime (strips tzinfo for MySQL DATETIME compatibility)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
