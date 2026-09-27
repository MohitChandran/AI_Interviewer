import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from backend.config import get_settings
from backend.db.engine import create_engine
from backend.db.models import Base, UTCDateTime

config = context.config

# Only configure logging when run from the `alembic` CLI. When the app runs migrations at
# startup, fileConfig() would otherwise replace (and disable) the app's own log handlers.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _render_item(type_, obj, autogen_context):
    # Render our UTCDateTime wrapper as a plain column type so migrations don't import app code.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        # SQLite can't ALTER most things in place; batch mode rebuilds the table instead.
        render_as_batch=True,
        render_item=_render_item,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
