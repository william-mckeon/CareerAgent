"""
tests/conftest.py

Test setup for the careeragent-infra proxy (src/api/main.py).

The module under test reads its configuration from environment variables AT
IMPORT TIME (API_KEY, AWS_REGION_NAME, BASE_MODEL, ... see src/api/main.py). It
also calls load_dotenv() at import. load_dotenv() does NOT override variables
already present in os.environ, so by setting our dummy test values here BEFORE
the module is ever imported we guarantee deterministic config regardless of any
.env file sitting in the repo.

No real Bedrock call is made by any test here — every case is rejected before a
provider call (bad auth, validation, unconfigured route) or is a pure function /
local check (health reads config + credential presence only).

We also put the repo root on sys.path so `import src.api.main` resolves.
"""

import os
import sys

# --- Fix sys.path so the `src` package is importable from the repo root. ------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# --- Dummy test configuration, set BEFORE importing the module under test. ----
# These shadow anything in .env (load_dotenv won't override existing values).
TEST_API_KEY = "test-secret-key-for-pytest-1234567890"
TEST_BASE_MODEL = "bedrock/us.anthropic.claude-opus-4-8"
TEST_NERVOUS_MODEL = "bedrock/us.anthropic.claude-haiku-4-5"

os.environ["API_KEY"] = TEST_API_KEY
os.environ["AWS_REGION_NAME"] = "us-east-1"
# Dummy AWS credentials so the health credential-presence check passes. They are
# never used to make a real call in these tests.
os.environ["AWS_ACCESS_KEY_ID"] = "test-access-key-id"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test-secret-access-key"
os.environ["BASE_MODEL"] = TEST_BASE_MODEL
os.environ["NERVOUS_SYSTEM_MODEL"] = TEST_NERVOUS_MODEL
# Set EMBEDDING_MODEL to empty (NOT just unset) so the "not configured" path is
# exercised. An explicitly-set value (even "") is never overridden by
# load_dotenv(), which guarantees a deterministic test config.
os.environ["EMBEDDING_MODEL"] = ""
os.environ["REASONING_EFFORT"] = "medium"

import pytest  # noqa: E402

# Import the module under test now that the environment is prepared.
from src.api import main as main_module  # noqa: E402


@pytest.fixture(scope="session")
def main():
    """The imported src.api.main module under test."""
    return main_module


@pytest.fixture(scope="session")
def valid_api_key():
    return TEST_API_KEY


@pytest.fixture
def client(main):
    """
    TestClient bound to the FastAPI app.

    Using it as a context manager runs the app lifespan (startup/shutdown),
    which is where REASONING_EFFORT validation and config logging happen.
    """
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c
