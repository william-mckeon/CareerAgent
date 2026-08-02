# ============================================================================
# tests/test_conversations.py
# ----------------------------------------------------------------------------
# Unit tests for src/frontend/conversations.py (ConversationManager).
#
# Like test_sse_decoder.py these run with NO real Streamlit and NO network.
# Both `streamlit` and `requests` are replaced with lightweight fakes injected
# into sys.modules BEFORE importing the module under test, so the manager's
# STATE + HTTP logic (list / fetch / restore / switch / new / delete) is
# exercised in isolation against controllable fake responses.
#
# The Streamlit UI method (render_sidebar) is intentionally not covered here —
# it is thin glue over the same methods these tests drive directly, and adds
# only Streamlit-widget calls with no logic of its own.
#
# Coverage:
#   - list()             -> payload on 200; [] on non-200; [] on exception
#   - load()             -> binds transcript + conversation_id + ?c= on 200
#                        -> missing conversation toasts, leaves state untouched
#   - restore_from_url() -> one-shot guard; no-op without ?c=; binds on hit
#   - start_new()        -> clears transcript, unbinds id, drops ?c=
#   - delete()           -> active conversation resets to new; other is left
# ============================================================================

import sys
import types

import pytest


# ----------------------------------------------------------------------------
# Fakes for streamlit + requests, injected before importing the module.
# ----------------------------------------------------------------------------

class FakeSessionState(dict):
    """Mimics st.session_state: attribute access AND item access, plus .get()."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class FakeQueryParams(dict):
    """Mimics st.query_params closely enough: get / __setitem__ / __delitem__."""


class FakeResponse:
    """Stand-in for a requests.Response with just the bits the manager uses."""

    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Recorder:
    """Callable that records calls and returns (or raises) a fixed response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _guard(*_args, **_kwargs):
    raise AssertionError("requests call not configured for this test")


# Install the fakes ONCE, before importing conversations. The module binds
# `import streamlit as st` / `import requests` at import time, but resolves
# `st.session_state`, `requests.get`, etc. at call time — so per-test resets of
# those attributes (in the autouse fixture below) take effect on the module.
_FAKE_ST = types.ModuleType("streamlit")
_FAKE_ST.session_state = FakeSessionState()
_FAKE_ST.query_params = FakeQueryParams()
_FAKE_ST.toast = lambda *a, **k: None
_FAKE_ST.markdown = lambda *a, **k: None
_FAKE_ST.divider = lambda *a, **k: None
_FAKE_ST.caption = lambda *a, **k: None
_FAKE_ST.button = lambda *a, **k: False
_FAKE_ST.rerun = lambda *a, **k: None
sys.modules["streamlit"] = _FAKE_ST

_FAKE_REQUESTS = types.ModuleType("requests")
_FAKE_REQUESTS.get = _guard
_FAKE_REQUESTS.delete = _guard
sys.modules["requests"] = _FAKE_REQUESTS

from frontend.conversations import ConversationManager  # noqa: E402


BASE = "http://careeragent-sessions:8005"
KEY = "key-123"
TIMEOUT = 5


@pytest.fixture(autouse=True)
def _reset_fakes():
    """Fresh session_state / query_params and unconfigured requests per test."""
    _FAKE_ST.session_state = FakeSessionState()
    _FAKE_ST.query_params = FakeQueryParams()
    _FAKE_REQUESTS.get = _guard
    _FAKE_REQUESTS.delete = _guard
    yield


@pytest.fixture
def mgr():
    return ConversationManager(BASE, KEY, TIMEOUT)


# ----------------------------------------------------------------------------
# list()
# ----------------------------------------------------------------------------

def test_list_returns_payload_on_200(mgr):
    payload = [{"conversation_id": "a", "title": "Hi", "message_count": 2}]
    rec = Recorder(FakeResponse(200, payload))
    _FAKE_REQUESTS.get = rec

    assert mgr.list() == payload
    # Correct endpoint + auth header + timeout.
    assert rec.calls[0]["url"] == f"{BASE}/conversations"
    assert rec.calls[0]["headers"] == {"X-API-Key": KEY}
    assert rec.calls[0]["timeout"] == TIMEOUT


def test_list_fails_open_on_non_200(mgr):
    _FAKE_REQUESTS.get = Recorder(FakeResponse(500, None))
    assert mgr.list() == []


def test_list_fails_open_on_exception(mgr):
    _FAKE_REQUESTS.get = Recorder(ConnectionError("sessions down"))
    assert mgr.list() == []


# ----------------------------------------------------------------------------
# load()
# ----------------------------------------------------------------------------

