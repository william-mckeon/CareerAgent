"""
tests/test_main.py

Tests for the careeragent-infra proxy (src/api/main.py), Bedrock backend.

Scope: authentication and the pure request-shaping/validation/routing logic — no
real Bedrock call is made. Every path exercised here rejects the request BEFORE
any provider call, or is a pure function / local check, so the tests are
hermetic.

Env config is set up in conftest.py before the module is imported.
"""

import pytest
from fastapi import HTTPException

from src.api.main import Message, to_message_dicts, verify_api_key

from .conftest import TEST_API_KEY


# ---------------------------------------------------------------------------
# to_message_dicts — pure request-shaping logic
# ---------------------------------------------------------------------------
class TestToMessageDicts:
    def test_converts_messages_to_plain_dicts(self):
        messages = [
            Message(role="system", content="You are CareerAgent."),
            Message(role="user", content="hello"),
        ]
        result = to_message_dicts(messages)

        assert result == [
            {"role": "system", "content": "You are CareerAgent."},
            {"role": "user", "content": "hello"},
        ]

    def test_preserves_order_and_all_roles(self):
        messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="question"),
            Message(role="assistant", content="answer"),
            Message(role="user", content="follow-up"),
        ]
        result = to_message_dicts(messages)

        assert [m["role"] for m in result] == ["system", "user", "assistant", "user"]
        assert result[3] == {"role": "user", "content": "follow-up"}

    def test_does_not_mutate_input_messages(self):
        original_content = "You are CareerAgent."
        messages = [
            Message(role="system", content=original_content),
            Message(role="user", content="hello"),
        ]
        to_message_dicts(messages)

        assert messages[0].content == original_content

    def test_returns_plain_dicts(self):
        messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="u"),
        ]
        result = to_message_dicts(messages)

        assert all(isinstance(m, dict) for m in result)
        assert all(set(m.keys()) == {"role", "content"} for m in result)


# ---------------------------------------------------------------------------
# verify_api_key — dependency-level auth logic (called directly with await)
# ---------------------------------------------------------------------------
class TestVerifyApiKey:
    @pytest.mark.asyncio
    async def test_correct_key_passes(self):
        result = await verify_api_key(key=TEST_API_KEY)
        assert result == TEST_API_KEY

    @pytest.mark.asyncio
    async def test_wrong_key_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(key="totally-wrong-key")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_key_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(key="")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_none_key_raises_401(self):
        # APIKeyHeader with auto_error=False passes None when the header is absent.
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(key=None)
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# resolve_chat_model — pure model -> Bedrock model id routing
# ---------------------------------------------------------------------------
class TestResolveChatModel:
    def test_base_routes_to_base_model(self, main):
        assert main.resolve_chat_model("base") == main.BASE_MODEL

    def test_nervous_system_routes_to_nervous_model(self, main):
        assert main.resolve_chat_model("nervous_system") == main.NERVOUS_SYSTEM_MODEL

    def test_unexpected_model_raises_value_error(self, main):
        with pytest.raises(ValueError):
            main.resolve_chat_model("not_a_real_model")

    def test_unconfigured_nervous_system_returns_falsy(self, main, monkeypatch):
        # When NERVOUS_SYSTEM_MODEL is empty the resolver returns the empty
        # string, which the /chat endpoint treats as "not configured" (503).
        monkeypatch.setattr(main, "NERVOUS_SYSTEM_MODEL", "")
        assert main.resolve_chat_model("nervous_system") == ""


