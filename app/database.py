from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,            # Per worker
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,      # Recycle idle connection periodically
)


@event.listens_for(engine.sync_engine, "connect")
def _register_pgvector(dbapi_connection, _connection_record):
    """
    Register pgvector's asyncpg type codec on every new connection so that
    SQLAlchemy can read/write vector(N) columns without manual casting.
    """
    try:
        from pgvector.asyncpg import register_vector
        dbapi_connection.run_sync(register_vector)
    except Exception:
        # pgvector extension not installed — vector columns will still work
        # via text fallback; search queries will fail until extension is enabled.
        pass


AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
