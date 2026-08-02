"""
tests/test_store.py

Pure-function tests for the store's exact-match edit + surrogate-clean helpers —
the risky, judgment-heavy logic — with NO database. The DB-backed methods are
covered by scripts/smoke.sh against a live stack.
"""
import pytest

from store import EditError, _apply_edit, _clean


# ---------------------------------------------------------------------------
# _apply_edit — exact-match-or-fail (no silent corruption)
# ---------------------------------------------------------------------------

def test_replaces_a_unique_match():
    assert _apply_edit("hello world", "world", "there", False) == "hello there"


def test_not_found_raises():
    with pytest.raises(EditError) as exc:
        _apply_edit("abc", "xyz", "q", False)
    assert exc.value.kind == "not_found"


def test_not_unique_raises_with_count():
    with pytest.raises(EditError) as exc:
        _apply_edit("a a a", "a", "b", False)
    assert exc.value.kind == "not_unique"
    assert exc.value.count == 3


def test_replace_all_allows_multiple():
    assert _apply_edit("a a a", "a", "b", True) == "b b b"


def test_seed_empty_target_with_empty_old_string():
    # First write into an empty profile/resume: "" matches once -> sets content.
    assert _apply_edit("", "", "seed", False) == "seed"


# ---------------------------------------------------------------------------
# _clean — strip lone UTF-16 surrogates, keep valid text
# ---------------------------------------------------------------------------

def test_clean_strips_lone_surrogate():
    assert _clean("ok\udc9d") == "ok"


def test_clean_keeps_valid_unicode():
    assert _clean("café ✅ résumé") == "café ✅ résumé"


def test_clean_passes_none_through():
    assert _clean(None) is None
