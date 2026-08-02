# ============================================================================
# tests/conftest.py
# ----------------------------------------------------------------------------
# Makes the SSE decoder importable from the repo root the same way it is in
# the Docker image, where PYTHONPATH=src and the package is imported as
# `frontend.sse_decoder`. We prepend <repo_root>/src to sys.path so that
# `from frontend.sse_decoder import ...` resolves during local pytest runs
# without requiring Streamlit, network, or any production wiring.
# ============================================================================

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
