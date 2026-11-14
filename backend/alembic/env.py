from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.database import Base, sync_database_url
from app.models import models  # noqa: F401 - register models with Base
from app.models import device_gateway  # noqa: F401 - register models with Base
from app.intelligence import models as intelligence_models  # noqa: F401
from app.intelligence.schema_lock import schema_lock

config = context.config

db_url = sync_database_url()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(db_url, poolclass=pool.NullPool)

    try:
        with connectable.connect() as connection, schema_lock(connection):
            context.configure(connection=connection, target_metadata=target_metadata)

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
