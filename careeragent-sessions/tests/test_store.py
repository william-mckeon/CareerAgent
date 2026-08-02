"""tests/test_store.py — pure DB-URL building (no real database touched)."""
import importlib
import os


def _reload_store():
    import store
    return importlib.reload(store)


class TestDatabaseUrl:
    def test_builds_asyncpg_url_from_parts(self, monkeypatch):
        monkeypatch.delenv("SESSIONS_DATABASE_URL", raising=False)
        monkeypatch.setenv("SESSIONS_DB_USER", "u")
        monkeypatch.setenv("SESSIONS_DB_PASSWORD", "p")
        monkeypatch.setenv("SESSIONS_DB_HOST", "h")
        monkeypatch.setenv("SESSIONS_DB_PORT", "6543")
        monkeypatch.setenv("SESSIONS_DB_NAME", "db")
        store = _reload_store()
        assert store._database_url() == "postgresql+asyncpg://u:p@h:6543/db"

    def test_explicit_url_wins(self, monkeypatch):
        monkeypatch.setenv("SESSIONS_DATABASE_URL", "postgresql+asyncpg://x/y")
        store = _reload_store()
        assert store._database_url() == "postgresql+asyncpg://x/y"

    def test_schema_default(self, monkeypatch):
        monkeypatch.delenv("SESSIONS_DB_SCHEMA", raising=False)
        store = _reload_store()
        assert store.SCHEMA == "careeragent_sessions"
