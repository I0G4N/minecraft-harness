import asyncio
import json
import os
import unittest
from unittest.mock import patch

from minecraft_harness.models import ChatCompletionsModel, FakeModel, ModelError, build_model


class ModelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_preserves_tool_ids_and_unknown_usage(self):
        model = FakeModel([{"tool_calls": [{"id": "call-1", "function": {
            "name": "workspace_write", "arguments": {"path": "a", "content": "x"}}}]}])
        response = await model.complete([], [])
        self.assertEqual(response.tool_calls[0]["id"], "call-1")
        self.assertEqual(json.loads(response.tool_calls[0]["function"]["arguments"])["path"], "a")
        self.assertEqual(response.as_message()["tool_calls"][0]["id"], "call-1")

    async def test_tool_choice_is_optional_and_passed_through(self):
        import httpx
        bodies = []

        async def reply(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                          "finish_reason": "stop"}]})

        original_client = httpx.AsyncClient
        patch_client = lambda **kw: original_client(transport=httpx.MockTransport(reply), **kw)
        with patch("httpx.AsyncClient", patch_client):
            model = ChatCompletionsModel("https://example.test/v1", "custom", "key")
            await model.complete([{"role": "user", "content": "hi"}], [])
            forced = {"type": "function", "function": {"name": "observer_report"}}
            await model.complete([{"role": "user", "content": "hi"}], [], tool_choice=forced)
            with self.assertRaises(ValueError):
                await model.complete([{"role": "user", "content": "hi"}], [], tool_choice="observer_report")
        self.assertNotIn("tool_choice", bodies[0])
        self.assertEqual(bodies[1]["tool_choice"], forced)

    async def test_http_protocol_and_unavailable_usage(self):
        import httpx
        requests = []

        async def reply(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "observed", "tool_calls": [
                {"id": "tool-7", "function": {"name": "events_read", "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}]})

        original_client = httpx.AsyncClient
        with patch("httpx.AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(reply), **kw)):
            response = await ChatCompletionsModel("https://example.test/v1", "custom", "hidden").complete([], [])
        self.assertEqual(requests[0].url.path, "/v1/chat/completions")
        self.assertEqual(requests[0].headers["authorization"], "Bearer hidden")
        self.assertEqual(response.tool_calls[0]["id"], "tool-7")
        self.assertIsNone(response.usage["total_tokens"])

    async def test_deepseek_non_thinking_tool_roundtrip_and_null_terminal_calls(self):
        import httpx
        requests = []
        provider_usage = {'prompt_tokens': 100, 'completion_tokens': 12, 'total_tokens': 112,
                          'prompt_cache_hit_tokens': 40, 'prompt_cache_miss_tokens': 60}

        async def reply(request):
            body = json.loads(request.content); requests.append(body)
            self.assertEqual(request.url.path, '/chat/completions')
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            self.assertEqual(body['max_tokens'], 512)
            if len(requests) == 1:
                message = {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'call_deepseek_1', 'type': 'function', 'function': {'name': 'events_read', 'arguments': '{}'}}]}
                finish = 'tool_calls'
            else:
                self.assertEqual(body['messages'][-1]['tool_call_id'], 'call_deepseek_1')
                self.assertEqual(body['messages'][-2]['tool_calls'][0]['id'], 'call_deepseek_1')
                message = {'role': 'assistant', 'content': 'Observed the diagnostic environment.', 'tool_calls': None}
                finish = 'stop'
            return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': finish}], 'usage': provider_usage})

        tools = [{'type': 'function', 'function': {'name': 'events_read', 'parameters': {'type': 'object', 'properties': {}}}}]
        messages = [{'role': 'user', 'content': 'Read the available events.'}]
        original_client = httpx.AsyncClient
        with patch('httpx.AsyncClient', lambda **kw: original_client(transport=httpx.MockTransport(reply), **kw)):
            model = ChatCompletionsModel('https://api.deepseek.com', 'deepseek-flash',
                                        max_output_tokens=512, extra_body={'thinking': {'type': 'disabled'}})
            first = await model.complete(messages, tools)
            messages += [first.as_message(), {'role': 'tool', 'tool_call_id': first.tool_calls[0]['id'], 'content': '{"events":[]}'}]
            final = await model.complete(messages, tools)
        self.assertEqual(len(requests), 2)
        self.assertEqual(final.finish_reason, 'stop')
        self.assertEqual(final.tool_calls, [])
        self.assertNotIn('tool_calls', final.as_message())
        self.assertEqual(final.usage['prompt_cache_hit_tokens'], 40)
        self.assertEqual(final.usage['prompt_cache_miss_tokens'], 60)

    async def test_http_errors_do_not_expose_response_secrets(self):
        import httpx

        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(401, text="secret-api-key"))
        with patch("httpx.AsyncClient", lambda **kw: original_client(transport=transport, **kw)):
            with self.assertRaises(ModelError) as caught:
                await ChatCompletionsModel("https://example.test/v1", "custom", "secret-api-key").complete([], [])
        self.assertNotIn("secret-api-key", str(caught.exception))

    async def test_transient_error_makes_exactly_one_billable_attempt(self):
        import httpx
        count = 0

        async def reply(request):
            nonlocal count
            count += 1
            return httpx.Response(503)

        original_client = httpx.AsyncClient
        with patch("httpx.AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(reply), **kw)):
            with self.assertRaises(ModelError) as caught:
                await ChatCompletionsModel("https://example.test/v1", "custom").complete([], [])
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(count, 1)

    async def test_malformed_provider_responses_are_retryable(self):
        import httpx

        for payload in (b"not-json", b'{"choices":[]}'):
            original_client = httpx.AsyncClient
            transport = httpx.MockTransport(lambda request, payload=payload: httpx.Response(200, content=payload))
            with patch("httpx.AsyncClient", lambda **kw: original_client(transport=transport, **kw)):
                with self.assertRaises(ModelError) as caught:
                    await ChatCompletionsModel("https://example.test/v1", "custom").complete([], [])
            self.assertTrue(caught.exception.retryable, payload)

    async def test_dripping_response_is_bounded_by_total_deadline(self):
        import httpx

        class Drip(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    await asyncio.sleep(0.005)
                    yield b" "

        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip()))
        with patch("httpx.AsyncClient", lambda **kw: original_client(transport=transport, **kw)):
            with self.assertRaises(ModelError) as caught:
                await asyncio.wait_for(ChatCompletionsModel("https://example.test/v1", "custom", timeout_s=0.03).complete([], []), 0.5)
        self.assertTrue(caught.exception.retryable)

    async def test_request_and_response_byte_limits(self):
        import httpx
        original_client = httpx.AsyncClient
        requests = []

        async def reply(request):
            requests.append(request)
            return httpx.Response(200, content=b"x" * 2000)

        with patch("httpx.AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(reply), **kw)):
            with self.assertRaisesRegex(ModelError, "request exceeds"):
                await ChatCompletionsModel("https://example.test/v1", "custom", max_request_bytes=1024).complete(
                    [{"role": "user", "content": "字" * 1000}], [])
            self.assertFalse(requests)
            with self.assertRaisesRegex(ModelError, "response exceeds"):
                await ChatCompletionsModel("https://example.test/v1", "custom", max_response_bytes=1024).complete([], [])
        self.assertEqual(len(requests), 1)

    async def test_invalid_response_shapes_are_sanitized(self):
        import httpx
        original_client = httpx.AsyncClient
        for message in ({"tool_calls": [None]}, {"tool_calls": False}, {"tool_calls": ""},
                        {"tool_calls": {}}, {"tool_calls": 0}, {"tool_calls": [{"id": "a", "function": 1}]},
                        {"tool_calls": [{}] * 33}, {"content": ["not text"]}):
            with self.subTest(message=message):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": message}]}))
                with patch("httpx.AsyncClient", lambda **kw: original_client(transport=transport, **kw)):
                    with self.assertRaises(ModelError):
                        await ChatCompletionsModel("https://example.test/v1", "custom").complete([], [])

    def test_factory_accepts_config_kind(self):
        self.assertIsInstance(build_model({"kind": "fake"}), FakeModel)

    async def test_fake_model_diagnostic_history_is_bounded_for_soaks(self):
        model = FakeModel(max_retained_calls=8)
        for index in range(100):
            await model.complete([{"role": "user", "content": str(index)}], [])
        self.assertEqual(model.total_calls, 100)
        self.assertEqual(len(model.calls), 8)
        self.assertEqual(model.calls[0]["messages"][0]["content"], "92")

    def test_extra_body_cannot_override_tool_protocol_or_output_budget(self):
        for field, value in (("messages", []), ("tools", []), ("model", "different"),
                             ("max_tokens", 999999), ("max_completion_tokens", 999999), ("stream", True)):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    ChatCompletionsModel("https://example.test/v1", "custom", extra_body={field: value})
        caller_owned = {"top_p": 0.9}
        model = ChatCompletionsModel("https://example.test/v1", "custom", extra_body=caller_owned)
        caller_owned["max_tokens"] = 999999
        self.assertNotIn("max_tokens", model.extra_body)

    def test_model_limits_are_validated_before_http_requests(self):
        invalid = {
            "timeout_s": [0, -1, float("nan"), float("inf"), True, "90", None],
            "max_output_tokens": [0, -1, 1.5, True, "4096", None],
            "max_retries": [-1, 1, 2, 1.5, True, "2", None],
            "max_request_bytes": [0, 1, True, 16_000_001],
            "max_response_bytes": [0, 1, True, 16_000_001],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        ChatCompletionsModel("https://example.test/v1", "custom", **{field: value})

    def test_missing_credential_error_does_not_echo_configured_name_or_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as caught:
                build_model({"kind": "chat_completions", "base_url": "https://example.test/v1",
                             "model": "custom", "api_key_env": "accidentally-pasted-secret"})
        self.assertNotIn("accidentally-pasted-secret", str(caught.exception))

    async def test_resolved_environment_credential_stays_out_of_request_body(self):
        import httpx
        original_client = httpx.AsyncClient
        requests = []

        async def reply(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

        with patch.dict(os.environ, {"HARNESS_TEST_API_KEY": "resolved-private-value"}), \
                patch("httpx.AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(reply), **kw)):
            model = build_model({"kind": "chat_completions", "base_url": "https://example.test/v1",
                                 "model": "custom", "api_key_env": "HARNESS_TEST_API_KEY", "max_output_tokens": 123})
            await model.complete([], [])
        self.assertEqual(requests[0].headers["authorization"], "Bearer resolved-private-value")
        self.assertNotIn(b"resolved-private-value", requests[0].content)
        self.assertEqual(json.loads(requests[0].content)["max_tokens"], 123)
