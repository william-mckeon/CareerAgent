"""Test bootstrap for careeragent-memory.

In Docker the service runs with PYTHONPATH=src, so modules import flat:
`import retrieval`, `from client.infra import InfraClient`, `from store import
Store`. These tests run from the repo root, so we prepend <repo>/src to sys.path
here to reproduce that import layout without setting an env var.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
