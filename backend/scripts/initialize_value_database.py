"""Initialize an explicitly named empty MySQL DB; never drop, move or upgrade data."""

import argparse
import json
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from alembic import command
from app.config import settings
from app.database import Base
from app.intelligence import models as intelligence_models  # noqa: F401
from app.intelligence.schema_version import SCHEMA_REVISION
from app.models import device_gateway, models  # noqa: F401


def initialize(expected_database):
    configured = make_url(settings.database_url)
    if not configured.drivername.startswith("mysql"):
        raise ValueError("This initializer supports the audit service MySQL database")
    url = configured.set(drivername="mysql+pymysql")
    if url.database != expected_database or not expected_database:
        raise ValueError(
            "Configured database does not match the explicitly named target"
        )
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            revision = MigrationContext.configure(connection).get_current_revision()
            if revision == SCHEMA_REVISION:
                expected = set(Base.metadata.tables)
                if tables - {"alembic_version"} != expected:
                    raise ValueError(
                        "Schema identity matches but table contract is incomplete"
                    )
                for table in Base.metadata.sorted_tables:
                    if {
                        row["name"] for row in inspector.get_columns(table.name)
                    } != set(table.columns.keys()):
                        raise ValueError(
                            "Schema identity matches but column contract differs"
                        )
                return {
                    "status": "already_initialized",
                    "schema": SCHEMA_REVISION,
                    "tables": len(expected),
                }
            if tables:
                raise ValueError(
                    "Target is not empty; no existing data is upgraded or removed"
                )
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(config, "head")
        return {
            "status": "fresh_database_initialized",
            "schema": SCHEMA_REVISION,
            "tables": len(Base.metadata.tables),
        }
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-database", required=True)
    args = parser.parse_args()
    try:
        result = initialize(args.expected_database)
    except Exception as error:
        # Never print connection URLs or credential-bearing DB exceptions.
        print(
            json.dumps(
                {
                    "status": "initialization_refused",
                    "error_class": type(error).__name__,
                }
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