# ---------------------------------------------------------------------------
# /chat endpoint — auth + pre-stream validation via TestClient
#
# All cases here are rejected BEFORE any Bedrock call: bad/missing auth,
# empty messages, missing user message, and an unconfigured route. None of
# them open the StreamingResponse, so Bedrock is never contacted.
# ---------------------------------------------------------------------------
class TestChatEndpoint:
    def _body(self, **overrides):
        body = {
            "messages": [
                {"role": "system", "content": "You are CareerAgent."},
                {"role": "user", "content": "hello"},
            ],
            "reasoning_effort": "medium",
        }
        body.update(overrides)
        return body

    def test_no_api_key_returns_401(self, client):
        resp = client.post("/chat", json=self._body())
        assert resp.status_code == 401

    def test_wrong_api_key_returns_401(self, client):
        resp = client.post(
            "/chat",
            json=self._body(),
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_valid_key_empty_messages_returns_400(self, client, valid_api_key):
        resp = client.post(
            "/chat",
            json={"messages": [], "reasoning_effort": "medium"},
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"].lower()

    def test_valid_key_no_user_message_returns_400(self, client, valid_api_key):
        resp = client.post(
            "/chat",
            json={
                "messages": [{"role": "system", "content": "only a system msg"}],
                "reasoning_effort": "medium",
            },
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 400
        assert "user" in resp.json()["detail"].lower()

    def test_valid_key_unconfigured_nervous_system_returns_503(
        self, client, valid_api_key, main, monkeypatch
    ):
        # Force the nervous-system route to be unconfigured and confirm the
        # endpoint returns a real 503 before any streaming begins.
        monkeypatch.setattr(main, "NERVOUS_SYSTEM_MODEL", "")
        resp = client.post(
            "/chat",
            json=self._body(model="nervous_system"),
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /embed endpoint — auth + validation (no embedding model configured in test env)
# ---------------------------------------------------------------------------
class TestEmbedEndpoint:
    def test_no_api_key_returns_401(self, client):
        resp = client.post("/embed", json={"input": "hello"})
        assert resp.status_code == 401

    def test_valid_key_empty_string_returns_400(self, client, valid_api_key):
        resp = client.post(
            "/embed",
            json={"input": "   "},
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 400

    def test_valid_key_empty_list_returns_400(self, client, valid_api_key):
        resp = client.post(
            "/embed",
            json={"input": []},
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 400

    def test_unconfigured_embedding_model_returns_503(self, client, valid_api_key):
        # EMBEDDING_MODEL is unset in the test env (see conftest), so a valid,
        # non-empty request reaches the "not configured" guard before any
        # Bedrock call.
        resp = client.post(
            "/embed",
            json={"input": "valid text"},
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /health endpoint — no auth required, must return 200 with the right shape
#
# /health is a local config + credential-presence check (no Bedrock call). In
# the test env BASE_MODEL + dummy AWS creds + region are set, so base_model is
# "ok"; EMBEDDING_MODEL is unset, so embedding is "not configured".
# ---------------------------------------------------------------------------
class TestHealthEndpoint:
    def test_health_returns_200_without_auth(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_json_shape(self, client):
        body = client.get("/health").json()
        for field in ("status", "proxy", "base_model", "nervous_system", "embedding"):
            assert field in body
        assert body["proxy"] == "ok"
        assert body["status"] in ("ok", "degraded")
        # EMBEDDING_MODEL is unset in the test env -> "not configured".
        assert body["embedding"] == "not configured"


# ---------------------------------------------------------------------------
# /complete endpoint — tool transport (non-streaming)
#
# The validation cases are hermetic (rejected before any Bedrock call). The
# transport case monkeypatches litellm.acompletion with a fake so no real
# Bedrock call is made, and asserts tools go IN and tool_calls come BACK — the
# whole contract of this endpoint.
# ---------------------------------------------------------------------------
class TestCompleteEndpoint:
    def _body(self, **overrides):
        body = {
            "messages": [{"role": "user", "content": "what am I tracking?"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "search_applications",
                    "description": "find apps",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            "tool_choice": "auto",
            "reasoning_effort": "low",
        }
        body.update(overrides)
        return body

    def test_no_api_key_returns_401(self, client):
        assert client.post("/complete", json=self._body()).status_code == 401

    def test_empty_messages_returns_400(self, client, valid_api_key):
        resp = client.post(
            "/complete", json={"messages": []}, headers={"X-API-Key": valid_api_key}
        )
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"].lower()

    def test_unconfigured_nervous_system_returns_503(
        self, client, valid_api_key, main, monkeypatch
    ):
        monkeypatch.setattr(main, "NERVOUS_SYSTEM_MODEL", "")
        resp = client.post(
            "/complete",
            json=self._body(model="nervous_system"),
            headers={"X-API-Key": valid_api_key},
        )
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"].lower()

    def test_tools_pass_through_and_tool_calls_returned(
        self, client, valid_api_key, main, monkeypatch
    ):
        captured = {}

        class FakeResp:
            def model_dump(self):
                return {
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "t1", "type": "function",
                                "function": {
                                    "name": "search_applications",
                                    "arguments": "{\"q\": \"x\"}",
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                }

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return FakeResp()

        monkeypatch.setattr(main.litellm, "acompletion", fake_acompletion)

        resp = client.post(
            "/complete", json=self._body(), headers={"X-API-Key": valid_api_key}
        )
        assert resp.status_code == 200
        data = resp.json()

        # infra TRANSPORTED the tools to the model, non-streaming...
        assert captured.get("stream") is False
        assert captured["tools"][0]["function"]["name"] == "search_applications"
        assert captured["tool_choice"] == "auto"
        # ...and returned the model's tool_calls verbatim.
        tc = data["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "search_applications"
