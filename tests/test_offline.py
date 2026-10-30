"""离线单测：不访问网络，用 fake provider/context/client 覆盖解析、缓存、限流、配置错误。

运行（Windows 示例）：
    PYTHONPATH="<CORE>;<workspace>/plugins" \
    <INSTANCE_VENV>/Scripts/python.exe tests/test_offline.py
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime
from typing import Any

try:  # 作为 AstrBot 插件包导入（目录名即包名）
    from astrbot_plugin_model_websearch.client import (
        SEARCH_TOOL_NAME,
        NativeSearchClient,
        SearchError,
        SearchOutcome,
        TurnSearchState,
        _beijing_now,
        build_turn_limit_text,
        detect_query_quality_issues,
        format_time_context,
        get_turn_state,
        is_conflicting_search_tool,
        is_official_source,
        simplify_query,
        strip_site_operators,
        time_context_date,
        truncate_answer_sentences,
    )
except ModuleNotFoundError:  # 直接从仓库根目录跑源码（模块名不带包前缀）
    from client import (
        SEARCH_TOOL_NAME,
        NativeSearchClient,
        SearchError,
        SearchOutcome,
        TurnSearchState,
        _beijing_now,
        build_turn_limit_text,
        detect_query_quality_issues,
        format_time_context,
        get_turn_state,
        is_conflicting_search_tool,
        is_official_source,
        simplify_query,
        strip_site_operators,
        time_context_date,
        truncate_answer_sentences,
    )


def _load_tools_module() -> Any:
    """离线导入 tools.py（AstrBot FunctionTool 用最小 stub 替代，不写磁盘）。"""
    import sys
    import types
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    parent = str(repo_root.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    if "astrbot.core.agent.tool" not in sys.modules and "astrbot" in sys.modules:
        core = types.ModuleType("astrbot.core")
        core.__path__ = []  # type: ignore[attr-defined]
        agent = types.ModuleType("astrbot.core.agent")
        agent.__path__ = []  # type: ignore[attr-defined]
        tool_module = types.ModuleType("astrbot.core.agent.tool")

        class _StubFunctionTool:
            def __init__(self, **kwargs: Any) -> None:
                self.__dict__.update(kwargs)

        tool_module.FunctionTool = _StubFunctionTool  # type: ignore[attr-defined]
        sys.modules["astrbot.core"] = core
        sys.modules["astrbot.core.agent"] = agent
        sys.modules["astrbot.core.agent.tool"] = tool_module
    from astrbot_plugin_model_websearch import tools as tools_module

    return tools_module


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
    def __init__(
        self,
        response: Any,
        *,
        base_url: str = "",
        api_key: str = "",
    ) -> None:
        self.responses = FakeResponses(response)
        self.base_url = base_url
        self.api_key = api_key


class FakeProvider:
    def __init__(self, response: Any, provider_id: str = "example-provider/model-a") -> None:
        self.provider_config = {
            "id": provider_id,
            "type": "openai_chat_completion",
            "api_base": "http://127.0.0.1:8080/v1",
            "model": "model-a",
        }
        self.client = FakeClient(response)

    def get_model(self) -> str:
        return "model-a"


class NoResponsesProvider:
    provider_config = {"id": "deepseek/model-a", "type": "openai_chat_completion"}

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
        outcome = client._parse_response(response, "example-provider/model-a", "model-a", 5)
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
        client = NativeSearchClient({"provider_mode": "fixed", "provider_id": "example-provider/not-exist", "cache_ttl_seconds": 0})

        async def run() -> None:
            await client.search(context, "umo", "q")

        with self.assertRaises(SearchError) as ctx:
            asyncio.run(run())
        self.assertIn("不存在", str(ctx.exception))

    def test_fixed_mode_uses_configured_provider(self) -> None:
        fixed_provider = FakeProvider(make_response(GOOD_JSON, ["q"], []), provider_id="example-provider/fixed")
        context = FakeContext(FakeProvider(make_response(GOOD_JSON, ["q"], [])), providers={"example-provider/fixed": fixed_provider})
        client = NativeSearchClient({"provider_mode": "fixed", "provider_id": "example-provider/fixed", "cache_ttl_seconds": 0})

        async def run() -> Any:
            return await client.search(context, "umo", "q")

        outcome = asyncio.run(run())
        self.assertEqual(outcome.provider_id, "example-provider/fixed")
        self.assertEqual(fixed_provider.client.responses.calls, 1)

    def test_profile_override_provider_takes_precedence(self) -> None:
        override_provider = FakeProvider(make_response(GOOD_JSON, ["q"], []), provider_id="example-provider/model-b")
        session_provider = FakeProvider(make_response(GOOD_JSON, ["q"], []))
        context = FakeContext(session_provider, providers={"example-provider/model-b": override_provider})
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        async def run() -> Any:
            return await client.search(
                context,
                "umo",
                "q",
                search_provider_id="example-provider/model-b",
            )

        outcome = asyncio.run(run())
        self.assertEqual(outcome.provider_id, "example-provider/model-b")
        self.assertEqual(override_provider.client.responses.calls, 1)
        self.assertEqual(session_provider.client.responses.calls, 0)

    def test_profile_override_missing_provider_errors(self) -> None:
        context = FakeContext(FakeProvider(make_response(GOOD_JSON, ["q"], [])))
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        async def run() -> None:
            await client.search(context, "umo", "q", search_provider_id="example-provider/nope")

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
        self.assertEqual(request["model"], "model-a")

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


class FailureContextTests(unittest.TestCase):
    """失败路径要带 provider/model/elapsed/output 类型，便于线上排障；成功路径结构不变。"""

    def _run_search(self, provider: Any, client: NativeSearchClient) -> SearchError:
        async def run() -> None:
            await client.search(FakeContext(provider), "umo", "测试查询")

        with self.assertRaises(SearchError) as ctx:
            asyncio.run(run())
        return ctx.exception

    def test_parse_failure_includes_provider_model_elapsed_output(self) -> None:
        response = {
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": GOOD_JSON, "annotations": []}
                    ],
                },
            ],
            "status": "completed",
            "usage": {"output_tokens": 45},
        }
        provider = FakeProvider(response)
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        error = self._run_search(provider, client)
        message = str(error)

        # 原有可读错误文本保留，且仍可被 main.py 包成「错误：…」。
        self.assertIn("模型没有实际执行网页搜索", message)
        # 定位信息：provider / model / 耗时 / output 类型序列与数量。
        self.assertIn("provider=example-provider/model-a", message)
        self.assertIn("model=model-a", message)
        self.assertIn("elapsed=", message)
        self.assertIn("output=[reasoning, message]", message)
        self.assertIn("2 项", message)
        # 只记类型，不把响应正文抄进日志/模型文本。
        self.assertNotIn("AstrBot 文档", message)

        self.assertEqual(error.debug["provider_id"], "example-provider/model-a")
        self.assertEqual(error.debug["model"], "model-a")
        self.assertGreaterEqual(error.debug["elapsed_ms"], 0)
        self.assertEqual(error.debug["output_types"], ["reasoning", "message"])
        self.assertEqual(error.debug["output_counts"], {"reasoning": 1, "message": 1})
        self.assertEqual(error.debug["output_items"], 2)
        self.assertEqual(error.debug["status"], "completed")

    def test_empty_results_with_search_calls_keeps_context(self) -> None:
        response = {
            "output": [
                {
                    "type": "web_search_call",
                    "action": {"type": "search", "queries": ["q"]},
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "", "annotations": []}],
                },
            ],
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        }
        provider = FakeProvider(response)
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        error = self._run_search(provider, client)
        message = str(error)

        self.assertIn("原生搜索没有返回可用结果", message)
        self.assertIn("provider=example-provider/model-a", message)
        self.assertIn("output=[web_search_call, message]", message)
        self.assertIn("status=incomplete", message)
        self.assertIn("max_output_tokens", message)
        self.assertEqual(error.debug["status"], "incomplete")
        self.assertEqual(error.debug["incomplete_details"], {"reason": "max_output_tokens"})

    def test_api_call_failure_debug_has_provider_model_elapsed(self) -> None:
        class BoomResponses:
            async def create(self, **kwargs: Any) -> Any:
                raise RuntimeError("boom")

        provider = FakeProvider({})
        provider.client.responses = BoomResponses()  # type: ignore[assignment]
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        error = self._run_search(provider, client)

        self.assertIn("原生网页搜索调用失败", str(error))
        self.assertEqual(error.debug["provider_id"], "example-provider/model-a")
        self.assertEqual(error.debug["model"], "model-a")
        self.assertGreaterEqual(error.debug["elapsed_ms"], 0)

    def test_success_payload_shape_unchanged(self) -> None:
        provider = FakeProvider(make_response(GOOD_JSON, ["q"], []))
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        async def run() -> str:
            outcome = await client.search(FakeContext(provider), "umo", "q")
            return outcome.to_tool_text()

        payload = json.loads(asyncio.run(run()))
        self.assertEqual(
            set(payload), {"results", "answer", "queries", "sources", "provider", "elapsed_ms"}
        )
        self.assertEqual(
            payload["provider"], {"id": "example-provider/model-a", "model": "model-a"}
        )
        self.assertNotIn("debug", payload)


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


# ---------------------------------------------------------------- Gemini 原生后端


class FakePost:
    """替换 NativeSearchClient._post_json 的离线假网络出口。"""

    def __init__(self, results: list[tuple[int, Any, dict[str, str]]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, Any, dict[str, str]]:
        self.calls.append(
            {
                "url": url,
                "body": json.loads(body.decode("utf-8")),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]


class GeminiProvider:
    """模型名含 gemini 的假 provider：同时带 base_url/api_key 与 Responses 通道。"""

    def __init__(
        self,
        model: str = "gemini-model-a",
        provider_id: str = "example-provider/gemini-model-a",
        response: Any | None = None,
    ) -> None:
        self.model = model
        self.provider_config = {
            "id": provider_id,
            "type": "openai_chat_completion",
            "api_base": "http://127.0.0.1:8080/v1",
            "model": model,
        }
        self.client = FakeClient(
            response if response is not None else make_response(GOOD_JSON, ["q"], []),
            base_url="http://127.0.0.1:8080/v1/",
            api_key="test-key",
        )

    def get_model(self) -> str:
        return self.model


def make_native_payload() -> dict[str, Any]:
    """最小 Gemini 原生响应：2 queries + 2 chunks + 2 supports + 2 段 answer。"""
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "groundingMetadata": {
                    "webSearchQueries": ["q1", "q2"],
                    "groundingChunks": [
                        {
                            "web": {
                                "uri": "https://vertexaisearch.cloud.google.com/"
                                "grounding-api-redirect/abc",
                                "title": "liquipedia.net",
                            }
                        },
                        {
                            "web": {
                                "uri": "https://vertexaisearch.cloud.google.com/"
                                "grounding-api-redirect/def",
                                "title": "wikipedia.org",
                            }
                        },
                    ],
                    "groundingSupports": [
                        {
                            "segment": {
                                "startIndex": 0,
                                "endIndex": 10,
                                "text": "  第一段   证据 ",
                            },
                            "groundingChunkIndices": [0],
                        },
                        {
                            "segment": {
                                "startIndex": 20,
                                "endIndex": 40,
                                "text": "第二段证据",
                            },
                            "groundingChunkIndices": [1],
                        },
                    ],
                },
                "content": {
                    "parts": [{"text": "这是 Gemini 的回答。"}, {"text": "第二段回答。"}]
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 20,
            "totalTokenCount": 30,
        },
    }


class SequencedResponses:
    """按顺序返回不同响应的 Responses 假实现（S7 复核重试用）。"""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.requests.append(kwargs)
        index = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[index]


def make_native_payload_simple(
    chunks: list[tuple[str, str]],
    supports: list[tuple[int, str]] | None = None,
    answer: str = "",
    queries: list[str] | None = None,
) -> dict[str, Any]:
    """构造简化 Gemini 原生响应：chunks=(uri, title)，supports=(chunk_index, 片段)。"""
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "groundingMetadata": {
                    "webSearchQueries": queries if queries is not None else ["q"],
                    "groundingChunks": [
                        {"web": {"uri": uri, "title": title}} for uri, title in chunks
                    ],
                    "groundingSupports": [
                        {
                            "segment": {"text": snippet},
                            "groundingChunkIndices": [index],
                        }
                        for index, snippet in (supports or [])
                    ],
                },
                "content": {"parts": [{"text": answer}]} if answer else {"parts": []},
            }
        ],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
    }


class GeminiNativeBackendTests(unittest.TestCase):
    def _client(self, **overrides: Any) -> NativeSearchClient:
        config: dict[str, Any] = {"cache_ttl_seconds": 0}
        config.update(overrides)
        return NativeSearchClient(config)

    def _search(
        self,
        client: NativeSearchClient,
        provider: Any,
        query: str = "测试查询",
        max_results: int | None = None,
    ) -> SearchOutcome:
        async def run() -> SearchOutcome:
            return await client.search(FakeContext(provider), "umo", query, max_results)

        return asyncio.run(run())

    # ---------------------------------------------------------- 成功路径
    def test_native_success_parses_grounding(self) -> None:
        provider = GeminiProvider()
        client = self._client()
        post = FakePost([(200, make_native_payload(), {})])
        client._post_json = post  # type: ignore[assignment]

        outcome = self._search(client, provider)

        self.assertEqual(outcome.backend, "gemini_native")
        self.assertEqual(outcome.evidence, "gemini_grounding")
        self.assertEqual(outcome.instructions_mode, "native")
        self.assertEqual(outcome.queries, ["q1", "q2"])
        self.assertEqual(len(outcome.results), 2)
        self.assertEqual(outcome.results[0]["title"], "liquipedia.net")
        self.assertTrue(outcome.results[0]["url"].endswith("/abc"))
        self.assertEqual(outcome.results[0]["snippet"], "第一段 证据")
        self.assertEqual(outcome.results[1]["snippet"], "第二段证据")
        self.assertTrue(outcome.results[0]["index"].endswith(".1"))
        self.assertEqual(outcome.answer, "这是 Gemini 的回答。\n第二段回答。")
        self.assertEqual(
            [source["url"] for source in outcome.sources],
            [row["url"] for row in outcome.results],
        )
        self.assertEqual(outcome.usage, {"input": 10, "output": 20, "total": 30})

        payload = json.loads(outcome.to_tool_text())
        self.assertEqual(payload["backend"], "gemini_native")
        self.assertEqual(payload["evidence"], "gemini_grounding")
        self.assertEqual(payload["results"][0]["snippet"], "第一段 证据")

        # 请求形态：/v1beta + Bearer + google_search，root 去掉 /v1，用 gemini 超时。
        call = post.calls[0]
        self.assertEqual(
            call["url"],
            "http://127.0.0.1:8080/v1beta/models/gemini-model-a:generateContent",
        )
        self.assertEqual(call["body"]["tools"], [{"google_search": {}}])
        # S1：发给后端的 query 已注入北京时间上下文，原 query 保留在后。
        contents_text = call["body"]["contents"][0]["parts"][0]["text"]
        self.assertTrue(contents_text.startswith("当前北京时间"))
        self.assertIn("（UTC+8）", contents_text)
        self.assertTrue(contents_text.endswith("测试查询"))
        # S2：systemInstruction 带北京时间基准 / 当地日期与北京日期换算规则。
        instruction = call["body"]["systemInstruction"]["parts"][0]["text"]
        self.assertIn("北京时间", instruction)
        self.assertIn("当地日期", instruction)
        self.assertIn("北京日期", instruction)
        self.assertIn("不得编造", instruction)
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(call["timeout"], 90)
        # G-2026-10-31：默认 thinkingBudget=128 会进 Gemini 原生 generationConfig。
        self.assertEqual(
            call["body"]["generationConfig"],
            {"thinkingConfig": {"thinkingBudget": 128}},
        )

    def test_native_thinking_budget_zero_omits_generation_config(self) -> None:
        provider = GeminiProvider()
        client = NativeSearchClient({"gemini_thinking_budget": 0})
        post = FakePost([(200, make_native_payload(), {})])
        client._post_json = post  # type: ignore[assignment]

        self._search(client, provider)

        self.assertNotIn("generationConfig", post.calls[0]["body"])

    def test_native_accepts_candidate_parts_shape(self) -> None:
        # 本机反代把 parts 直接放在 candidate 上（官方是 content.parts）。
        payload = make_native_payload()
        candidate = payload["candidates"][0]
        candidate.pop("content")
        candidate["parts"] = [{"text": "代理形态回答"}]
        outcome = NativeSearchClient({})._parse_gemini_native_response(
            payload, "p", "gemini-x", 5, status=200
        )
        self.assertEqual(outcome.answer, "代理形态回答")
        self.assertEqual(len(outcome.results), 2)

    def test_bearer_401_retries_with_key_param(self) -> None:
        provider = GeminiProvider()
        client = self._client()
        post = FakePost(
            [(401, {"error": "auth"}, {}), (200, make_native_payload(), {})]
        )
        client._post_json = post  # type: ignore[assignment]

        outcome = self._search(client, provider)

        self.assertEqual(len(post.calls), 2)
        self.assertNotIn("?key=", post.calls[0]["url"])
        self.assertIn("?key=test-key", post.calls[1]["url"])
        self.assertNotIn("Authorization", post.calls[1]["headers"])
        self.assertEqual(outcome.backend, "gemini_native")

    def test_native_cache_hit_skips_second_model_call(self) -> None:
        provider = GeminiProvider()
        client = self._client(cache_ttl_seconds=60)
        post = FakePost([(200, make_native_payload(), {})])
        client._post_json = post  # type: ignore[assignment]

        async def run() -> tuple[SearchOutcome, SearchOutcome]:
            first = await client.search(FakeContext(provider), "umo", "同一个查询")
            second = await client.search(FakeContext(provider), "umo", "同一个查询")
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(first.results, second.results)

    # ---------------------------------------------------------- 失败/回退
    def test_no_grounding_raises_with_context(self) -> None:
        provider = GeminiProvider()
        client = self._client(gemini_native_fallback_to_responses=False)
        payload = {
            "candidates": [
                {"finishReason": "STOP", "parts": [{"text": "没有 grounding"}]}
            ]
        }
        client._post_json = FakePost([(200, payload, {})])  # type: ignore[assignment]

        with self.assertRaises(SearchError) as ctx:
            self._search(client, provider)

        error = ctx.exception
        message = str(error)
        self.assertIn("groundingMetadata", message)
        self.assertIn("backend=gemini_native", message)
        self.assertIn("grounding=none", message)
        self.assertEqual(error.debug["backend"], "gemini_native")
        self.assertEqual(error.debug["grounding"], "none")
        self.assertEqual(error.debug["status"], 200)
        self.assertEqual(error.debug["model"], "gemini-model-a")
        self.assertTrue(str(error.debug["endpoint"]).endswith(":generateContent"))
        self.assertEqual(provider.client.responses.calls, 0)

    def test_native_http_error_falls_back_to_hybrid_responses(self) -> None:
        hybrid_text = "先给简短回答。\n```json\n" + GOOD_JSON + "\n```"
        hybrid_response = make_response(
            hybrid_text,
            ["q"],
            [{"title": "来源", "url": "https://docs.astrbot.app/x"}],
        )
        provider = GeminiProvider(response=hybrid_response)
        client = self._client()
        client._post_json = FakePost([(404, {"error": "not found"}, {})])  # type: ignore[assignment]

        outcome = self._search(client, provider)

        self.assertEqual(outcome.backend, "responses_fallback")
        self.assertEqual(outcome.evidence, "responses_structured")
        self.assertEqual(outcome.instructions_mode, "hybrid")
        self.assertEqual(provider.client.responses.calls, 1)
        request = provider.client.responses.requests[0]
        self.assertIn("先用中文给出简短回答", request["instructions"])
        self.assertIn("results 最多", request["instructions"])
        self.assertGreaterEqual(len(outcome.results), 1)
        payload = json.loads(outcome.to_tool_text())
        self.assertEqual(payload["backend"], "responses_fallback")

    def test_fallback_disabled_propagates_native_error(self) -> None:
        provider = GeminiProvider()
        client = self._client(gemini_native_fallback_to_responses=False)
        client._post_json = FakePost([(500, {"error": "boom"}, {})])  # type: ignore[assignment]

        with self.assertRaises(SearchError) as ctx:
            self._search(client, provider)

        error = ctx.exception
        self.assertIn("HTTP 500", str(error))
        self.assertEqual(error.debug["backend"], "gemini_native")
        self.assertEqual(error.debug["status"], 500)
        self.assertEqual(provider.client.responses.calls, 0)

    # ---------------------------------------------------------- 空结果策略
    def test_empty_grounding_chunks_notice_by_default(self) -> None:
        provider = GeminiProvider()
        client = self._client()
        payload = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "groundingMetadata": {
                        "webSearchQueries": ["只有查询词"],
                        "groundingChunks": [],
                        "groundingSupports": [],
                    },
                    "content": {"parts": [{"text": "没有来源的模型回答"}]},
                }
            ]
        }
        client._post_json = FakePost([(200, payload, {})])  # type: ignore[assignment]

        outcome = self._search(client, provider)

        self.assertEqual(outcome.results, [])
        self.assertEqual(outcome.sources, [])
        self.assertTrue(outcome.notice)
        self.assertEqual(outcome.answer, "没有来源的模型回答")
        payload_out = json.loads(outcome.to_tool_text())
        self.assertIn("notice", payload_out)
        self.assertEqual(payload_out["backend"], "gemini_native")

    def test_empty_grounding_chunks_error_policy(self) -> None:
        provider = GeminiProvider()
        client = self._client(
            empty_result_policy="error", gemini_native_fallback_to_responses=False
        )
        payload = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "groundingMetadata": {
                        "webSearchQueries": ["只有查询词"],
                        "groundingChunks": [],
                    },
                }
            ]
        }
        client._post_json = FakePost([(200, payload, {})])  # type: ignore[assignment]

        with self.assertRaises(SearchError) as ctx:
            self._search(client, provider)

        error = ctx.exception
        self.assertIn("原生搜索没有返回可用结果", str(error))
        self.assertIn(error.debug["grounding"], {"queries_only", "empty"})

    def test_grounding_without_queries_or_chunks_always_raises(self) -> None:
        provider = GeminiProvider()
        client = self._client(
            empty_result_policy="notice", gemini_native_fallback_to_responses=False
        )
        payload = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "groundingMetadata": {
                        "webSearchQueries": [],
                        "groundingChunks": [],
                    },
                }
            ]
        }
        client._post_json = FakePost([(200, payload, {})])  # type: ignore[assignment]

        with self.assertRaises(SearchError) as ctx:
            self._search(client, provider)

        self.assertIn("原生搜索没有返回可用结果", str(ctx.exception))
        self.assertEqual(ctx.exception.debug["grounding"], "empty")

    # ---------------------------------------------------------- vertex 还原
    def test_vertex_redirect_resolve_replaces_urls(self) -> None:
        provider = GeminiProvider()
        client = self._client(vertex_redirect_resolve=True)
        client._post_json = FakePost([(200, make_native_payload(), {})])  # type: ignore[assignment]
        resolved: list[str] = []

        def fake_resolver(uri: str) -> str:
            resolved.append(uri)
            if uri.endswith("/abc"):
                return "https://lolesports.com/en-SG/tournament/115"
            return ""

        client._resolve_vertex_location = fake_resolver  # type: ignore[assignment]

        outcome = self._search(client, provider)

        urls = [row["url"] for row in outcome.results]
        self.assertEqual(urls[0], "https://lolesports.com/en-SG/tournament/115")
        # 还原失败的保留原 vertex 跳转 URL。
        self.assertTrue(urls[1].startswith("https://vertexaisearch.cloud.google.com/"))
        self.assertEqual(outcome.sources[0]["url"], urls[0])
        self.assertEqual(len(resolved), 2)
        # S12：还原成功 = resolved True，仍为跳转 = False（results/sources 同步）。
        self.assertTrue(outcome.results[0]["resolved"])
        self.assertFalse(outcome.results[1]["resolved"])
        self.assertTrue(outcome.sources[0]["resolved"])
        self.assertFalse(outcome.sources[1]["resolved"])

    def test_vertex_resolve_disabled_never_resolves(self) -> None:
        provider = GeminiProvider()
        client = self._client(vertex_redirect_resolve=False)
        client._post_json = FakePost([(200, make_native_payload(), {})])  # type: ignore[assignment]
        calls: list[str] = []

        def fake_resolver(uri: str) -> str:
            calls.append(uri)
            return "https://example.com/should-not-happen"

        client._resolve_vertex_location = fake_resolver  # type: ignore[assignment]

        outcome = self._search(client, provider)

        self.assertEqual(calls, [])
        self.assertTrue(
            all(
                "vertexaisearch" in row["url"]
                for row in outcome.results
            )
        )

    def test_vertex_safety_filter_rejects_private_hosts(self) -> None:
        self.assertTrue(
            NativeSearchClient._is_safe_public_http_url("https://lolesports.com/x")
        )
        self.assertFalse(
            NativeSearchClient._is_safe_public_http_url("http://127.0.0.1/x")
        )
        self.assertFalse(
            NativeSearchClient._is_safe_public_http_url("http://localhost/x")
        )
        self.assertFalse(
            NativeSearchClient._is_safe_public_http_url("file:///C:/secret.txt")
        )

    # ---------------------------------------------------------- 后端选择
    def test_gemini_can_be_forced_to_responses_backend(self) -> None:
        provider = GeminiProvider()
        client = self._client(gemini_search_backend="responses")
        client._post_json = FakePost([])  # type: ignore[assignment] - 被调用会 IndexError

        outcome = self._search(client, provider)

        self.assertEqual(outcome.backend, "responses")
        self.assertEqual(outcome.instructions_mode, "json")
        self.assertEqual(provider.client.responses.calls, 1)
        self.assertIn("不要写总结", provider.client.responses.requests[0]["instructions"])
        payload = json.loads(outcome.to_tool_text())
        self.assertEqual(
            set(payload),
            {"results", "answer", "queries", "sources", "provider", "elapsed_ms"},
        )

    def test_non_gemini_model_never_calls_native(self) -> None:
        provider = FakeProvider(make_response(GOOD_JSON, ["q"], []))
        client = self._client()

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("非 Gemini 模型不应调用原生端点")

        client._call_gemini_native = boom  # type: ignore[assignment]
        outcome = self._search(client, provider)
        self.assertEqual(outcome.backend, "responses")
        self.assertEqual(provider.client.responses.calls, 1)


class GeminiConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        client = NativeSearchClient({})
        self.assertEqual(client.gemini_search_backend, "auto")
        self.assertTrue(client.gemini_native_fallback_to_responses)
        self.assertEqual(client.gemini_timeout_seconds, 90)
        self.assertEqual(client.gemini_thinking_budget, 128)
        self.assertEqual(client.empty_result_policy, "notice")
        self.assertFalse(client.vertex_redirect_resolve)

    def test_invalid_values_fall_back_and_clamp(self) -> None:
        client = NativeSearchClient(
            {
                "gemini_search_backend": "weird",
                "gemini_timeout_seconds": 1,
                "empty_result_policy": "nope",
                "gemini_native_fallback_to_responses": "false",
                "vertex_redirect_resolve": "true",
                "gemini_thinking_budget": 99999,
            }
        )
        self.assertEqual(client.gemini_search_backend, "auto")
        self.assertEqual(client.gemini_timeout_seconds, 5)
        self.assertEqual(client.gemini_thinking_budget, 4096)
        self.assertEqual(client.empty_result_policy, "notice")
        self.assertFalse(client.gemini_native_fallback_to_responses)
        self.assertTrue(client.vertex_redirect_resolve)
        self.assertEqual(
            NativeSearchClient({"gemini_timeout_seconds": 9999}).gemini_timeout_seconds,
            300,
        )

    def test_should_use_gemini_native(self) -> None:
        self.assertTrue(
            NativeSearchClient({})._should_use_gemini_native("gemini-model-a")
        )
        self.assertFalse(NativeSearchClient({})._should_use_gemini_native("gpt-model-a"))
        self.assertTrue(
            NativeSearchClient({"gemini_search_backend": "native"})._should_use_gemini_native(
                "gpt-model-a"
            )
        )
        self.assertFalse(
            NativeSearchClient({"gemini_search_backend": "responses"})._should_use_gemini_native(
                "gemini-model-a"
            )
        )

    def test_hybrid_instructions_text(self) -> None:
        text = NativeSearchClient({})._hybrid_instructions(3)
        self.assertIn("先用中文给出简短回答", text)
        self.assertIn("results 最多 3 条", text)
        self.assertIn('"answer":""', text)


# ---------------------------------------------------------------- 0.2.4 搜索正确率


class TimeContextTests(unittest.TestCase):
    """S1：注入/指令里的时间基准必须是北京时间（UTC+8）+ 日期 + 星期。"""

    def test_format_time_context_is_beijing(self) -> None:
        text = format_time_context(datetime(2026, 10, 31, 2, 29))
        self.assertEqual(text, "当前北京时间 2026-10-31 02:29 周六（UTC+8）")

    def test_time_context_date_bucket(self) -> None:
        self.assertEqual(time_context_date(datetime(2026, 10, 31, 23, 59)), "2026-10-31")

    def test_live_clock_returns_datetime(self) -> None:
        self.assertIsInstance(_beijing_now(), datetime)


class QueryGuardTests(unittest.TestCase):
    """S1：query 预处理 —— 注入时间、剥 site:、保留原话、可关闭。"""

    def test_strip_site_operators(self) -> None:
        cleaned, removed = strip_site_operators("site:lolesports.com/en news 今天")
        self.assertNotIn("site:", cleaned)
        self.assertIn("news", cleaned)
        self.assertIn("今天", cleaned)
        self.assertEqual(removed, ["site:lolesports.com/en"])

    def test_prepare_query_injects_time_and_strips_site(self) -> None:
        client = NativeSearchClient({})
        prepared, guard = client._prepare_query("site:lolesports.com 今天S赛赛程")
        self.assertTrue(prepared.startswith("当前北京时间"))
        self.assertIn("今天S赛赛程", prepared)
        self.assertNotIn("site:", prepared)
        self.assertTrue(guard["changed"])
        self.assertEqual(guard["stripped_site"], ["site:lolesports.com"])

    def test_prepare_query_switches_off(self) -> None:
        client = NativeSearchClient(
            {"query_context_inject": False, "strip_site_operator": False}
        )
        prepared, guard = client._prepare_query("site:x 今天")
        self.assertEqual(prepared, "site:x 今天")
        self.assertFalse(guard["changed"])

    def test_prepare_query_keeps_original_wording(self) -> None:
        client = NativeSearchClient({})
        prepared, _ = client._prepare_query("查询今天S赛的赛程")
        self.assertTrue(prepared.endswith("查询今天S赛的赛程"))

    def test_prepare_query_only_site_falls_back_to_original(self) -> None:
        client = NativeSearchClient({"query_context_inject": False})
        prepared, guard = client._prepare_query("site:lolesports.com")
        self.assertEqual(prepared, "site:lolesports.com")
        self.assertEqual(guard["stripped_site"], ["site:lolesports.com"])

    def test_build_retry_query_simplifies_dates_and_site(self) -> None:
        client = NativeSearchClient({})
        retry = client._build_retry_query(
            "site:lolesports.com/en-GB/news/worlds-2026-primer "
            "October 31 2026 Worlds schedule November 1"
        )
        self.assertIn("当前北京时间", retry)
        self.assertNotIn("site:", retry)
        self.assertNotIn("November 1", retry)
        self.assertIn("Worlds schedule", retry)

    def test_simplify_query_fallback_when_everything_removed(self) -> None:
        self.assertEqual(simplify_query("October 31"), "October 31")


class QueryQualityWarningTests(unittest.TestCase):
    """S10：今天/today + 外文绝对日期或 site: 时告警（只告警不改行为）。"""

    def test_today_plus_foreign_date_is_flagged(self) -> None:
        issues = detect_query_quality_issues("查询今天S赛 October 31 schedule")
        self.assertTrue(any("绝对日期" in item for item in issues))

    def test_today_plus_site_is_flagged(self) -> None:
        issues = detect_query_quality_issues("今天赛程 site:lolesports.com")
        self.assertTrue(any("site:" in item for item in issues))

    def test_clean_query_is_not_flagged(self) -> None:
        self.assertEqual(detect_query_quality_issues("查询今天S赛的赛程"), [])
        self.assertEqual(detect_query_quality_issues("October 31 Worlds schedule"), [])


class InstructionTextTests(unittest.TestCase):
    """S2/S6：native systemInstruction 与 Responses/fallback 指令都带日期规则。"""

    def test_native_system_instruction_rules(self) -> None:
        text = NativeSearchClient({})._gemini_system_instruction()
        for token in ("北京时间", "UTC+8", "当地日期", "北京日期", "不得编造", "官方"):
            self.assertIn(token, text)

    def test_responses_instructions_include_date_rules(self) -> None:
        text = NativeSearchClient({})._instructions(10, False)
        self.assertIn("北京时间", text)
        self.assertIn("3–5 条", text)
        self.assertIn('"answer":""', text)
        self.assertIn("不要写总结", text)

    def test_hybrid_instructions_include_date_rules_and_min_results(self) -> None:
        text = NativeSearchClient({})._hybrid_instructions(5)
        self.assertIn("北京时间", text)
        self.assertIn("3–5 条", text)
        self.assertIn("results 最多 5 条", text)


class SuspiciousRetryTests(unittest.TestCase):
    """S7：可疑「没有比赛/无赛程」或无结果时自动复核 1 次（每个 search 最多 1 次）。"""

    def _search(
        self, client: NativeSearchClient, provider: Any, query: str = "查询今天S赛的赛程"
    ) -> SearchOutcome:
        async def run() -> SearchOutcome:
            return await client.search(FakeContext(provider), "umo", query)

        return asyncio.run(run())

    def test_native_no_results_retry_then_success(self) -> None:
        empty_payload = make_native_payload_simple([], answer="没有检索到相关来源")
        good_payload = make_native_payload_simple(
            [("https://www.lolesports.com/en-SG/tournament/115", "lolesports.com")],
            supports=[(0, "北京 10 月 31 日 04:00 LOS vs TLAW")],
            answer="北京 10 月 31 日 04:00 LOS vs TLAW。",
        )
        provider = GeminiProvider()
        client = NativeSearchClient({"cache_ttl_seconds": 0})
        post = FakePost([(200, empty_payload, {}), (200, good_payload, {})])
        client._post_json = post  # type: ignore[assignment]

        outcome = self._search(client, provider)

        self.assertEqual(len(post.calls), 2)
        retry_query = post.calls[1]["body"]["contents"][0]["parts"][0]["text"]
        self.assertIn("当前北京时间", retry_query)
        self.assertEqual(len(outcome.results), 1)
        self.assertIn("LOS vs TLAW", outcome.answer)
        self.assertFalse(outcome.notice)

    def test_retry_at_most_once_when_still_suspicious(self) -> None:
        suspicious = make_native_payload_simple(
            [("https://accountshark.net/x", "accountshark.net")],
            supports=[(0, "There are no matches scheduled on Sunday, November 1")],
            answer="北京时间今天没有比赛。",
        )
        provider = GeminiProvider()
        client = NativeSearchClient({"cache_ttl_seconds": 0})
        post = FakePost([(200, suspicious, {}), (200, suspicious, {})])
        client._post_json = post  # type: ignore[assignment]

        outcome = self._search(client, provider)

        self.assertEqual(len(post.calls), 2)  # 首次 + 复核 1 次，绝不再重试
        self.assertTrue(outcome.notice)
        self.assertIn("自动复核", outcome.notice)

    def test_suspicious_matchup_present_does_not_retry(self) -> None:
        payload = make_native_payload_simple(
            [("https://www.lolesports.com/x", "lolesports.com")],
            supports=[(0, "no matches scheduled on Sunday, but LOS vs TLAW on Oct 30")],
            answer="no matches on Sunday；北京 10/31 04:00 LOS vs TLAW。",
        )
        provider = GeminiProvider()
        client = NativeSearchClient({"cache_ttl_seconds": 0})
        post = FakePost([(200, payload, {})])
        client._post_json = post  # type: ignore[assignment]

        self._search(client, provider)
        self.assertEqual(len(post.calls), 1)

    def test_retry_disabled_keeps_single_call(self) -> None:
        suspicious = make_native_payload_simple(
            [("https://accountshark.net/x", "accountshark.net")],
            supports=[(0, "There are no matches scheduled on Sunday, November 1")],
            answer="北京时间今天没有比赛。",
        )
        provider = GeminiProvider()
        client = NativeSearchClient(
            {"cache_ttl_seconds": 0, "suspicious_result_retry": False}
        )
        post = FakePost([(200, suspicious, {})])
        client._post_json = post  # type: ignore[assignment]

        outcome = self._search(client, provider)

        self.assertEqual(len(post.calls), 1)
        self.assertFalse(outcome.notice)

    def test_responses_path_retry_uses_simplified_query(self) -> None:
        first = make_response(
            json.dumps(
                {
                    "results": [
                        {
                            "title": "accountshark",
                            "url": "https://accountshark.net/x",
                            "snippet": "no matches scheduled on Sunday, November 1",
                        }
                    ],
                    "answer": "今天没有比赛",
                },
                ensure_ascii=False,
            ),
            ["q"],
            [],
        )
        second = make_response(GOOD_JSON, ["q"], [])
        provider = FakeProvider(first)
        provider.client.responses = SequencedResponses([first, second])  # type: ignore[assignment]
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        outcome = self._search(
            client, provider, query="site:lolesports.com 今天S赛 October 31"
        )

        self.assertEqual(provider.client.responses.calls, 2)
        retry_input = provider.client.responses.requests[1]["input"]
        self.assertIn("当前北京时间", retry_input)
        self.assertNotIn("site:", retry_input)
        self.assertEqual(len(outcome.results), 2)

    def test_error_policy_raises_when_retry_still_empty(self) -> None:
        empty_response = make_response(
            json.dumps({"results": [], "answer": "没有找到"}, ensure_ascii=False), ["q"], []
        )
        provider = FakeProvider(empty_response)
        provider.client.responses = SequencedResponses(  # type: ignore[assignment]
            [empty_response, empty_response]
        )
        client = NativeSearchClient(
            {"cache_ttl_seconds": 0, "empty_result_policy": "error"}
        )

        with self.assertRaises(SearchError) as ctx:
            self._search(client, provider)

        self.assertIn("自动复核", str(ctx.exception))
        self.assertEqual(ctx.exception.debug["retry_reason"], "no_results")
        self.assertEqual(provider.client.responses.calls, 2)


class AnswerTruncationTests(unittest.TestCase):
    """S11：native answer 按句截断（默认 800），snippet 标注来源。"""

    def test_truncate_by_sentence(self) -> None:
        text = "第一句。" * 40
        out = truncate_answer_sentences(text, 100)
        self.assertLessEqual(len(out), 100)
        self.assertTrue(out.endswith("。"))

    def test_short_answer_unchanged(self) -> None:
        self.assertEqual(truncate_answer_sentences("短回答。", 800), "短回答。")

    def test_default_limit_keeps_700_chars(self) -> None:
        payload = make_native_payload_simple([], answer="a" * 700)
        outcome = NativeSearchClient({})._parse_gemini_native_response(
            payload, "p", "gemini-x", 5
        )
        self.assertEqual(len(outcome.answer), 700)

    def test_native_answer_uses_configured_limit(self) -> None:
        payload = make_native_payload_simple([], answer="甲" * 500)
        outcome = NativeSearchClient({"answer_max_chars": 100})._parse_gemini_native_response(
            payload, "p", "gemini-x", 5
        )
        self.assertLessEqual(len(outcome.answer), 100)

    def test_native_snippet_source_marked(self) -> None:
        payload = make_native_payload_simple(
            [("https://example.com/a", "example.com")],
            supports=[(0, "片段")],
            answer="回答",
        )
        outcome = NativeSearchClient({})._parse_gemini_native_response(
            payload, "p", "gemini-x", 5
        )
        self.assertEqual(outcome.results[0]["snippet_source"], "model_answer_segment")
        self.assertEqual(
            json.loads(outcome.to_tool_text())["results"][0]["snippet_source"],
            "model_answer_segment",
        )


class OfficialSourceTests(unittest.TestCase):
    """S12：官方域名优先排序 + vertex 还原 resolved 标记。"""

    def test_is_official_source(self) -> None:
        self.assertTrue(is_official_source("https://lolesports.com/x"))
        self.assertTrue(is_official_source("https://en.wikipedia.org/wiki/x"))
        self.assertFalse(is_official_source("https://accountshark.net/x"))
        self.assertFalse(
            is_official_source("https://vertexaisearch.cloud.google.com/redirect/abc")
        )

    def test_native_results_put_official_first(self) -> None:
        payload = make_native_payload_simple(
            [
                ("https://accountshark.net/a", "accountshark.net"),
                ("https://www.lolesports.com/x", "lolesports.com"),
                ("https://timesaver.gg/b", "timesaver.gg"),
                ("https://liquipedia.net/leagueoflegends/c", "liquipedia.net"),
            ],
            supports=[(0, "非官方一"), (1, "官方一"), (2, "非官方二"), (3, "官方二")],
            answer="回答",
        )
        outcome = NativeSearchClient({})._parse_gemini_native_response(
            payload, "p", "gemini-x", 10
        )
        urls = [row["url"] for row in outcome.results]
        self.assertTrue(urls[0].startswith("https://www.lolesports.com"))
        self.assertTrue(urls[1].startswith("https://liquipedia.net"))
        # 同组内保持后端返回顺序（稳定排序）。
        self.assertTrue(urls[2].startswith("https://accountshark.net"))
        self.assertTrue(urls[3].startswith("https://timesaver.gg"))
        self.assertEqual([source["url"] for source in outcome.sources], urls)

    def test_responses_results_put_official_first(self) -> None:
        # S12 收尾：Responses/fallback 路径也按官方域名优先（字段/结构不变，无 resolved）。
        payload = json.dumps(
            {
                "results": [
                    {
                        "title": "accountshark",
                        "url": "https://accountshark.net/a",
                        "snippet": "非官方一",
                    },
                    {
                        "title": "lolesports",
                        "url": "https://www.lolesports.com/x",
                        "snippet": "官方一",
                    },
                    {
                        "title": "timesaver",
                        "url": "https://timesaver.gg/b",
                        "snippet": "非官方二",
                    },
                    {
                        "title": "liquipedia",
                        "url": "https://liquipedia.net/leagueoflegends/c",
                        "snippet": "官方二",
                    },
                ],
                "answer": "回答",
            },
            ensure_ascii=False,
        )
        provider = FakeProvider(make_response(payload, ["q"], []))
        client = NativeSearchClient({"cache_ttl_seconds": 0})

        async def run() -> SearchOutcome:
            return await client.search(FakeContext(provider), "umo", "q")

        outcome = asyncio.run(run())
        urls = [row["url"] for row in outcome.results]
        self.assertTrue(urls[0].startswith("https://www.lolesports.com"))
        self.assertTrue(urls[1].startswith("https://liquipedia.net"))
        # 同组内保持后端返回顺序（稳定排序）。
        self.assertTrue(urls[2].startswith("https://accountshark.net"))
        self.assertTrue(urls[3].startswith("https://timesaver.gg"))
        # index 序号跟随展示顺序；Responses 结果不新增 resolved 字段。
        self.assertTrue(outcome.results[0]["index"].endswith(".1"))
        self.assertTrue(outcome.results[3]["index"].endswith(".4"))
        self.assertNotIn("resolved", outcome.results[0])
        payload_out = json.loads(outcome.to_tool_text())
        self.assertEqual(
            set(payload_out),
            {"results", "answer", "queries", "sources", "provider", "elapsed_ms"},
        )

    def test_resolved_flags_for_vertex_and_direct(self) -> None:
        vertex_url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
        payload = make_native_payload_simple(
            [(vertex_url, "lolesports.com"), ("https://example.com/direct", "example.com")],
            answer="回答",
        )
        outcome = NativeSearchClient({})._parse_gemini_native_response(
            payload, "p", "gemini-x", 5
        )
        flags = {row["url"]: row["resolved"] for row in outcome.results}
        self.assertFalse(flags[vertex_url])
        self.assertTrue(flags["https://example.com/direct"])

    def test_vertex_resolve_reorders_official_first(self) -> None:
        # 回归：解析时 URL 全是 vertex 跳转（都非官方），还原出真实域名后必须重新排序，
        # 否则官方来源（如 lolesports/liquipedia）会排在非官方后面。
        payload = make_native_payload_simple(
            [
                (
                    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/unresolvable",
                    "accountshark.net",
                ),
                (
                    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/lolesports",
                    "lolesports.com",
                ),
                (
                    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/liquipedia",
                    "liquipedia.net",
                ),
            ],
            supports=[(0, "片段一"), (1, "片段二"), (2, "片段三")],
            answer="回答",
        )
        provider = GeminiProvider()
        client = NativeSearchClient(
            {"cache_ttl_seconds": 0, "vertex_redirect_resolve": True}
        )
        client._post_json = FakePost([(200, payload, {})])  # type: ignore[assignment]

        def fake_resolver(uri: str) -> str:
            if uri.endswith("/lolesports"):
                return "https://www.lolesports.com/en-SG/tournament/115"
            if uri.endswith("/liquipedia"):
                return "https://liquipedia.net/leagueoflegends/x"
            return ""  # 还原失败：保留原 vertex 跳转

        client._resolve_vertex_location = fake_resolver  # type: ignore[assignment]

        async def run() -> SearchOutcome:
            return await client.search(FakeContext(provider), "umo", "查询赛程")

        outcome = asyncio.run(run())
        urls = [row["url"] for row in outcome.results]
        self.assertTrue(urls[0].startswith("https://www.lolesports.com"))
        self.assertTrue(urls[1].startswith("https://liquipedia.net"))
        self.assertTrue(urls[2].startswith("https://vertexaisearch.cloud.google.com/"))
        # index 序号与展示顺序一致。
        self.assertTrue(outcome.results[0]["index"].endswith(".1"))
        self.assertTrue(outcome.results[1]["index"].endswith(".2"))
        self.assertTrue(outcome.results[2]["index"].endswith(".3"))
        # sources 顺序与 results 同步，resolved 标记保留。
        self.assertEqual([source["url"] for source in outcome.sources], urls)
        self.assertEqual(
            [source["resolved"] for source in outcome.sources],
            [True, True, False],
        )
        self.assertEqual(
            [row["resolved"] for row in outcome.results], [True, True, False]
        )


class SearchGuardConfigTests(unittest.TestCase):
    """新增 4 项配置的默认值与钳制。"""

    def test_defaults(self) -> None:
        client = NativeSearchClient({})
        self.assertTrue(client.query_context_inject)
        self.assertTrue(client.strip_site_operator)
        self.assertTrue(client.suspicious_result_retry)
        self.assertEqual(client.answer_max_chars, 800)

    def test_invalid_values_fall_back_and_clamp(self) -> None:
        client = NativeSearchClient(
            {
                "query_context_inject": "false",
                "strip_site_operator": 0,
                "suspicious_result_retry": "off",
                "answer_max_chars": 5,
            }
        )
        self.assertFalse(client.query_context_inject)
        self.assertFalse(client.strip_site_operator)
        self.assertFalse(client.suspicious_result_retry)
        self.assertEqual(client.answer_max_chars, 100)
        self.assertEqual(
            NativeSearchClient({"answer_max_chars": 99999}).answer_max_chars, 4000
        )


class ToolDescriptionTests(unittest.TestCase):
    """S3：工具描述约束主模型用自然语言/原话，不预换算日期、不加 site:。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = _load_tools_module()

    def test_description_constrains_query(self) -> None:
        text = self.tools.TOOL_DESCRIPTION
        for token in (
            "中文自然语言",
            "用户原话",
            "不要自行把「今天/明天」换算成绝对日期",
            "site:",
            "8–10",
        ):
            self.assertIn(token, text)

    def test_query_parameter_description(self) -> None:
        description = self.tools.TOOL_PARAMETERS["properties"]["query"]["description"]
        self.assertIn("自然语言", description)
        self.assertIn("site:", description)
        max_results = self.tools.TOOL_PARAMETERS["properties"]["max_results"]["description"]
        self.assertIn("8–10", max_results)


if __name__ == "__main__":
    unittest.main(verbosity=2)
