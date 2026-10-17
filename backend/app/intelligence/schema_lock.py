"""Connection-scoped mutex shared by schema changes and database exports."""

from contextlib import contextmanager
from hashlib import sha256

from sqlalchemy import text


class SchemaLockUnavailable(RuntimeError):
    pass


@contextmanager
def schema_lock(connection, timeout=60):
    if connection.dialect.name != "mysql":
        raise SchemaLockUnavailable("Schema mutex requires MySQL")
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
