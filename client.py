"""模型原生网页搜索：provider 解析、Responses API 调用、结果解析、缓存与限流。

设计要点：
- 只依赖 AstrBot 自带依赖（openai / aiohttp），不引入新包。
- provider 的 OpenAI 客户端（provider.client）优先复用，保证 api_base / key / 代理 / 自定义 header 与聊天请求一致。
- 任何失败都转成 SearchError，由调用方变成模型可读的「错误：…」文本，不让异常打断整轮对话。
- 日志只记录 provider id、模型、耗时、token 用量，不记录 API Key。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

# AstrBot 本体的哨兵值：插件只在这个值下接管网页搜索工具。
SENTINEL_PROVIDER = "model_native"
SEARCH_TOOL_NAME = "web_search_model_native"

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_URL_ALLOWED = ("http://", "https://")

# AstrBot 内置网页搜索相关工具名（astrbot/core/tools/web_search_tools.py）。
_BUILTIN_SEARCH_TOOL_NAMES = {
    "web_search_baidu",
    "web_search_tavily",
    "tavily_extract_web_page",
    "web_search_bocha",
    "web_search_brave",
    "web_search_firecrawl",
    "firecrawl_extract_web_page",
    "web_search_exa",
    "exa_get_contents",
    "web_search_anysearch",
    "web_search_serpapi",
    "image_search_serpapi",
    "finance_search_serpapi",
    "scholar_search_serpapi",
    "web_search_deepseek",
    "deepseek_fetch_web_page",
}
# 其它常见搜索插件的工具名前缀/名字（AnySearch 等）。
_CONFLICT_PREFIXES = ("web_search", "anysearch_")
_DEFAULT_CONFLICT_NAMES = _BUILTIN_SEARCH_TOOL_NAMES | {
    "search_web",
    "websearch",
    "anysearch_search",
    "anysearch_extract",
    "anysearch_batch_search",
}


def is_conflicting_search_tool(tool_name: str, extra_names: set[str] | None = None) -> bool:
    """判断一个工具名是否属于「同类网页搜索工具」（用于冲突处理）。

    Args:
        tool_name: 待判断的工具名。
        extra_names: 用户额外配置的冲突工具名。

    Returns:
        True 表示与本插件工具功能重叠。
    """
    name = (tool_name or "").strip()
    if not name or name == SEARCH_TOOL_NAME:
        return False
    if extra_names and name in extra_names:
        return True
    if name in _DEFAULT_CONFLICT_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _CONFLICT_PREFIXES)


class SearchError(RuntimeError):
    """搜索失败。消息会直接展示给模型/用户，不包含密钥。"""


@dataclass
class SearchOutcome:
    """一次搜索的规范化结果。"""

    results: list[dict[str, str]]
    answer: str
    queries: list[str]
    sources: list[dict[str, str]]
    provider_id: str
    model: str
    elapsed_ms: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "results": self.results,
            "answer": self.answer,
            "queries": self.queries,
            "sources": self.sources,
            "provider": {"id": self.provider_id, "model": self.model},
            "elapsed_ms": self.elapsed_ms,
        }

    def to_tool_text(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)


_TURN_STATE_ATTR = "_model_websearch_turn_state"


@dataclass
class TurnSearchState:
    """同一轮用户消息内的搜索计数与最后一次成功结果。"""

    calls: int = 0
    last: SearchOutcome | None = None


def get_turn_state(event: Any) -> TurnSearchState:
    """读取/创建挂在 event 上的本轮搜索状态（AstrBot 一轮 Agent 复用同一 event）。"""
    state = getattr(event, _TURN_STATE_ATTR, None)
    if not isinstance(state, TurnSearchState):
        state = TurnSearchState()
        try:
            setattr(event, _TURN_STATE_ATTR, state)
        except Exception:  # noqa: BLE001 - 某些 event 可能禁止加属性，退化为不限制
            pass
    return state


def build_turn_limit_text(
    state: TurnSearchState, limit: int, tool_name: str
) -> str | None:
    """本轮搜索次数达到上限时，返回给模型的「复用已有结果、不要再搜」文本。"""
    if limit <= 0 or state.calls < limit:
        return None
    if state.last is None:
        return f"错误：本轮网页搜索次数已达上限（{limit}），且没有可用结果。"
    payload = state.last.to_payload()
    payload["notice"] = (
        f"本轮已执行 {state.calls} 次网页搜索（上限 {limit}）。"
        f"不要再次调用 {tool_name}；请直接基于以上结果作答。"
    )
    return json.dumps(payload, ensure_ascii=False)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001 - 解析失败时退回空 dict
            return {}
    return {}


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "启用", "开启"}
    return default if value is None else bool(value)


def _coerce_int(value: Any, default: int, *, low: int | None = None, high: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


class NativeSearchClient:
    """通过聊天模型 provider 的原生 web_search 执行搜索。"""

    def __init__(self, config: dict[str, Any] | None) -> None:
        self.config: dict[str, Any] = config or {}
        self.disabled = _coerce_bool(self.config.get("disable_web_search"), False)
        self.provider_mode = str(self.config.get("provider_mode") or "current").strip().lower()
        self.fixed_provider_id = str(self.config.get("provider_id") or "").strip()
        self.max_results = _coerce_int(self.config.get("max_results"), 10, low=1, high=10)
        context_size = str(self.config.get("search_context_size") or "low").strip().lower()
        self.search_context_size = (
            context_size if context_size in {"low", "medium", "high"} else "low"
        )
        self.max_output_tokens = _coerce_int(
            self.config.get("max_output_tokens"), 400, low=100, high=2000
        )
        self.timeout_seconds = _coerce_int(
            self.config.get("timeout_seconds"), 45, low=5, high=300
        )
        self.cache_ttl_seconds = _coerce_int(
            self.config.get("cache_ttl_seconds"), 300, low=0, high=3600
        )
        self.rate_limit_per_minute = _coerce_int(
            self.config.get("rate_limit_per_minute"), 6, low=0, high=120
        )
        # 同一轮用户消息里最多允许几次真实搜索（0=不限制）；超过后工具直接返回已有结果。
        self.max_calls_per_turn = _coerce_int(
            self.config.get("max_calls_per_turn"), 2, low=0, high=5
        )
        # 搜索后端是否额外生成一段 answer；false 时只返回 results，最终回答由主模型写。
        self.include_answer = _coerce_bool(self.config.get("include_answer"), False)
        policy = str(self.config.get("conflict_policy") or "prefer_self").strip().lower()
        self.conflict_policy = (
            policy if policy in {"prefer_self", "keep_all", "skip_self"} else "prefer_self"
        )
        extra = str(self.config.get("extra_conflict_tool_names") or "")
        self.extra_conflict_names = {
            item.strip() for item in re.split(r"[,，\s]+", extra) if item.strip()
        }
        self.command_chunk_size = _coerce_int(
            self.config.get("command_result_chunk_size"), 1200, low=0, high=8000
        )
        self.chunk_private = _coerce_bool(
            self.config.get("chunk_private_command_results"), True
        )
        self.chunk_group = _coerce_bool(
            self.config.get("chunk_group_command_results"), False
        )
        # cache: key -> (过期时间戳, SearchOutcome)
        self._cache: dict[str, tuple[float, SearchOutcome]] = {}
        # rate: umo -> deque[时间戳]
        self._rate: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------ 配置
    def is_conflicting_tool(self, tool_name: str) -> bool:
        return is_conflicting_search_tool(tool_name, self.extra_conflict_names)

    # ------------------------------------------------------------------ 主流程
    async def search(
        self,
        context: Any,
        umo: str | None,
        query: str,
        max_results: int | None = None,
        *,
        search_provider_id: str | None = None,
    ) -> SearchOutcome:
        """执行一次原生网页搜索。

        Args:
            context: AstrBot 插件 Context（用于解析 provider）。
            umo: unified_msg_origin，用于会话级限流与隔离。
            query: 搜索查询。
            max_results: 覆盖配置的结果条数。
            search_provider_id: 可选的固定搜索模型 provider id（来自 profile 的
                `provider_settings.websearch_native_provider_id`）；非空时优先于插件配置。
        """
        text = (query or "").strip()
        if not text:
            raise SearchError("query 不能为空。")

        if self.disabled:
            raise SearchError("模型原生网页搜索插件已被禁用（插件配置：禁用网页搜索=开）。")

        provider_id, provider, model, responses = await self._resolve_provider(
            context, umo, search_provider_id
        )

        limit = _coerce_int(
            max_results if max_results is not None else self.max_results,
            self.max_results,
            low=1,
            high=10,
        )

        cache_key = self._cache_key(provider_id, model, text, limit)
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.info(
                "[model_websearch] 命中缓存 provider=%s model=%s results=%d",
                provider_id,
                model,
                len(cached.results),
            )
            return cached

        self._check_rate_limit(umo)

        started = time.monotonic()
        request = {
            "model": model,
            "input": text,
            "instructions": self._instructions(limit, self.include_answer),
            "tools": [
                {
                    "type": "web_search",
                    "search_context_size": self.search_context_size,
                }
            ],
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        try:
            response = await asyncio.wait_for(
                responses.create(**request), timeout=self.timeout_seconds
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise SearchError(
                f"搜索超时（>{self.timeout_seconds}s，provider={provider_id} model={model}）。"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
            raise self._map_error(exc, provider_id, model) from exc

        outcome = self._parse_response(response, provider_id, model, limit)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        outcome.elapsed_ms = elapsed_ms
        usage = _as_dict(getattr(response, "usage", None))
        logger.info(
            "[model_websearch] provider=%s model=%s elapsed=%.1fs results=%d queries=%d usage=%s",
            provider_id,
            model,
            elapsed_ms / 1000,
            len(outcome.results),
            len(outcome.queries),
            {
                "input": usage.get("input_tokens"),
                "output": usage.get("output_tokens"),
                "total": usage.get("total_tokens"),
            },
        )
        self._cache_put(cache_key, outcome)
        return outcome

    # ------------------------------------------------------------- provider
    async def _resolve_provider(
        self, context: Any, umo: str | None, search_provider_id: str | None = None
    ) -> tuple[str, Any, str, Any]:
        """解析实际要使用的 provider。

        优先级：本轮显式指定的 search_provider_id（来自 profile 设置）> 插件配置的
        fixed 模式 > 当前会话聊天模型。显式指定为空/不存在时明确报错，不静默回落。
        """
        override = str(search_provider_id or "").strip()
        if override:
            provider = context.get_provider_by_id(override)
            if provider is None:
                raise SearchError(f"指定的搜索模型 provider 不存在：{override}")
        elif self.provider_mode == "fixed":
            provider_id = self.fixed_provider_id
            if not provider_id:
                raise SearchError(
                    "固定搜索模型模式（provider_mode=fixed）下 provider_id 为空；"
                    "请填写 provider_id（不会自动回落当前会话模型）。"
                )
            provider = context.get_provider_by_id(provider_id)
            if provider is None:
                raise SearchError(f"指定的固定搜索模型 provider 不存在：{provider_id}")
        else:
            try:
                provider = await context.get_using_provider_async(umo=umo)
            except Exception as exc:  # noqa: BLE001
                raise SearchError(f"获取当前会话聊天模型失败：{exc}") from exc
            if provider is None:
                raise SearchError("当前会话没有可用的聊天模型 provider。")

        provider_id = str(
            getattr(provider, "provider_config", {}).get("id")
            if isinstance(getattr(provider, "provider_config", None), dict)
            else ""
        ).strip() or override or self.fixed_provider_id or "unknown"
        model = ""
        get_model = getattr(provider, "get_model", None)
        if callable(get_model):
            try:
                model = str(get_model() or "")
            except Exception:  # noqa: BLE001
                model = ""
        if not model:
            config = getattr(provider, "provider_config", {})
            if isinstance(config, dict):
                model = str(config.get("model") or "")
        model = model or "unknown"

        client = getattr(provider, "client", None)
        responses = getattr(client, "responses", None) if client is not None else None
        if responses is None or not hasattr(responses, "create"):
            provider_type = ""
            config = getattr(provider, "provider_config", {})
            if isinstance(config, dict):
                provider_type = str(config.get("type") or "")
            raise SearchError(
                f"当前聊天模型（provider={provider_id} model={model} type={provider_type}）"
                "不支持 OpenAI Responses API 的原生网页搜索。"
                "请切换到支持该能力的模型（如本机 example-provider 的 model-a），"
                "或在插件配置里把 provider_mode 设为 fixed 并指定支持 Responses API 的 provider_id。"
            )
        return provider_id, provider, model, responses

    # --------------------------------------------------------------- 解析响应
    def _parse_response(
        self,
        response: Any,
        provider_id: str,
        model: str,
        limit: int,
    ) -> SearchOutcome:
        payload = _as_dict(response)
        output = payload.get("output") if payload else getattr(response, "output", None)
        output = output or []

        queries: list[str] = []
        citations: list[dict[str, str]] = []
        texts: list[str] = []
        search_calls = 0

        for item in output:
            item_dict = _as_dict(item)
            item_type = item_dict.get("type")
            if item_type == "web_search_call":
                search_calls += 1
                action = _as_dict(item_dict.get("action"))
                raw_queries = action.get("queries")
                if isinstance(raw_queries, list):
                    queries.extend(str(q) for q in raw_queries if str(q).strip())
                elif action.get("query"):
                    queries.append(str(action["query"]))
                for source in action.get("sources") or []:
                    source_dict = _as_dict(source)
                    url = str(source_dict.get("url") or "").strip()
                    if url:
                        citations.append(
                            {"title": str(source_dict.get("title") or ""), "url": url}
                        )
            elif item_type == "message":
                for part in item_dict.get("content") or []:
                    part_dict = _as_dict(part)
                    text = str(part_dict.get("text") or "")
                    if text:
                        texts.append(text)
                    for annotation in part_dict.get("annotations") or []:
                        ann = _as_dict(annotation)
                        url = str(ann.get("url") or "").strip()
                        if url:
                            citations.append(
                                {"title": str(ann.get("title") or ""), "url": url}
                            )

        raw_text = "\n".join(texts).strip()
        parsed = self._try_parse_json(raw_text)

        results: list[dict[str, str]] = []
        answer = ""
        if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
            for row in parsed["results"][:limit]:
                row_dict = _as_dict(row)
                url = str(row_dict.get("url") or "").strip()
                if not url:
                    continue
                results.append(
                    {
                        "title": str(row_dict.get("title") or ""),
                        "url": url,
                        "snippet": str(row_dict.get("snippet") or ""),
                    }
                )
            answer = str(parsed.get("answer") or "").strip()

        if not results:
            seen: set[str] = set()
            for citation in citations:
                url = citation["url"]
                if url in seen:
                    continue
                seen.add(url)
                results.append(
                    {"title": citation["title"], "url": url, "snippet": ""}
                )
                if len(results) >= limit:
                    break

        # 模型没有按 JSON 返回时，纯文本就是它基于检索给出的回答。
        if not answer and raw_text and not isinstance(parsed, dict):
            answer = raw_text[:500]

        if search_calls == 0 and not citations:
            raise SearchError(
                "模型没有实际执行网页搜索（响应里没有 web_search_call，也没有引用来源）。"
                "请确认当前模型支持原生 web_search，或改用其它搜索提供商。"
            )
        if not results and not answer:
            raise SearchError("原生搜索没有返回可用结果。")

        # 去重、限长、补 index（与 AstrBot 官方工具返回格式保持一致，方便模型引用）
        deduped: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for row in results:
            url = row["url"]
            if url in seen_urls or not url.startswith(_URL_ALLOWED):
                continue
            seen_urls.add(url)
            deduped.append(row)
            if len(deduped) >= limit:
                break

        ref_uuid = str(uuid.uuid4())[:4]
        for index, row in enumerate(deduped, 1):
            row["index"] = f"{ref_uuid}.{index}"

        deduped_queries: list[str] = []
        seen_queries: set[str] = set()
        for query in queries:
            query = query.strip()
            if query and query not in seen_queries:
                seen_queries.add(query)
                deduped_queries.append(query)

        deduped_sources: list[dict[str, str]] = []
        seen_sources: set[str] = set()
        for citation in citations:
            url = citation["url"]
            if url in seen_sources or not url.startswith(_URL_ALLOWED):
                continue
            seen_sources.add(url)
            deduped_sources.append(citation)
        if not deduped_sources:
            # 部分 provider 只在返回结果里给链接、不给 url_citation；用结果链接补全 sources。
            deduped_sources = [
                {"title": row["title"], "url": row["url"]} for row in deduped
            ]

        return SearchOutcome(
            results=deduped,
            answer=answer,
            queries=deduped_queries,
            sources=deduped_sources,
            provider_id=provider_id,
            model=model,
            elapsed_ms=0,
        )

    @staticmethod
    def _try_parse_json(text: str) -> Any:
        if not text:
            return None
        cleaned = _JSON_FENCE.sub("", text).strip()
        candidates = [cleaned]
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            candidates.append(cleaned[start : end + 1])
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    @staticmethod
    def _instructions(limit: int, include_answer: bool = False) -> str:
        if include_answer:
            return (
                "你是网页搜索后端。必须先调用 web_search 工具检索最新信息，"
                "再基于检索结果整理成单个 JSON 对象：\n"
                '{"results":[{"title":"标题","url":"来源链接","snippet":"摘要"}],'
                '"answer":"基于检索结果的简短回答（中文，不超过200字）"}\n'
                f"results 最多 {limit} 条，snippet 不超过 120 字。"
                "不要输出 JSON 以外的任何文字，不要编造检索结果。"
            )
        return (
            "你是网页搜索后端。必须先调用 web_search 工具检索最新信息。"
            "只返回检索到的网页结果，不要写总结、不要解释。输出单个 JSON：\n"
            '{"results":[{"title":"标题","url":"source url","snippet":"不超过80字的摘要"}],'
            '"answer":""}\n'
            f"results 最多 {limit} 条；不要输出 JSON 以外的任何文字。"
        )

    # ------------------------------------------------------------------ 错误
    @staticmethod
    def _map_error(exc: Exception, provider_id: str, model: str) -> SearchError:
        status = getattr(exc, "status_code", None)
        summary = f"{type(exc).__name__}: {str(exc)[:300]}"
        if status in {400, 404, 405, 501}:
            return SearchError(
                f"当前聊天模型或代理不支持原生网页搜索（provider={provider_id} "
                f"model={model}，HTTP {status}）。可改用官方搜索提供商，"
                "或在插件配置里把 provider_mode 设为 fixed 并指定支持 Responses API 的模型。"
            )
        if status in {401, 403}:
            return SearchError(
                f"聊天模型 provider 鉴权失败（provider={provider_id} model={model}，HTTP {status}）。"
            )
        if status == 429:
            return SearchError(
                f"聊天模型 provider 限流（provider={provider_id} model={model}，HTTP 429），请稍后再试。"
            )
        return SearchError(f"原生网页搜索调用失败（provider={provider_id} model={model}）：{summary}")

    # ------------------------------------------------------------- 缓存/限流
    def _cache_key(self, provider_id: str, model: str, query: str, limit: int) -> str:
        return "|".join(
            [provider_id, model, query.strip().lower(), str(limit), self.search_context_size]
        )

    def _cache_get(self, key: str) -> SearchOutcome | None:
        if self.cache_ttl_seconds <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, outcome = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(key, None)
            return None
        return outcome

    def _cache_put(self, key: str, outcome: SearchOutcome) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        if len(self._cache) >= 256:
            # 简单淘汰：清掉最早过期的 64 条，避免无限增长。
            for old_key in list(self._cache)[:64]:
                self._cache.pop(old_key, None)
        self._cache[key] = (time.monotonic() + self.cache_ttl_seconds, outcome)

    def _check_rate_limit(self, umo: str | None) -> None:
        if self.rate_limit_per_minute <= 0:
            return
        key = umo or "__global__"
        now = time.monotonic()
        bucket = self._rate.setdefault(key, deque())
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= self.rate_limit_per_minute:
            raise SearchError(
                f"搜索频率超限（{self.rate_limit_per_minute} 次/分钟），请稍后再试。"
            )
        bucket.append(now)
