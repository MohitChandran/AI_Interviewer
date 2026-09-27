from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends
from fastapi.requests import HTTPConnection
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionMaker = async_sessionmaker[AsyncSession]


def _configure_sqlite(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    # SQLite ignores foreign keys (and so ON DELETE CASCADE) unless enabled per connection.
    cursor.execute("PRAGMA foreign_keys=ON")
    # Write-ahead logging lets HTTP reads proceed while an interview is writing.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(url, echo=echo)
    if is_sqlite:
        event.listen(engine.sync_engine, "connect", _configure_sqlite)
    return engine


def create_sessionmaker(engine: AsyncEngine) -> SessionMaker:
    # expire_on_commit=False: after commit, attributes stay readable. Otherwise the next
    # attribute access would trigger an implicit reload, which async sessions can't do.
    return async_sessionmaker(engine, expire_on_commit=False)


def get_sessionmaker(conn: HTTPConnection) -> SessionMaker:
    """FastAPI dependency; works for both HTTP and WebSocket routes."""
    return conn.app.state.db_sessionmaker


async def get_db(sessionmaker: SessionMaker = Depends(get_sessionmaker)) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per HTTP request."""
    async with sessionmaker() as session:
        yield session
