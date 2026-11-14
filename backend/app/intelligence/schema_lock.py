"""Connection-scoped mutex shared by schema changes and database exports."""

from contextlib import contextmanager
from hashlib import sha256

from sqlalchemy import text


class SchemaLockUnavailable(RuntimeError):
    pass


@contextmanager
def schema_lock(connection, timeout=60):
    if connection.dialect.name == "postgresql":
        with postgres_schema_lock(connection, timeout):
            yield
        return
    if connection.dialect.name != "mysql":
        raise SchemaLockUnavailable("Unsupported database for schema mutex")
    if connection.in_transaction():
        raise SchemaLockUnavailable("Schema mutex requires a fresh connection")
    acquired = False
    name = None
    try:
        database = connection.scalar(text("SELECT DATABASE()"))
        if not isinstance(database, str) or not database:
            raise SchemaLockUnavailable("A dedicated target database is required")
        name = "citation-schema-" + sha256(database.encode()).hexdigest()[:40]
        acquired = (
            connection.scalar(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": name, "timeout": timeout},
            )
            == 1
        )
        # GET_LOCK is session-scoped and survives COMMIT. Clear SQLAlchemy's
        # implicit read transaction before Alembic detects transaction ownership.
        connection.commit()
        if not acquired:
            raise SchemaLockUnavailable("Schema mutex is busy or unavailable")
        yield
    except SchemaLockUnavailable:
        raise
    except Exception:
        raise SchemaLockUnavailable(
            "Schema operation failed; inspect protected diagnostics"
        ) from None
    finally:
        try:
            connection.rollback()
            if acquired:
                released = connection.scalar(
                    text("SELECT RELEASE_LOCK(:name)"), {"name": name}
                )
                connection.commit()
                if released != 1:
                    raise SchemaLockUnavailable(
                        "Schema mutex release was not confirmed"
                    )
        except Exception:
            # Closing the connection releases a named lock even after connection
            # failure. Never return an uncertain lock to a pool.
            connection.invalidate()
            raise SchemaLockUnavailable("Schema mutex cleanup failed") from None


@contextmanager
def postgres_schema_lock(connection, timeout):
    from time import monotonic, sleep

    if connection.in_transaction():
        raise SchemaLockUnavailable("Schema mutex requires a fresh connection")
    # Advisory locks are scoped to the current PostgreSQL database.
    key = int.from_bytes(sha256(b"citation-schema").digest()[:8], "big", signed=True)
    limit = monotonic() + timeout
    acquired = False
    try:
        while True:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            connection.commit()
            if acquired:
                break
            if monotonic() >= limit:
                raise SchemaLockUnavailable("Schema mutex is busy or unavailable")
            sleep(min(0.1, max(0, limit - monotonic())))
        yield
    finally:
        try:
            connection.rollback()
            if acquired:
                released = connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
                if not released:
                    raise SchemaLockUnavailable("Schema mutex release was not confirmed")
        except Exception:
            connection.invalidate()
            raise SchemaLockUnavailable("Schema mutex cleanup failed") from None
