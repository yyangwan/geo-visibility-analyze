from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


def sync_database_url(database_url=None):
    url = make_url(database_url or settings.database_url)
    if url.get_backend_name() == "mysql":
        return url.set(drivername="mysql+pymysql")
    return url


database_url = make_url(settings.database_url)
connect_args = (
    {"options": "-c timezone=UTC"}
    if database_url.get_backend_name() == "postgresql"
    else {"charset": "utf8mb4", "init_command": "SET time_zone = '+00:00'"}
)

engine = create_async_engine(
    database_url,
    echo=False,
    pool_recycle=3600,
    pool_pre_ping=True,
    connect_args=connect_args,
)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session
