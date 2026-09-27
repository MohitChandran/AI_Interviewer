import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings, get_settings
from backend.db.engine import create_engine, create_sessionmaker
from backend.db.migrations import upgrade_to_head


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch):
    """Tests never read the developer's .env, so they behave the same locally and in CI."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "0" * 40)  # the Deepgram SDK insists on 40 hex characters
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def database_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def settings(tmp_path, database_url, monkeypatch):
    """Point the app at a throwaway database and upload directory for each test."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
async def sessionmaker(database_url):
    """A migrated database, for testing repository and recorder code directly."""
    await asyncio.to_thread(upgrade_to_head, database_url)
    engine = create_engine(database_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
async def db(sessionmaker):
    async with sessionmaker() as session:
        yield session


@pytest.fixture
def client(settings):
    from backend.main import create_app

    # Entering the context runs the app's lifespan (migrations, engine setup).
    with TestClient(create_app()) as test_client:
        yield test_client
