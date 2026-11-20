"""Native Claude protocol and provider failure tests; no network or real credentials."""

import json

import httpx
import pytest
from agent_service.claude import Claude
from agent_service.main import AgentSettings
from blissbiz_common.errors import ApiError

TOOLS = [
    {
        "name": "search_products",
        "description": "Search this shop",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    }
]


def message(text="A grounded answer", stop="end_turn", **usage):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": 20, "output_tokens": 8, **usage},
    }


def provider(handler, key="test-only-key"):
    return Claude(
        key, "https://api.anthropic.com", "claude-haiku-5-5", httpx.MockTransport(handler)
    )


def test_native_tool_protocol_and_history():
    def handler(request):
        assert request.url == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "test-only-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["system"] == "Select a permitted tool"
        assert body["messages"] == [
            {"role": "assistant", "content": "Previous answer"},
            {"role": "user", "content": "blue kurta"},
        ]
        assert body["tools"][0]["input_schema"] == TOOLS[0]["parameters"]
        assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
        assert body["thinking"] == {"type": "disabled"}
        assert "temperature" not in body and "max_completion_tokens" not in body
        data = message(cache_read_input_tokens=4, cache_creation_input_tokens=6)
        data["stop_reason"] = "tool_use"
        data["content"] = [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "search_products",
                "input": {"question": "blue kurta"},
            }
        ]
        return httpx.Response(200, json=data)

    client = provider(handler)
    try:
        result = client.choose_tool(
            "Select a permitted tool",
            "blue kurta",
            TOOLS,
            [{"role": "assistant", "content": "Previous answer"}],
        )
        assert result.name == "search_products" and result.args == {"question": "blue kurta"}
        assert result.prompt_tokens == 30 and result.completion_tokens == 8
    finally:
        client.close()


@pytest.mark.parametrize(
    "data",
    [
        {},
        [],
        {"content": None},
        {"content": [None], "stop_reason": "tool_use"},
        {
            "content": [{"type": "tool_use", "name": "delete_database", "input": {}}],
            "stop_reason": "tool_use",
        },
        {
            "content": [{"type": "tool_use", "name": "search_products", "input": "bad"}],
            "stop_reason": "tool_use",
        },
        {
            "content": [{"type": "tool_use", "name": "search_products", "input": {}}],
            "stop_reason": "max_tokens",
        },
    ],
)
def test_malformed_or_disallowed_tool_call_degrades_safely(data):
    client = provider(lambda request: httpx.Response(200, json=data))
    try:
        assert client.choose_tool("Route", "query", TOOLS).name is None
        assert client.grounded_reply("query", [], "search_products", {}) == (None, {})
    finally:
        client.close()


@pytest.mark.parametrize("status", [401, 403, 429, 500, 529])
def test_provider_failures_leave_local_shopping_answer_available(status):
    client = provider(lambda request: httpx.Response(status, json={"error": "private detail"}))
    try:
        assert client.choose_tool("Route", "query", TOOLS).name is None
        assert client.grounded_reply("query", [], "search_products", {}) == (None, {})
    finally:
        client.close()


def test_missing_key_makes_no_http_request():
    def unexpected(request):
        pytest.fail("No inference request without an Anthropic key")

    client = provider(unexpected, key="")
    try:
        assert client.choose_tool("Route", "query", TOOLS).name is None
        assert client.grounded_reply("query", [], "search_products", {}) == (None, {})
        with pytest.raises(ApiError, match="ANTHROPIC_API_KEY"):
            client.compose("claude-sonnet-5-5", [])
    finally:
        client.close()


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failure_does_not_retry_other_model(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "private detail"})

    client = provider(handler)
    try:
        with pytest.raises(ApiError) as exc:
            client.compose("claude-sonnet-5-5", [{"role": "user", "content": "Help"}])
        assert exc.value.code == "guide_provider_auth" and len(calls) == 1
        assert "private detail" not in str(exc.value)
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["rate_limit", "truncated", "empty", "network"])
def test_guide_falls_back_once_and_reports_all_attempts(failure):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            assert body["model"] == "claude-sonnet-5-5"
            assert body["thinking"] == {"type": "between_tools"}
            assert body["system"] == "Use shop evidence"
            assert all(m["role"] != "system" for m in body["messages"])
            if failure == "rate_limit":
                return httpx.Response(429)
            if failure == "network":
                raise httpx.ConnectError("No connection", request=request)
            return httpx.Response(
                200,
                json=message(text="partial" if failure == "truncated" else "", stop="max_tokens"),
            )
        assert body["model"] == "claude-haiku-5-5"
        return httpx.Response(200, json=message(cache_read_input_tokens=5))

    client = provider(handler)
    try:
        answer, usage = client.compose(
            "claude-sonnet-5-5",
            [
                {"role": "system", "content": "Use shop evidence"},
                {"role": "user", "content": "Help"},
            ],
        )
        assert answer == "A grounded answer" and len(calls) == 2 and usage["llm_calls"] == 2
        assert usage["cached_tokens"] == 5
        assert usage["prompt_tokens"] == (45 if failure in ("empty", "truncated") else 25)
    finally:
        client.close()


def test_rate_limit_preserves_retry_after_without_unbounded_retries():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "45"})

    client = provider(handler)
    try:
        with pytest.raises(ApiError) as exc:
            client.compose("claude-haiku-5-5", [{"role": "user", "content": "Help"}])
        assert exc.value.code == "assistant_busy" and exc.value.headers == {"Retry-After": "45"}
        assert len(calls) == 1
    finally:
        client.close()


def test_only_visible_text_is_returned():
    data = message(cache_read_input_tokens=3)
    data["content"].insert(0, {"type": "thinking", "thinking": "private reasoning"})
    client = provider(lambda request: httpx.Response(200, json=data))
    try:
        answer, usage = client.grounded_reply("blue dress", [], "search_products", {"products": []})
        assert answer == "A grounded answer" and usage["cached_tokens"] == 3
    finally:
        client.close()


def test_old_groq_key_does_not_enable_claude(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "old-test-only-key")
    settings = AgentSettings(_env_file=None)
    assert settings.anthropic_api_key.get_secret_value() == ""
    assert settings.chat_model == "claude-haiku-5-5"
    assert settings.guide_model == "claude-sonnet-5-5"
