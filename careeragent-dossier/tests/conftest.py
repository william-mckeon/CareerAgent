"""
tests/conftest.py

Test setup for careeragent-dossier. Modules read env at import, so dummy values
are set BEFORE importing. The src dir is put on sys.path so the flat imports
(`from store import ...`) resolve, matching the container layout
(PYTHONPATH=/app/src). Tests here are hermetic — no DB, no network.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

TEST_API_KEY = "test-dossier-key-for-pytest-1234567890"

os.environ["DOSSIER_API_KEY"] = TEST_API_KEY
os.environ["DOSSIER_DB_USER"] = "careeragent_dossier"
os.environ["DOSSIER_DB_PASSWORD"] = "test-pw"
os.environ["DOSSIER_DB_HOST"] = "dossier-db"
os.environ["DOSSIER_DB_PORT"] = "5432"
os.environ["DOSSIER_DB_NAME"] = "careeragent_dossier"
os.environ["DOSSIER_DB_SCHEMA"] = "careeragent_dossier"

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def valid_api_key():
    return TEST_API_KEY