def test_load_binds_transcript_id_and_url(mgr):
    conv = {
        "conversation_id": "abc",
        "messages": [
            {"role": "user", "content": "hello", "idx": 0},
            {"role": "assistant", "content": "hi there", "idx": 1},
        ],
    }
    rec = Recorder(FakeResponse(200, conv))
    _FAKE_REQUESTS.get = rec

    mgr.load("abc")

    # Only role/content survive into session_state.
    assert _FAKE_ST.session_state.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    assert _FAKE_ST.session_state.conversation_id == "abc"
    assert _FAKE_ST.query_params["c"] == "abc"
    assert rec.calls[0]["url"] == f"{BASE}/conversations/abc"


def test_load_missing_conversation_leaves_state(mgr):
    _FAKE_REQUESTS.get = Recorder(FakeResponse(404, None))
    # Pre-existing state must be untouched on a failed load.
    _FAKE_ST.session_state.messages = [{"role": "user", "content": "keep me"}]
    _FAKE_ST.session_state.conversation_id = "current"

    mgr.load("ghost")

    assert _FAKE_ST.session_state.messages == [{"role": "user", "content": "keep me"}]
    assert _FAKE_ST.session_state.conversation_id == "current"
    assert "c" not in _FAKE_ST.query_params


# ----------------------------------------------------------------------------
# restore_from_url()
# ----------------------------------------------------------------------------

def test_restore_is_one_shot(mgr):
    # Guard already set -> must not touch the network at all.
    _FAKE_ST.session_state.history_loaded = True
    _FAKE_ST.query_params["c"] = "abc"
    _FAKE_REQUESTS.get = _guard  # any call raises AssertionError

    mgr.restore_from_url()  # should return immediately, no request

    assert _FAKE_ST.session_state.history_loaded is True


def test_restore_without_param_is_noop_but_sets_guard(mgr):
    _FAKE_REQUESTS.get = _guard  # no ?c= -> never called
    mgr.restore_from_url()
    assert _FAKE_ST.session_state.history_loaded is True
    assert "messages" not in _FAKE_ST.session_state


def test_restore_loads_transcript_when_param_present(mgr):
    conv = {"conversation_id": "xyz", "messages": [{"role": "user", "content": "q"}]}
    _FAKE_ST.query_params["c"] = "xyz"
    _FAKE_REQUESTS.get = Recorder(FakeResponse(200, conv))

    mgr.restore_from_url()

    assert _FAKE_ST.session_state.history_loaded is True
    assert _FAKE_ST.session_state.conversation_id == "xyz"
    assert _FAKE_ST.session_state.messages == [{"role": "user", "content": "q"}]


# ----------------------------------------------------------------------------
# start_new()
# ----------------------------------------------------------------------------

def test_start_new_clears_state_and_url(mgr):
    _FAKE_ST.session_state.messages = [{"role": "user", "content": "old"}]
    _FAKE_ST.session_state.conversation_id = "old-id"
    _FAKE_ST.query_params["c"] = "old-id"

    mgr.start_new()

    assert _FAKE_ST.session_state.messages == []
    assert _FAKE_ST.session_state.conversation_id is None
    assert "c" not in _FAKE_ST.query_params


def test_start_new_tolerates_missing_url_param(mgr):
    # No ?c= present -> the del is swallowed, no error.
    mgr.start_new()
    assert _FAKE_ST.session_state.conversation_id is None


# ----------------------------------------------------------------------------
# delete()
# ----------------------------------------------------------------------------

def test_delete_active_conversation_resets_to_new(mgr):
    _FAKE_ST.session_state.messages = [{"role": "user", "content": "x"}]
    _FAKE_ST.session_state.conversation_id = "active"
    _FAKE_ST.query_params["c"] = "active"
    rec = Recorder(FakeResponse(200, {"deleted": "active"}))
    _FAKE_REQUESTS.delete = rec

    mgr.delete("active")

    # Deleting the open conversation falls back to a fresh one.
    assert _FAKE_ST.session_state.conversation_id is None
    assert _FAKE_ST.session_state.messages == []
    assert "c" not in _FAKE_ST.query_params
    assert rec.calls[0]["url"] == f"{BASE}/conversations/active"


def test_delete_other_conversation_keeps_state(mgr):
    _FAKE_ST.session_state.messages = [{"role": "user", "content": "x"}]
    _FAKE_ST.session_state.conversation_id = "active"
    _FAKE_ST.query_params["c"] = "active"
    _FAKE_REQUESTS.delete = Recorder(FakeResponse(200, {"deleted": "other"}))

    mgr.delete("other")

    # Deleting a different conversation must not disturb the open one.
    assert _FAKE_ST.session_state.conversation_id == "active"
    assert _FAKE_ST.session_state.messages == [{"role": "user", "content": "x"}]
    assert _FAKE_ST.query_params["c"] == "active"
