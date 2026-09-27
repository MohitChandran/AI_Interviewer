from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from backend.db.migrations import upgrade_to_head
from backend.db.models import Base


def test_migrations_match_models(tmp_path):
    """Fails if someone edits models.py without generating a migration."""
    db_file = tmp_path / "migrated.db"
    upgrade_to_head(f"sqlite+aiosqlite:///{db_file}")

    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()

    assert diff == []
