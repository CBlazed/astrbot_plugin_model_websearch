"""离线单测：不访问网络，用 fake provider/context/client 覆盖解析、缓存、限流、配置错误。

运行（Windows 示例）：
    PYTHONPATH="<CORE>;<workspace>/plugins" \
    <INSTANCE_VENV>/Scripts/python.exe tests/test_offline.py
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

try:  # 作为 AstrBot 插件包导入（目录名即包名）
    from astrbot_plugin_model_websearch.client import (
        SEARCH_TOOL_NAME,
        NativeSearchClient,
        SearchError,
        SearchOutcome,
        TurnSearchState,
        build_turn_limit_text,
        get_turn_state,
        is_conflicting_search_tool,
    )
except ModuleNotFoundError:  # 直接从仓库根目录跑源码（模块名不带包前缀）
    from client import (
        SEARCH_TOOL_NAME,
        NativeSearchClient,
        SearchError,
        SearchOutcome,
        TurnSearchState,
        build_turn_limit_text,
        get_turn_state,
        is_conflicting_search_tool,
    )


class FakeResponses:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.requests.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response: Any) -> None:
        self.responses = FakeResponses(response)


class FakeProvider:
    def __init__(self, response: Any, provider_id: str = "<provider>/<model>") -> None:
        self.provider_config = {
            "id": provider_id,
            "type": "openai_chat_completion",
            "api_base": "http://127.0.0.1:<端口>/v1",
            "model": "<model>",
        }
        self.client = FakeClient(response)

    def get_model(self) -> str:
        return "<model>"


class NoResponsesProvider:
    provider_config = {"id": "deepseek/<model>", "type": "openai_chat_completion"}

    def get_model(self) -> str:
        return "deepseek-flash"


class FakeContext:
    def __init__(self, provider: Any, providers: dict[str, Any] | None = None) -> None:
        self.provider = provider
        self.providers = providers or {}

    async def get_using_provider_async(self, umo: str | None = None) -> Any:
        return self.provider

    def get_provider_by_id(self, provider_id: str) -> Any:
        return self.providers.get(provider_id)


def make_response(text: str, queries: list[str], annotations: list[dict[str, str]], sources: list[dict[str, str]] | None = None) -> Any:
    return {
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "type": "search",
                    "queries": queries,
                    "sources": sources or [],
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [
                            {"type": "url_citation", "title": a.get("title", ""), "url": a["url"]}
                            for a in annotations
                        ],
                    }
                ],
            },
        ],
        "usage": {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
    }


GOOD_JSON = json.dumps(
    {
        "results": [
            {"title": "AstrBot 文档", "url": "https://docs.astrbot.app/x", "snippet": "一句摘要"},
            {"title": "第二篇", "url": "https://example.com/b", "snippet": "另一句摘要"},
        ],
        "answer": "这是基于检索的简短回答。",
    },
    ensure_ascii=False,
)


class ParseTests(unittest.TestCase):
    def test_parse_structured_json(self) -> None:
        client = NativeSearchClient({})
        response = make_response(GOOD_JSON, ["q1", "q2"], [{"title": "AstrBot 文档", "url": "https://docs.astrbot.app/x"}])
        outcome = client._parse_response(response, "<provider>/<model>", "<model>", 5)
        self.assertEqual(len(outcome.results), 2)
        self.assertEqual(outcome.results[0]["title"], "AstrBot 文档")
        self.assertTrue(outcome.results[0]["index"].endswith(".1"))
        self.assertEqual(outcome.answer, "这是基于检索的简短回答。")
        self.assertEqual(outcome.queries, ["q1", "q2"])
        self.assertTrue(outcome.sources)

    def test_parse_fallback_to_citations(self) -> None:
        client = NativeSearchClient({})
        response = make_response(
            "纯文本回答，没有 JSON。",
            ["q1"],
            [{"title": "来源一", "url": "https://a.example/1"}, {"title": "来源二", "url": "https://b.example/2"}],
        )
        outcome = client._parse_response(response, "p", "m", 5)
        self.assertEqual([r["url"] for r in outcome.results], ["https://a.example/1", "https://b.example/2"])
        self.assertEqual(outcome.answer, "纯文本回答，没有 JSON。")

    def test_no_actual_search_raises(self) -> None:
        client = NativeSearchClient({})
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": GOOD_JSON, "annotations": []}]}]}
        with self.assertRaises(SearchError):
            client._parse_response(response, "p", "m", 5)

    def test_limit_results(self) -> None:
        client = NativeSearchClient({})
        rows = [{"title": f"t{i}", "url": f"https://e/{i}", "snippet": "s"} for i in range(8)]
        response = make_response(json.dumps({"results": rows, "answer": "a"}, ensure_ascii=False), ["q"], [])
        outcome = client._parse_response(response, "p", "m", 3)
        self.assertEqual(len(outcome.results), 3)

    def test_sources_fallback_to_results(self) -> None:
        client = NativeSearchClient({})
        response = make_response(GOOD_JSON, ["q"], [])
        outcome = client._parse_response(response, "p", "m", 5)
        self.assertEqual([s["url"] for s in outcome.sources], [r["url"] for r in outcome.results])


class SearchFlowTests(unittest.TestCase):
    def test_cache_hit_skips_second_model_call(self) -> None:
        response = make_response(GOOD_JSON, ["q"], [])
        provider = FakeProvider(response)
        context = FakeContext(provider)
        client = NativeSearchClient({"cache_ttl_seconds": 60, "rate_limit_per_minute": 6})

        async def run() -> tuple[Any, Any]:
            first = await client.search(context, "umo-1", "AstrBot 是什么")
            second = await client.search(context, "umo-1", "AstrBot 是什么")
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first.results, second.results)
        self.assertEqual(provider.client.responses.calls, 1)

    def test_rate_limit(self) -> None:
        response = make_response(GOOD_JSON, ["q"], [])
        provider = FakeProvider(response)
        context = FakeContext(provider)
        client = NativeSearchClient({"cache_ttl_seconds": 0, "rate_limit_per_minute": 2})

        async def run() -> None:
            for i in range(3):
                await client.search(context, "umo-x", f"查询 {i}")

        with self.assertRaises(SearchError) as ctx:
            asyncio.run(run())
        self.assertIn("频率超限", str(ctx.exception))

    def test_fixed_mode_empty_provider_id(self) -> None:
        provider = FakeProvider(make_response(GOOD_JSON, ["q"], []))
        context = FakeContext(provider)
        client = NativeSearchClient({"provider_mode": "fixed", "provider_id": "", "cache_ttl_seconds": 0})

        async def run() -> None:
            await client.search(context, "umo", "q")

        with self.assertRaises(SearchError) as ctx:
            asyncio.run(run())
        self.assertIn("provider_id 为空", str(ctx.exception))
        self.assertEqual(provider.client.responses.calls, 0)

    def test_fixed_mode_provider_not_found(self) -> None:
        provider = FakeProvider(make_response(GOOD_JSON, ["q"], []))
        context = FakeContext(provider, providers={})
        client = NativeSearchClient({"provider_mode": "fixed", "provider_id": "<provider>/not-exist", "cache_ttl_seconds": 0})

        async def run() -> None:
            await client.search(context, "umo", "q")

        with self.assertRaises(SearchError) as ctx:
            asyncio.run(run())
        self.assertIn("不存在", str(ctx.exception))

    def test_fixed_mode_uses_configured_provider(self) -> None:
        fixed_provider = FakeProvider(make_response(GOOD_JSON, ["q"], []), provider_id="<provider>/fixed")
        context = FakeContext(FakeProvider(make_response(GOOD_JSON, ["q"], [])), providers={"<provider>/fixed": fixed_provider})
        client = NativeSearchClient({"provider_mode": "fixed", "provider_id": "<provider>/fixed", "cache_ttl_seconds": 0})

        async def run() -> Any:
            return await client.search(context, "umo", "q")

        outcome = asyncio.run(run())
        self.assertEqual(outcome.provider_id, "<provider>/fixed")
        self.assertEqual(fixed_provider.client.responses.calls, 1)

    def test_profile_override_provider_takes_precedence(self) -> None:
        override_provider = FakeProvider(make_response(GOOD_JSON, ["q"], []), provider_id="<provider>/<model-2>")
        session_provider = FakeProvider(make_response(GOOD_JSON, ["q"], []))
        context = FakeContext(session_provider, providers={"<provider>/<model-2>": override_provider})
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        async def run() -> Any:
            return await client.search(
                context,
                "umo",
                "q",
                search_provider_id="<provider>/<model-2>",
            )

        outcome = asyncio.run(run())
        self.assertEqual(outcome.provider_id, "<provider>/<model-2>")
        self.assertEqual(override_provider.client.responses.calls, 1)
        self.assertEqual(session_provider.client.responses.calls, 0)

    def test_profile_override_missing_provider_errors(self) -> None:
        context = FakeContext(FakeProvider(make_response(GOOD_JSON, ["q"], [])))
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        async def run() -> None:
            await client.search(context, "umo", "q", search_provider_id="<provider>/nope")

        with self.assertRaises(SearchError) as ctx:
            asyncio.run(run())
        self.assertIn("不存在", str(ctx.exception))

    def test_provider_without_responses_api(self) -> None:
        client = NativeSearchClient({})

        async def run() -> None:
            await client.search(FakeContext(NoResponsesProvider()), "umo", "q")

        with self.assertRaises(SearchError) as ctx:
            asyncio.run(run())
        self.assertIn("Responses API", str(ctx.exception))

    def test_request_shape(self) -> None:
        response = make_response(GOOD_JSON, ["q"], [])
        provider = FakeProvider(response)
        context = FakeContext(provider)
        client = NativeSearchClient({"search_context_size": "medium", "max_output_tokens": 600, "cache_ttl_seconds": 0})

        async def run() -> None:
            await client.search(context, "umo", "查询", max_results=3)

        asyncio.run(run())
        request = provider.client.responses.requests[0]
        self.assertEqual(request["tools"], [{"type": "web_search", "search_context_size": "medium"}])
        self.assertEqual(request["max_output_tokens"], 600)
        self.assertFalse(request["store"])
        self.assertEqual(request["model"], "<model>")

    def test_tool_text_is_json(self) -> None:
        response = make_response(GOOD_JSON, ["q"], [])
        provider = FakeProvider(response)
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        async def run() -> str:
            outcome = await client.search(FakeContext(provider), "umo", "q")
            return outcome.to_tool_text()

        payload = json.loads(asyncio.run(run()))
        self.assertIn("results", payload)
        self.assertIn("answer", payload)
        self.assertIn("provider", payload)


class ConflictTests(unittest.TestCase):
    def test_builtin_and_anysearch_conflicts(self) -> None:
        self.assertTrue(is_conflicting_search_tool("web_search_tavily"))
        self.assertTrue(is_conflicting_search_tool("web_search_model_native_v2"))
        self.assertTrue(is_conflicting_search_tool("anysearch_search"))
        self.assertTrue(is_conflicting_search_tool("deepseek_fetch_web_page"))
        self.assertFalse(is_conflicting_search_tool(SEARCH_TOOL_NAME))
        self.assertFalse(is_conflicting_search_tool("weather"))

    def test_extra_conflict_names(self) -> None:
        client = NativeSearchClient({"extra_conflict_tool_names": "my_search, other_search"})
        self.assertTrue(client.is_conflicting_tool("my_search"))
        self.assertFalse(client.is_conflicting_tool("unrelated"))


class ConfigClampTests(unittest.TestCase):
    def test_values_are_clamped(self) -> None:
        client = NativeSearchClient(
            {
                "max_results": 99,
                "search_context_size": "weird",
                "timeout_seconds": 1,
                "cache_ttl_seconds": -5,
                "rate_limit_per_minute": 999,
                "conflict_policy": "nonsense",
            }
        )
        self.assertEqual(client.max_results, 10)
        self.assertEqual(client.search_context_size, "low")
        self.assertEqual(client.timeout_seconds, 5)
        self.assertEqual(client.cache_ttl_seconds, 0)
        self.assertEqual(client.rate_limit_per_minute, 120)
        self.assertEqual(client.conflict_policy, "prefer_self")

    def test_speed_defaults(self) -> None:
        client = NativeSearchClient({})
        self.assertEqual(client.max_results, 10)
        self.assertEqual(client.max_output_tokens, 400)
        self.assertEqual(client.timeout_seconds, 45)
        self.assertEqual(client.cache_ttl_seconds, 300)
        self.assertEqual(client.max_calls_per_turn, 2)
        self.assertFalse(client.include_answer)

    def test_instructions_without_answer(self) -> None:
        text = NativeSearchClient({})._instructions(10, False)
        self.assertIn('"answer":""', text)
        self.assertIn("不要写总结", text)
        self.assertNotIn("不超过200字", text)

    def test_instructions_with_answer(self) -> None:
        text = NativeSearchClient({})._instructions(10, True)
        self.assertIn("不超过200字", text)


class TurnLimitTests(unittest.TestCase):
    def test_state_attached_to_event(self) -> None:
        class FakeEvent:
            pass

        event = FakeEvent()
        state = get_turn_state(event)
        self.assertIsInstance(state, TurnSearchState)
        self.assertIs(get_turn_state(event), state)

    def test_no_limit_when_zero(self) -> None:
        state = TurnSearchState(calls=5, last=None)
        self.assertIsNone(build_turn_limit_text(state, 0, SEARCH_TOOL_NAME))

    def test_limit_not_reached(self) -> None:
        state = TurnSearchState(calls=1, last=None)
        self.assertIsNone(build_turn_limit_text(state, 2, SEARCH_TOOL_NAME))

    def test_limit_reached_reuses_last_result(self) -> None:
        outcome = SearchOutcome(
            results=[{"title": "t", "url": "https://e/1", "snippet": "s", "index": "abcd.1"}],
            answer="",
            queries=["q"],
            sources=[],
            provider_id="p",
            model="m",
            elapsed_ms=100,
        )
        state = TurnSearchState(calls=2, last=outcome)
        text = build_turn_limit_text(state, 2, SEARCH_TOOL_NAME)
        self.assertIsNotNone(text)
        payload = json.loads(text or "{}")
        self.assertEqual(payload["results"][0]["url"], "https://e/1")
        self.assertIn("不要再次调用", payload["notice"])
        self.assertIn(SEARCH_TOOL_NAME, payload["notice"])

    def test_limit_reached_without_result(self) -> None:
        state = TurnSearchState(calls=2, last=None)
        text = build_turn_limit_text(state, 2, SEARCH_TOOL_NAME)
        self.assertIn("错误", text or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
