# ============================================================================
# careeragent-api - Test bootstrap
# Maintainer: William McKeon
# ============================================================================
#
# Puts <repo>/src on sys.path so the signing helpers import the same way the
# production runtime does (PYTHONPATH=/app/src per Dockerfile):
#
#     from client.logger import _canonical_string
#
# That keeps the tests exercising the real import path rather than a synthetic
# one, so a packaging regression would surface here too.
# ============================================================================

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")

if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
