from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any


class ModelError(RuntimeError):
    """A model request failed without exposing authorization headers."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=lambda: {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None})
    finish_reason: str | None = None
    latency_ms: float = 0

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            message["tool_calls"] = copy.deepcopy(self.tool_calls)
        return message


class ChatCompletionsModel:
    """An interchangeable HTTP tool-calling backend, using no provider SDK.

    Provider reasoning-message extensions are not retained by ModelResponse.
    DeepSeek tool tests must explicitly set extra_body.thinking.type=disabled;
    thinking mode requires reasoning_content round-tripping before it can be
    used safely for multiple tool turns. See api-docs.deepseek.com/guides/thinking_mode/.
    """

    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 timeout_s: float = 90, max_output_tokens: int = 4096,
                 temperature: float | None = None, max_retries: int = 0,
                 extra_body: dict[str, Any] | None = None,
                 max_request_bytes: int = 1_000_000, max_response_bytes: int = 2_000_000):
        if not base_url or not model:
            raise ValueError("Model base_url and model are required")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Model timeout_s must be a finite positive number")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise ValueError("Model max_output_tokens must be a positive integer")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries != 0:
            raise ValueError("Model max_retries must be 0; the agent accounts for each retry separately")
        for name, value in (("max_request_bytes", max_request_bytes), ("max_response_bytes", max_response_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 16_000_000:
                raise ValueError(f"Model {name} must be an integer between 1024 and 16000000")
        self.endpoint = base_url.rstrip("/")
        if not self.endpoint.endswith("/chat/completions"):
            self.endpoint += "/chat/completions"
        self.model, self.api_key = model, api_key
        self.timeout_s, self.max_output_tokens = timeout_s, max_output_tokens
        self.temperature, self.max_retries = temperature, max_retries
        self.max_request_bytes, self.max_response_bytes = max_request_bytes, max_response_bytes
        if extra_body is not None and not isinstance(extra_body, dict):
            raise ValueError("Model extra_body must be an object")
        self.extra_body = copy.deepcopy(extra_body or {})
        if set(self.extra_body) & {"messages", "tools", "model", "max_tokens", "max_completion_tokens", "stream"}:
            raise ValueError("extra_body cannot override model messages, tools, output limits, or streaming mode")

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                       tool_choice: dict[str, Any] | None = None) -> ModelResponse:
        import httpx

        if tool_choice is not None and not isinstance(tool_choice, dict):
            raise ValueError("Model tool_choice must be an object or None")
        encoded = self._request(messages, tools, tool_choice)
        if len(encoded) > self.max_request_bytes:
            raise ModelError("Model request exceeds the configured byte limit")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        started = time.monotonic()
        # One admission is exactly one HTTP attempt. Retrying inside this client
        # hides potentially billed requests from the team's durable budget.
        try:
            async with asyncio.timeout(self.timeout_s):
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    async with client.stream("POST", self.endpoint, content=encoded, headers=headers) as response:
                        if response.is_error:
                            raise ModelError(f"Model backend returned HTTP {response.status_code}",
                                             retryable=response.status_code in {408, 409, 425, 429} or response.status_code >= 500)
                        payload = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(payload) + len(chunk) > self.max_response_bytes:
                                raise ModelError("Model response exceeds the configured byte limit")
                            payload.extend(chunk)
                        data = json.loads(payload)
        except (TimeoutError, httpx.TransportError):
            raise ModelError("Model request failed or exceeded its total deadline", retryable=True) from None
        except (ValueError, UnicodeError):
            # A malformed/truncated body is a provider/protocol fault, not a bad
            # request. Treat it as retryable; the agent still bounds attempts.
            raise ModelError("Model backend returned invalid JSON", retryable=True) from None
        try:
            choice = data["choices"][0]
            message = choice["message"]
            calls = _normalize_calls(message.get("tool_calls", []))
            usage = data.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("Model usage must be an object")
            normalized = {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
            normalized.update({key: value for key, value in usage.items() if key not in normalized})
            content = message.get("content") or ""
            if not isinstance(content, str):
                raise ValueError("Only text content is supported by this backend")
            return ModelResponse(content, calls, normalized,
                                 choice.get("finish_reason"), (time.monotonic() - started) * 1000)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("Model backend returned an invalid tool-calling response", retryable=True) from None

    def _request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 tool_choice: dict[str, Any] | None = None) -> bytes:
        body = {**self.extra_body, "model": self.model, "messages": messages, "tools": tools,
                "max_tokens": self.max_output_tokens}
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if self.temperature is not None:
            body["temperature"] = self.temperature
        return json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def request_bytes(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        return len(self._request(messages, tools))


def _normalize_calls(calls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    # A terminal assistant message may explicitly use JSON null for no calls.
    # Do not coerce other falsey values: malformed strings/maps/bools are errors.
    if calls is None:
        return []
    if not isinstance(calls, list) or len(calls) > 32:
        raise ValueError("At most 32 tool calls are allowed per response")
    result = []
    identifiers: set[str] = set()
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise ValueError("Invalid tool call object")
        identifier = call["id"]
        function = call["function"]
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200 or identifier in identifiers:
            raise ValueError("Missing or duplicated tool call ID")
        identifiers.add(identifier)
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if (not isinstance(arguments, str) or len(arguments.encode()) > 1_000_000 or
                not isinstance(function["name"], str) or not 1 <= len(function["name"]) <= 100):
            raise ValueError("Invalid tool call")
        result.append({"id": identifier, "type": "function", "function": {
            "name": function["name"], "arguments": arguments}})
    return result


class FakeModel:
    """Offline protocol fixture. No Minecraft strategy or skills are built in."""

    def __init__(self, script: list[dict[str, Any] | ModelResponse] | None = None,
                 max_retained_calls: int = 128):
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []
        if isinstance(max_retained_calls, bool) or not isinstance(max_retained_calls, int) or max_retained_calls < 1:
            raise ValueError("Fake model call history must be a positive bounded length")
        self.max_retained_calls = max_retained_calls
        self.total_calls = 0

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                       tool_choice: dict[str, Any] | None = None) -> ModelResponse:
        self.calls.append(copy.deepcopy({"messages": messages, "tools": tools, "tool_choice": tool_choice}))
        self.total_calls += 1
        del self.calls[:-self.max_retained_calls]
        if not self.script:
            return ModelResponse(text="No further scripted model response.", finish_reason="stop",
                                 usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        response = self.script.pop(0)
        if isinstance(response, ModelResponse):
            return copy.deepcopy(response)
        return ModelResponse(text=response.get("text", response.get("content", "")),
                             tool_calls=_normalize_calls(response.get("tool_calls", [])),
                             usage=response.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
                             finish_reason=response.get("finish_reason", "stop"))


def build_model(config: dict[str, Any]) -> ChatCompletionsModel | FakeModel:
    kind = config.get("kind", config.get("type", config.get("backend", "chat_completions")))
    if kind == "fake":
        return FakeModel(config.get("script"))
    if kind not in {"chat_completions", "openai_compatible"}:
        raise ValueError(f"Unsupported model backend: {kind}")
    key = config.get("api_key")
    if not key and config.get("api_key_env"):
        variable = config["api_key_env"]
        if not isinstance(variable, str):
            raise ValueError("Model credential environment variable must be a name")
        key = os.environ.get(variable)
        if not key:
            raise ValueError("The configured model credential environment variable is unset")
    return ChatCompletionsModel(base_url=config["base_url"], model=config["model"], api_key=key,
                                timeout_s=config.get("timeout_s", 90),
                                max_output_tokens=config.get("max_output_tokens", 4096),
                                temperature=config.get("temperature"),
                                max_retries=config.get("max_retries", 0), extra_body=config.get("extra_body"),
                                max_request_bytes=config.get("max_request_bytes", 1_000_000),
                                max_response_bytes=config.get("max_response_bytes", 2_000_000))
