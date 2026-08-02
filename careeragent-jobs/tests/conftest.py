"""
tests/conftest.py

Test setup for careeragent-jobs. Modules read env, so dummy values are set
BEFORE importing anything under test. The src dir is put on sys.path so the flat
imports (`from store import ...`) resolve, matching the container layout
(PYTHONPATH=/app/src). The pure-logic + worker + jobtypes + API-auth tests are
hermetic (no DB, no network). The store round-trip tests need a live Postgres
and SKIP when one is not reachable (see tests/test_store.py).
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

TEST_API_KEY = "test-jobs-key-for-pytest-1234567890"

os.environ["JOBS_API_KEY"] = TEST_API_KEY
os.environ["JOBS_DB_USER"] = "careeragent_jobs"
os.environ["JOBS_DB_PASSWORD"] = "test-pw"
os.environ["JOBS_DB_HOST"] = "jobs-db"
os.environ["JOBS_DB_PORT"] = "5432"
os.environ["JOBS_DB_NAME"] = "careeragent_jobs"
os.environ["JOBS_DB_SCHEMA"] = "careeragent_jobs"
os.environ.setdefault("SESSIONS_URL", "http://careeragent-sessions:8005")
os.environ.setdefault("SESSIONS_API_KEY", "test-sessions-key")
os.environ.setdefault("REVIEW_URL", "http://careeragent-review:8007")
os.environ.setdefault("REVIEW_API_KEY", "test-review-key")
os.environ.setdefault("CODE_URL", "http://careeragent-code:8012")
os.environ.setdefault("CODE_API_KEY", "test-code-key")

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def valid_api_key():
    return TEST_API_KEY
