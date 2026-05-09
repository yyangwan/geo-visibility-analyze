from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_recycle=3600,
    pool_pre_ping=True,
    connect_args={"charset": "utf8mb4"},
)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    """Run Alembic migrations on startup."""
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    # Point to the correct script location (relative to backend/)
    alembic_cfg.set_main_option("script_location", "alembic")

    # Use sync URL for Alembic
    db_url = settings.database_url.replace("+aiomysql", "+pymysql")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    command.upgrade(alembic_cfg, "head")
