"""Claude Messages API over HTTPS. Browser voice arrives as text; no audio is sent.

ANTHROPIC_API_KEY enables inference. Local tools remain usable without a provider key.
"""

import json
import logging
from dataclasses import dataclass, field

import httpx
from blissbiz_common.errors import ApiError

logger = logging.getLogger(__name__)


@dataclass
class ToolChoice:
    name: str | None
    args: dict = field(default_factory=dict)
    content: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class Claude:
    def __init__(self, api_key: str, base_url: str, chat_model: str, transport: httpx.BaseTransport | None = None):
        self.enabled = bool(api_key.strip())
        self.chat_model = chat_model
        self.http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(30, connect=5),
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"} if self.enabled else {},
        )

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def _body(model: str, messages: list[dict], max_tokens: int) -> dict:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [m for m in messages if m["role"] != "system"],
        }
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if system:
            body["system"] = system
        # Current models reject temperature; their lowest thinking settings differ.
        if model == "claude-sonnet-5-5":
            body["thinking"] = {"type": "between_tools"}
        elif model == "claude-haiku-5-5":
            body["thinking"] = {"type": "disabled"}
        return body

    @staticmethod
    def _usage(data: dict) -> dict:
        usage = data.get("usage") or {}
        cached = usage.get("cache_read_input_tokens", 0)
        return {
            "llm_calls": 1,
            "prompt_tokens": usage.get("input_tokens", 0) + cached + usage.get("cache_creation_input_tokens", 0),
            "cached_tokens": cached,
            "completion_tokens": usage.get("output_tokens", 0),
        }

    @staticmethod
    def _answer(data: dict) -> str:
        if data.get("stop_reason") not in ("end_turn", "stop_sequence"):
            raise ValueError("Incomplete reply")
        answer = "\n".join(b["text"] for b in data["content"] if b.get("type") == "text").strip()
        if not answer:
            raise ValueError("Empty reply")
        return answer

    def choose_tool(
        self, system: str, question: str, tools: list[dict], history: list[dict[str, str]] | None = None
    ) -> ToolChoice:
        if not self.enabled:
            return ToolChoice(None)
        body = self._body(
            self.chat_model,
            [
                {"role": "system", "content": system},
                *(history or [])[-10:],
                {"role": "user", "content": question},
            ],
            1024,
        )
        if tools:
            body["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
            ]
            body["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        try:
            response = self.http.post("/v1/messages", json=body)
            if response.status_code >= 400:
                logger.warning(
                    "Claude tool selection failed: model=%s status=%s", self.chat_model, response.status_code
                )
                return ToolChoice(None)
            data = response.json()
            usage = self._usage(data)
            if data.get("stop_reason") not in ("tool_use", "end_turn"):
                return ToolChoice(None)
            calls = [b for b in data["content"] if b.get("type") == "tool_use"]
            name, args = None, {}
            if calls:
                call = calls[0]
                allowed = {t["name"] for t in tools}
                if call["name"] not in allowed or not isinstance(call["input"], dict):
                    return ToolChoice(None)
                name, args = call["name"], call["input"]
            content = "\n".join(b["text"] for b in data["content"] if b.get("type") == "text").strip() or None
            return ToolChoice(name, args, content, usage["prompt_tokens"], usage["completion_tokens"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            return ToolChoice(None)

    def grounded_reply(
        self, question: str, history: list[dict[str, str]], tool_name: str, result: dict
    ) -> tuple[str | None, dict]:
        """Phrase only facts returned by the existing tenant-scoped shopping tool."""
        if not self.enabled:
            return None, {}
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a warm, concise shopping assistant. Answer the user's latest question "
                    "using only the supplied SHOP DATA. Use recent conversation to understand follow-up "
                    "wording, but never invent products, prices, sizes, colours, availability, policies, "
                    "orders or actions. Do not mention databases, tools or SHOP DATA. If several products "
                    "are possible, explain that clearly or ask a short clarifying question. Keep the answer "
                    "to one short paragraph unless a compact list is genuinely clearer."
                    " Use plain text, never HTML; product cards already provide navigation."
                    " An empty filtered search means no matching products; it does not prove that the "
                    "shop carries none of that category or that the whole category is out of stock."
                ),
            },
            *history[-8:],
            {"role": "user", "content": question},
            {
                "role": "user",
                "content": "SHOP DATA\n" + json.dumps({"tool": tool_name, **result}, default=str),
            },
        ]
        body = self._body(self.chat_model, messages, 1024)
        try:
            response = self.http.post("/v1/messages", json=body)
            if response.status_code >= 400:
                logger.warning(
                    "Claude shopper response failed: model=%s status=%s", self.chat_model, response.status_code
                )
                return None, {}
            data = response.json()
            return self._answer(data), self._usage(data)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            return None, {}

    def compose(self, model: str, messages: list[dict[str, str]]) -> tuple[str, dict]:
        """Generate Guide text, with at most one fallback to the configured chat model."""
        if not self.enabled:
            raise ApiError(503, "guide_unconfigured", "The Business Guide needs ANTHROPIC_API_KEY on the server.")
        candidates = [model] + ([self.chat_model] if self.chat_model != model else [])
        last_error = None
        totals = {"llm_calls": 0, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
        for selected in candidates:
            totals["llm_calls"] += 1
            try:
                response = self.http.post("/v1/messages", json=self._body(selected, messages, 2400))
            except httpx.HTTPError:
                last_error = ApiError(503, "assistant_unavailable", "The Business Guide could not connect. Try again.")
                continue
            if response.status_code in (401, 403):
                raise ApiError(
                    503, "guide_provider_auth", "Claude rejected the Business Guide credentials or permissions."
                )
            if response.status_code >= 400:
                logger.warning("Claude Business Guide failed: model=%s status=%s", selected, response.status_code)
                if response.status_code == 429:
                    last_error = ApiError(
                        503,
                        "assistant_busy",
                        "The assistant is busy right now. Try again in a minute.",
                        headers={"Retry-After": response.headers.get("retry-after", "30")},
                    )
                else:
                    last_error = ApiError(
                        503, "assistant_unavailable", "The Business Guide service did not answer. Try again."
                    )
                continue
            try:
                data = response.json()
                usage = self._usage(data)
                for key in ("prompt_tokens", "cached_tokens", "completion_tokens"):
                    totals[key] += usage[key]
                answer = self._answer(data)
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                last_error = ApiError(
                    503, "assistant_unavailable", "The Business Guide returned an incomplete answer. Try again."
                )
                continue
            return answer, totals
        raise last_error or ApiError(503, "assistant_unavailable", "The Business Guide is temporarily unavailable.")
