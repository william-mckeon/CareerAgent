"""
tests/conftest.py

Test setup for careeragent-sessions. The module under test reads env at import,
so dummy values are set BEFORE importing. The src dir is put on sys.path so the
flat imports (`from store import ...`) resolve, matching the container layout
(PYTHONPATH=/app/src). Tests here are hermetic — no DB, no network.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

TEST_API_KEY = "test-sessions-key-for-pytest-1234567890"

os.environ["SESSIONS_API_KEY"] = TEST_API_KEY
os.environ["CAREERAGENT_API_KEY"] = "test-upstream-api-key"
os.environ["CAREERAGENT_API_URL"] = "http://careeragent-api:8001"
os.environ["SESSIONS_DB_USER"] = "careeragent_sessions"
os.environ["SESSIONS_DB_PASSWORD"] = "test-pw"
os.environ["SESSIONS_DB_HOST"] = "sessions-db"
os.environ["SESSIONS_DB_PORT"] = "5432"
os.environ["SESSIONS_DB_NAME"] = "careeragent_sessions"
os.environ["SESSIONS_DB_SCHEMA"] = "careeragent_sessions"

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def valid_api_key():
    return TEST_API_KEY
