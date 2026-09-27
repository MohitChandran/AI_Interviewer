from alembic import command
from alembic.config import Config

from backend.config import PROJECT_ROOT


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade_to_head(database_url: str) -> None:
    """Blocking: call from a worker thread (alembic's env.py runs its own event loop)."""
    command.upgrade(alembic_config(database_url), "head")
