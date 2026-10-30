"""模型原生网页搜索：provider 解析、Responses API 调用、结果解析、缓存与限流。

设计要点：
- 只依赖 AstrBot 自带依赖（openai / aiohttp），不引入新包。
- provider 的 OpenAI 客户端（provider.client）优先复用，保证 api_base / key / 代理 / 自定义 header 与聊天请求一致。
- 任何失败都转成 SearchError，由调用方变成模型可读的「错误：…」文本，不让异常打断整轮对话。
- 日志只记录 provider id、模型、耗时、token 用量与 query 预处理结果（原 query/处理后，
  各截 200 字），不记录 API Key、不抄响应正文。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from astrbot.api import logger

try:  # Python 3.9+ 自带 zoneinfo；极端环境缺失时退回本地时间并在首次使用时告警。
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 仅缺 tz 数据库的极端环境
    ZoneInfo = None  # type: ignore[assignment]

# AstrBot 本体的哨兵值：插件只在这个值下接管网页搜索工具。
SENTINEL_PROVIDER = "model_native"
SEARCH_TOOL_NAME = "web_search_model_native"

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_URL_ALLOWED = ("http://", "https://")

# Gemini 原生搜索后端（方案 B）相关常量。
# 端点：{root}/v1beta/models/{model}:generateContent；root 由 provider 的 base_url 去掉 /v1 得到。
_GEMINI_NATIVE_PATH = "/v1beta/models/{model}:generateContent"
# 安全：grounding 来源里的 vertexaisearch 跳转域名，只有该域名允许首跳还原。
_VERTEX_HOST = "vertexaisearch.cloud.google.com"
_VERTEX_MAX_ITEMS = 5
_VERTEX_TIMEOUT_SECONDS = 10.0

# S1/S2/S6/S10：时间基准统一用北京时间（UTC+8）。
_BEIJING_TZ = "Asia/Shanghai"
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_ZONEINFO_WARNED = False

# S1：query 预处理 —— 剥离 `site:域名/路径` 操作符。
_SITE_OPERATOR_RE = re.compile(r"\bsite[:：]\s*\S+", re.IGNORECASE)
_URL_TOKEN_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# 英文绝对日期（October 31 / Nov 1, 2026）：S7 复核时去掉，避免把预先写死的日期继续带偏后端。
_EN_MONTH_DATE_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s*\d{1,2}(?:st|nd|rd|th)?(?:,?\s*20\d{2})?",
    re.IGNORECASE,
)

# S10：query 质量告警 —— 「今天/today」+ 外文绝对日期/星期，或带 site:。
_TODAY_WORD_RE = re.compile(r"今天|今日|today", re.IGNORECASE)
_FOREIGN_DATE_HINT_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"
    r"|周[一二三四五六日天]|星期[一二三四五六日天]",
    re.IGNORECASE,
)

# S7：可疑「没有比赛/无赛程」结果识别。出现这些措辞、且结果里没有明确的
# 「A vs B / A 对阵 B」时，认为可能踩了日期口径（当地 vs 北京）误判，自动复核 1 次。
_SUSPICIOUS_NO_RESULT_PHRASES = (
    "no matches",
    "no match",
    "no games",
    "no scheduled",
    "no schedule",
    "no fixtures",
    "no events",
    "没有比赛",
    "没有场次",
    "没有赛事",
    "无赛程",
    "没有赛程",
    "暂无赛程",
    "今天没赛",
)
_MATCHUP_RE = re.compile(
    r"[A-Z0-9]{2,6}\s*(?:vs\.?|v\.?s\.?|对阵|🆚)\s*[A-Z0-9]{2,6}", re.IGNORECASE
)

# S11：按句截断 answer 时认可的句末字符。
_SENTENCE_END_CHARS = "。！？!?；;…"

# S12：官方/权威来源域名（含子域名），排序时优先，便于主模型引用可靠来源。
_OFFICIAL_DOMAINS = (
    "lolesports.com",
    "liquipedia.net",
    "wikipedia.org",
    "leagueoflegends.com",
    "riotgames.com",
    "op.gg",
    "escharts.com",
    "lol.fandom.com",
    "gol.gg",
    "dotesports.com",
)

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
    """搜索失败。消息会直接展示给模型/用户，不包含密钥。

    `debug` 是给日志/排障用的结构化上下文（provider、model、耗时、响应 output
    类型序列等），不进入成功路径的返回结构，也不含任何密钥或响应正文。
    """

    def __init__(self, message: str, *, debug: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.debug: dict[str, Any] = dict(debug) if debug else {}


@dataclass
class SearchOutcome:
    """一次搜索的规范化结果。

    `backend` / `evidence` / `instructions_mode` / `notice` / `usage` 是 0.2.3 新增的
    内部排障字段：只用于日志与 Gemini 原生后端标记，`to_payload` 仅在原生/回退后端
    时附带，保证旧 Responses 路径的对外结构不变（向后兼容）。
    """

    results: list[dict[str, str]]
    answer: str
    queries: list[str]
    sources: list[dict[str, str]]
    provider_id: str
    model: str
    elapsed_ms: int
    evidence: str = ""
    backend: str = ""
    instructions_mode: str = ""
    notice: str = ""
    usage: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "results": self.results,
            "answer": self.answer,
            "queries": self.queries,
            "sources": self.sources,
            "provider": {"id": self.provider_id, "model": self.model},
            "elapsed_ms": self.elapsed_ms,
        }
        # 旧 Responses 路径（backend=responses）保持键集合不变；原生与回退后端才带标记。
        if self.backend and self.backend != "responses":
            payload["backend"] = self.backend
            if self.evidence:
                payload["evidence"] = self.evidence
        if self.notice:
            payload["notice"] = self.notice
        return payload

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


def _decode_json_bytes(raw: bytes) -> Any:
    """把响应体解码成 JSON；不是 JSON 时退回文本（错误信息里只截前 200 字）。"""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随 3xx，便于读取 vertexaisearch 首跳的 Location。"""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:  # noqa: ANN401, D401
        return None


def _response_output_debug(response: Any) -> dict[str, Any]:
    """从响应里提取排障用的轻量上下文（只留类型/数量，不留正文，避免泄漏或日志膨胀）。

    Returns:
        含 output_types（按出现顺序的类型序列）、output_items（总项数）、
        output_counts（各类型计数），以及可选的 status / incomplete_details。
    """
    payload = _as_dict(response)
    output = payload.get("output") if payload else getattr(response, "output", None)
    output = output or []

    output_types: list[str] = []
    output_counts: dict[str, int] = {}
    for item in output:
        item_dict = _as_dict(item)
        item_type = str(
            item_dict.get("type") or getattr(item, "type", "") or type(item).__name__
        )
        output_types.append(item_type)
        output_counts[item_type] = output_counts.get(item_type, 0) + 1

    debug: dict[str, Any] = {
        "output_types": output_types,
        "output_counts": output_counts,
        "output_items": len(output),
    }
    status = payload.get("status") if payload else getattr(response, "status", None)
    if status:
        debug["status"] = str(status)
    incomplete = (
        payload.get("incomplete_details")
        if payload
        else getattr(response, "incomplete_details", None)
    )
    if incomplete:
        incomplete_dict = _as_dict(incomplete)
        debug["incomplete_details"] = incomplete_dict or str(incomplete)[:200]
    return debug


def _format_failure_context(debug: dict[str, Any]) -> str:
    """把失败上下文压成一行给模型/日志看的中文诊断串。"""
    parts = [
        f"provider={debug.get('provider_id') or 'unknown'}",
        f"model={debug.get('model') or 'unknown'}",
        f"elapsed={(int(debug.get('elapsed_ms') or 0)) / 1000:.1f}s",
    ]
    if debug.get("backend"):
        parts.append(f"backend={debug['backend']}")
    if debug.get("instructions_mode"):
        parts.append(f"instructions_mode={debug['instructions_mode']}")
    output_types = debug.get("output_types")
    if output_types is not None:
        types = list(output_types)
        joined = ", ".join(str(item) for item in types)
        parts.append(f"output=[{joined}]（{debug.get('output_items', len(types))} 项）")
    if debug.get("grounding"):
        parts.append(f"grounding={debug['grounding']}")
    if debug.get("endpoint"):
        parts.append(f"endpoint={debug['endpoint']}")
    if debug.get("status"):
        parts.append(f"status={debug['status']}")
    if debug.get("incomplete_details"):
        parts.append(f"incomplete_details={debug['incomplete_details']}")
    if debug.get("finish_reason"):
        parts.append(f"finish_reason={debug['finish_reason']}")
    return " ".join(parts)


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


# ---------------------------------------------------------- 时间 / query 预处理
def _beijing_now() -> datetime:
    """返回当前北京时间；zoneinfo/tzdata 不可用时退回本地时间并告警一次。"""
    global _ZONEINFO_WARNED
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(_BEIJING_TZ))
        except Exception as exc:  # noqa: BLE001 - tzdata 缺失/系统时区库异常
            if not _ZONEINFO_WARNED:
                _ZONEINFO_WARNED = True
                logger.warning(
                    "[model_websearch] 无法加载时区 %s（%s），退回本地时间", _BEIJING_TZ, exc
                )
    return datetime.now()


def format_time_context(now: datetime | None = None) -> str:
    """S1/S2/S6：注入给后端的「当前北京时间 + 日期 + 星期」上下文。"""
    current = now or _beijing_now()
    return (
        f"当前北京时间 {current:%Y-%m-%d %H:%M} "
        f"{_WEEKDAY_CN[current.weekday()]}（UTC+8）"
    )


def time_context_date(now: datetime | None = None) -> str:
    """缓存 bucketing 用的北京日期（同一天内允许缓存命中，跨天自动失效）。"""
    current = now or _beijing_now()
    return f"{current:%Y-%m-%d}"


def strip_site_operators(query: str) -> tuple[str, list[str]]:
    """S1：移除 query 里的 `site:域名/路径`，返回 (处理后的 query, 被移除的项)。"""
    original = query or ""
    removed = [match.group(0) for match in _SITE_OPERATOR_RE.finditer(original)]
    cleaned = _SITE_OPERATOR_RE.sub(" ", original)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip("，,；;、 ")
    return cleaned, removed


def simplify_query(query: str) -> str:
    """S7：复核用的简化 query —— 去 URL/`site:`/英文绝对日期，压缩空白并限长。

    只去掉最容易把后端带偏的「预先写死的外文日期」与站点锁；赛事名、年份等关键词保留。
    """
    text = _URL_TOKEN_RE.sub(" ", query or "")
    text, _ = strip_site_operators(text)
    text = _EN_MONTH_DATE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" ，,；;、")
    text = text[:160].strip()
    return text or (query or "").strip()[:160]


def detect_query_quality_issues(query: str) -> list[str]:
    """S10：检测高风险 query（只告警，不改变行为）。"""
    text = query or ""
    if not _TODAY_WORD_RE.search(text):
        return []
    issues: list[str] = []
    if _FOREIGN_DATE_HINT_RE.search(text):
        issues.append("含「今天/today」+ 绝对日期/星期（主模型可能已预先算错日期）")
    if _SITE_OPERATOR_RE.search(text):
        issues.append("含 site: 操作符（会把检索锁死到单一站点）")
    return issues


def truncate_answer_sentences(text: str, limit: int) -> str:
    """S11：按句截断 answer（尽量停在句末），硬上限 `limit` 字符。"""
    body = text or ""
    if limit <= 0 or len(body) <= limit:
        return body
    window = body[:limit]
    cut = max(window.rfind(char) for char in _SENTENCE_END_CHARS)
    if cut >= max(1, limit // 2):
        return window[: cut + 1].rstrip()
    for separator in ("\n", " "):
        cut = window.rfind(separator)
        if cut >= max(1, limit // 2):
            return window[:cut].rstrip()
    return window.rstrip()


def is_official_source(url: str) -> bool:
    """S12：URL 是否属于官方/权威来源域名（含子域名）。"""
    try:
        host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in _OFFICIAL_DOMAINS)


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
        # Gemini 原生搜索后端（方案 B）：auto=解析出的模型名含 gemini 时走 /v1beta grounding。
        backend_mode = str(self.config.get("gemini_search_backend") or "auto").strip().lower()
        self.gemini_search_backend = (
            backend_mode if backend_mode in {"auto", "native", "responses"} else "auto"
        )
        # 原生调用失败（HTTP/超时/无 grounding）时是否回退到 Responses + hybrid instructions。
        self.gemini_native_fallback_to_responses = _coerce_bool(
            self.config.get("gemini_native_fallback_to_responses"), True
        )
        # Gemini 原生与 fallback 的有效超时（argon 实测 53.9s，45s 不够）。
        self.gemini_timeout_seconds = _coerce_int(
            self.config.get("gemini_timeout_seconds"), 90, low=5, high=300
        )
        # Gemini 原生 thinkingBudget（G-2026-10-31）：128 实测比默认更快且保持正确率；0=不发送。
        self.gemini_thinking_budget = _coerce_int(
            self.config.get("gemini_thinking_budget"), 128, low=0, high=4096
        )
        empty_policy = str(self.config.get("empty_result_policy") or "notice").strip().lower()
        self.empty_result_policy = (
            empty_policy if empty_policy in {"notice", "error"} else "notice"
        )
        # 是否把 vertexaisearch 跳转还原成真实 URL（仅该域名、最多 5 条、2s/条）。
        self.vertex_redirect_resolve = _coerce_bool(
            self.config.get("vertex_redirect_resolve"), False
        )
        # S1：调用搜索前注入「当前北京时间/日期+星期」，并剥离 site: 操作符。
        # 只作用于发给搜索后端的请求，不改变工具返回给主模型的结构与语义。
        self.query_context_inject = _coerce_bool(
            self.config.get("query_context_inject"), True
        )
        self.strip_site_operator = _coerce_bool(
            self.config.get("strip_site_operator"), True
        )
        # S7：结果疑似「没有比赛/无赛程」或无结果时，用北京时间前缀 + 简化 query 复核重试 1 次。
        self.suspicious_result_retry = _coerce_bool(
            self.config.get("suspicious_result_retry"), True
        )
        # S11：native answer 按句截断的字符上限（默认 800，替代旧的硬截 500）。
        self.answer_max_chars = _coerce_int(
            self.config.get("answer_max_chars"), 800, low=100, high=4000
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

    # ------------------------------------------------------- query 预处理/告警
    def _prepare_query(self, query: str) -> tuple[str, dict[str, Any]]:
        """S1：只预处理「发给搜索后端」的 query。

        注入北京时间上下文、剥离 `site:`，用户原话保留在注入前缀之后。
        返回 (发给后端的 query, query_guard 排障字段)；开关关闭时原样透传。
        """
        original = (query or "").strip()
        cleaned = original
        removed: list[str] = []
        if self.strip_site_operator:
            cleaned, removed = strip_site_operators(original)
            if not cleaned:
                # query 只有 site: 时不能把空串发给后端：退回原话，宁可保留操作符。
                cleaned = original
        prefix = f"{format_time_context()}。用户问题：" if self.query_context_inject else ""
        prepared = f"{prefix}{cleaned}" if prefix else cleaned
        guard: dict[str, Any] = {
            "original": original,
            "prepared": prepared,
            "inject_time": self.query_context_inject,
            "strip_site": self.strip_site_operator,
            "stripped_site": removed,
            "changed": prepared != original,
        }
        logger.info(
            "[model_websearch] query_guard original=%r prepared=%r "
            "inject_time=%s strip_site=%s stripped_site=%s",
            original[:200],
            prepared[:200],
            guard["inject_time"],
            guard["strip_site"],
            removed or "-",
        )
        return prepared, guard

    def _build_retry_query(self, query: str) -> str:
        """S7：复核重试用 query —— 去 `site:`/英文绝对日期后，再补北京时间前缀。"""
        simplified = simplify_query(query) or (query or "").strip()
        if self.query_context_inject:
            return f"{format_time_context()}。用户问题：{simplified}"
        return simplified

    @staticmethod
    def _warn_query_quality(query: str) -> None:
        """S10：记录高风险 query 告警（不改行为）。"""
        issues = detect_query_quality_issues(query)
        if not issues:
            return
        logger.warning(
            "[model_websearch] query_guard 质量告警 query=%r issues=%s"
            "（建议用中文自然语言/用户原话，不要预换算日期、不要加 site:）",
            (query or "")[:200],
            "；".join(issues),
        )

    def _retry_reason(self, outcome: SearchOutcome) -> str:
        """S7：返回需要自动复核的原因；空字符串表示结果看起来正常。"""
        if not outcome.results:
            return "no_results"
        return self._suspicious_no_result_reason(outcome)

    def _suspicious_no_result_reason(self, outcome: SearchOutcome) -> str:
        """结果/answer 出现「没有比赛/无赛程」且没有明确对阵时，返回命中的措辞。"""
        text = " ".join(
            [outcome.answer or ""]
            + [
                f"{row.get('title', '')} {row.get('snippet', '')}"
                for row in outcome.results
            ]
        )
        lowered = text.lower()
        hit = next(
            (phrase for phrase in _SUSPICIOUS_NO_RESULT_PHRASES if phrase in lowered),
            "",
        )
        if not hit:
            return ""
        if _MATCHUP_RE.search(text):
            # 已有明确对阵（A vs B），说明结果不是「整天空赛程」，不触发复核。
            return ""
        return hit

    @staticmethod
    def _pick_retry_outcome(
        first: SearchOutcome, second: SearchOutcome
    ) -> SearchOutcome:
        """两次结果都不可靠时，保留结果更多的那份（同样多则保留首次）。"""
        return second if len(second.results) > len(first.results) else first

    def _finalize_retry_outcome(
        self,
        outcome: SearchOutcome,
        reason: str,
        retry_attempted: int,
        retry_improved: bool,
    ) -> None:
        """S7：复核后仍无更可靠结果时按 `empty_result_policy` 处理。"""
        if retry_improved:
            return
        if self.empty_result_policy == "error" and not outcome.results:
            raise SearchError(
                f"搜索自动复核后仍没有可用结果（首次触发原因：{reason}）。",
                debug={
                    "backend": outcome.backend or "unknown",
                    "provider_id": outcome.provider_id,
                    "model": outcome.model,
                    "retry": retry_attempted,
                    "retry_reason": reason,
                },
            )
        notice = (
            f"首次结果触发自动复核（原因：{reason}），用北京时间前缀 + 简化 query "
            "重试 1 次后仍没有更可靠的结果；请不要把来源地日期当作北京时间今天，"
            "若仍不确定请说明没有检索到明确信息，不要编造。"
        )
        outcome.notice = f"{outcome.notice}；{notice}" if outcome.notice else notice

    @staticmethod
    def _sort_results_official(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        """S12：官方/权威域名优先（稳定排序，同组内保持后端返回顺序）。"""
        return sorted(
            rows,
            key=lambda row: 0 if is_official_source(str(row.get("url") or "")) else 1,
        )

    def _reorder_results_official(
        self, rows: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """S12：Responses/fallback 解析完成后按官方优先重排 results。

        `_parse_response` 的 `index` 是按原顺序分配的；重排后同步把 `index` 的序号部分
        改成展示位置，保持「index 与顺序一致」（不新增/删除任何字段）。
        """
        ordered = self._sort_results_official(rows)
        if [row.get("url") for row in ordered] == [row.get("url") for row in rows]:
            return ordered
        prefix = ""
        for row in ordered:
            index = str(row.get("index") or "")
            if "." in index:
                prefix = index.split(".", 1)[0]
                break
        if not prefix:
            return ordered
        for position, row in enumerate(ordered, 1):
            if "index" in row:
                row["index"] = f"{prefix}.{position}"
        return ordered

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

        内部按配置分派后端：
        - `gemini_search_backend=auto` 且解析出的模型名含 gemini → Gemini 原生
          `/v1beta:generateContent` + `groundingMetadata`（方案 B）；
        - 其它情况保持 v0.2.2 的 Responses API 路径（请求体与解析不变）。

        0.2.4 行为：
        - S1：发给后端的 query 先注入北京时间上下文、剥离 `site:`（工具返回结构不变）；
        - S7：结果疑似「没有比赛/无赛程」或无结果时自动复核 1 次（每个 search() 最多 1 次）。

        Args:
            context: AstrBot 插件 Context（用于解析 provider）。
            umo: unified_msg_origin，用于会话级限流与隔离。
            query: 搜索查询（主模型/用户原查询）。
            max_results: 覆盖配置的结果条数。
            search_provider_id: 可选的固定搜索模型 provider id（来自 profile 的
                `provider_settings.websearch_native_provider_id`）；非空时优先于插件配置。
        """
        original_text = (query or "").strip()
        if not original_text:
            raise SearchError("query 不能为空。")

        if self.disabled:
            raise SearchError("模型原生网页搜索插件已被禁用（插件配置：禁用网页搜索=开）。")

        provider_id, provider, model = await self._resolve_provider(
            context, umo, search_provider_id
        )

        limit = _coerce_int(
            max_results if max_results is not None else self.max_results,
            self.max_results,
            low=1,
            high=10,
        )

        # S10：query 质量告警（只记录，不改变行为）。
        self._warn_query_quality(original_text)
        # S1：只改「发给后端的请求」；缓存/返回结构仍以原 query 语义为准。
        backend_text, query_guard = self._prepare_query(original_text)

        use_native = self._should_use_gemini_native(model)
        cache_scope = "gemini_native" if use_native else "responses"
        time_bucket = time_context_date() if self.query_context_inject else ""
        cached = self._cache_get(
            self._cache_key(
                provider_id, model, original_text, limit, cache_scope, time_bucket
            )
        )
        if cached is None and use_native:
            # 原生失败回退的结果按 responses_fallback 缓存：同一 query 下次直接命中。
            cached = self._cache_get(
                self._cache_key(
                    provider_id,
                    model,
                    original_text,
                    limit,
                    "responses_fallback",
                    time_bucket,
                )
            )
        if cached is not None:
            logger.info(
                "[model_websearch] 命中缓存 provider=%s model=%s backend=%s results=%d",
                provider_id,
                model,
                cached.backend or cache_scope,
                len(cached.results),
            )
            return cached

        self._check_rate_limit(umo)

        outcome = await self._run_backend(
            provider_id, provider, model, backend_text, limit, use_native
        )

        # S7：可疑结果自动复核（每个 search() 最多 1 次内部重试；失败计数规则不变）。
        retry_attempted = 0
        retry_improved = False
        retry_reason = ""
        if self.suspicious_result_retry:
            retry_reason = self._retry_reason(outcome)
            if retry_reason:
                retry_attempted = 1
                retry_text = self._build_retry_query(original_text)
                logger.warning(
                    "[model_websearch] 触发可疑结果自动复核 retry=1 reason=%s query=%r",
                    retry_reason,
                    retry_text[:200],
                )
                try:
                    retry_outcome = await self._run_backend(
                        provider_id, provider, model, retry_text, limit, use_native
                    )
                except SearchError as exc:
                    logger.warning(
                        "[model_websearch] 复核重试失败，保留首次结果 retry=1 "
                        "reason=%s debug=%s",
                        retry_reason,
                        getattr(exc, "debug", None) or "-",
                    )
                else:
                    retry_still = self._retry_reason(retry_outcome)
                    if not retry_still:
                        outcome = retry_outcome
                        retry_improved = True
                        logger.info(
                            "[model_websearch] 复核重试成功 retry=1 improved=True results=%d",
                            len(retry_outcome.results),
                        )
                    else:
                        outcome = self._pick_retry_outcome(outcome, retry_outcome)
                        logger.info(
                            "[model_websearch] 复核重试仍无更可靠结果 retry=1 "
                            "improved=False reason=%s",
                            retry_still,
                        )
                self._finalize_retry_outcome(
                    outcome, retry_reason, retry_attempted, retry_improved
                )

        direct_urls, vertex_urls = self._url_breakdown(outcome)
        logger.info(
            "[model_websearch] provider=%s model=%s elapsed=%.1fs results=%d queries=%d "
            "backend=%s instructions_mode=%s evidence=%s url_kinds=direct:%d vertex:%d usage=%s "
            "query_guard=%s retry=%d reason=%s improved=%s",
            provider_id,
            model,
            outcome.elapsed_ms / 1000,
            len(outcome.results),
            len(outcome.queries),
            outcome.backend or cache_scope,
            outcome.instructions_mode or "-",
            outcome.evidence or "-",
            direct_urls,
            vertex_urls,
            outcome.usage or "-",
            "changed" if query_guard.get("changed") else "unchanged",
            retry_attempted,
            retry_reason or "-",
            retry_improved,
        )
        self._cache_put(
            self._cache_key(
                provider_id,
                model,
                original_text,
                limit,
                outcome.backend or cache_scope,
                time_bucket,
            ),
            outcome,
        )
        return outcome

    async def _run_backend(
        self,
        provider_id: str,
        provider: Any,
        model: str,
        text: str,
        limit: int,
        use_native: bool,
    ) -> SearchOutcome:
        """按后端分派一次真实搜索调用（含 Gemini 原生失败回退 Responses）。"""
        if use_native:
            try:
                return await self._search_gemini_native(
                    provider_id, provider, model, text, limit
                )
            except SearchError as exc:
                debug = dict(exc.debug)
                debug.setdefault("provider_id", provider_id)
                debug.setdefault("model", model)
                debug.setdefault("backend", "gemini_native")
                debug.setdefault("instructions_mode", "native")
                if not self.gemini_native_fallback_to_responses:
                    message = f"{exc}（诊断：{_format_failure_context(debug)}）"
                    raise SearchError(message, debug=debug) from exc
                logger.warning(
                    "[model_websearch] Gemini 原生后端失败，回退 Responses：%s | debug=%s",
                    exc,
                    debug,
                )
                try:
                    return await self._search_via_responses(
                        provider_id,
                        provider,
                        model,
                        text,
                        limit,
                        timeout_seconds=self.gemini_timeout_seconds,
                        instructions_mode="hybrid",
                        backend="responses_fallback",
                    )
                except SearchError as fallback_exc:
                    debug2 = dict(fallback_exc.debug)
                    debug2.setdefault("provider_id", provider_id)
                    debug2.setdefault("model", model)
                    debug2.setdefault("backend", "responses_fallback")
                    debug2.setdefault("instructions_mode", "hybrid")
                    debug2["native_error"] = str(exc)[:300]
                    message = (
                        f"{fallback_exc}（Gemini 原生后端也失败：{exc}；"
                        f"诊断：{_format_failure_context(debug2)}）"
                    )
                    raise SearchError(message, debug=debug2) from fallback_exc
        return await self._search_via_responses(
            provider_id,
            provider,
            model,
            text,
            limit,
            timeout_seconds=self.timeout_seconds,
            instructions_mode="json",
            backend="responses",
        )

    # ------------------------------------------------------------- provider
    async def _resolve_provider(
        self, context: Any, umo: str | None, search_provider_id: str | None = None
    ) -> tuple[str, Any, str]:
        """解析实际要使用的 provider。

        优先级：本轮显式指定的 search_provider_id（来自 profile 设置）> 插件配置的
        fixed 模式 > 当前会话聊天模型。显式指定为空/不存在时明确报错，不静默回落。

        注意：这里不再强制要求 Responses API —— Gemini 原生后端只需要 provider 的
        `client.base_url` / `api_key`；Responses 能力在 `_responses_api` 里按需校验。
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
        return provider_id, provider, model

    def _responses_api(self, provider: Any, provider_id: str, model: str) -> Any:
        """确认 provider 暴露 OpenAI Responses API；只读，不改 provider。"""
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
        return responses

    def _should_use_gemini_native(self, model: str) -> bool:
        """按配置与模型名决定是否走 Gemini 原生 grounding 后端。"""
        if self.gemini_search_backend == "responses":
            return False
        if self.gemini_search_backend == "native":
            return True
        return "gemini" in (model or "").lower()

    # ---------------------------------------------------- Gemini 原生后端（方案 B）
    def _gemini_target(self, provider: Any, model: str) -> tuple[str, str]:
        """从 provider 推导 Gemini 原生端点与 API Key。

        Returns:
            (endpoint, api_key)；endpoint 不含查询串，密钥只进请求头/`?key=`。
        """
        client = getattr(provider, "client", None)
        base = str(getattr(client, "base_url", "") or "") if client is not None else ""
        config = getattr(provider, "provider_config", {})
        if not base and isinstance(config, dict):
            base = str(config.get("api_base") or config.get("base_url") or "")
        root = base.strip().rstrip("/")
        if root.lower().endswith("/v1"):
            root = root[: -len("/v1")].rstrip("/")
        if not root:
            raise SearchError(
                f"无法从 provider（model={model}）解析 Gemini 原生端点 base_url；"
                "可在插件配置里把 gemini_search_backend 设为 responses。",
                debug={"backend": "gemini_native", "grounding": "none"},
            )
        model_name = (model or "").strip()
        if model_name.lower().startswith("models/"):
            model_name = model_name[len("models/") :]
        if not model_name:
            raise SearchError(
                "无法确定 Gemini 模型名（model 为空）。",
                debug={"backend": "gemini_native"},
            )
        endpoint = root + _GEMINI_NATIVE_PATH.format(
            model=urllib.parse.quote(model_name, safe="")
        )
        api_key = ""
        raw_key = getattr(client, "api_key", None) if client is not None else None
        if raw_key is not None:
            get_secret = getattr(raw_key, "get_secret_value", None)
            api_key = str(get_secret() if callable(get_secret) else raw_key).strip()
        if not api_key and isinstance(config, dict):
            api_key = str(config.get("key") or config.get("api_key") or "").strip()
        return endpoint, api_key

    async def _post_json(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, Any, dict[str, str]]:
        """POST JSON 并返回 (HTTP 状态, 解析后的响应体, 响应头)；不抛 HTTPError。

        这是唯一的网络出口，离线单测直接替换本方法即可覆盖全部原生逻辑。
        """

        def _do() -> tuple[int, Any, dict[str, str]]:
            request = urllib.request.Request(
                url, data=body, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read()
                    return (
                        int(getattr(response, "status", 200)),
                        _decode_json_bytes(raw),
                        dict(response.headers),
                    )
            except urllib.error.HTTPError as exc:
                raw = exc.read() or b""
                return int(exc.code), _decode_json_bytes(raw), dict(exc.headers or {})

        return await asyncio.to_thread(_do)

    def _gemini_system_instruction(self, now: datetime | None = None) -> str:
        """S2：Gemini 原生 `systemInstruction` —— 北京时间基准 + 日期换算 + 防编造。"""
        return (
            "你是联网搜索助理，必须调用 google_search 检索后再回答。"
            f"当前时间基准：{format_time_context(now)}。规则：\n"
            "1. 用户说「今天/明天/这周/next」时，一律按北京时间（UTC+8，Asia/Shanghai）理解；"
            "「今天」就是上面给出的北京日期，不要用来源地日期替代。\n"
            "2. 涉及赛事/活动日期（Worlds、S 赛、美中/北美/欧洲当地时间等）时，"
            "必须同时给出「来源地当地日期时间」与「对应的北京日期时间」，"
            "例如「当地 10 月 30 日 15:00 CDT = 北京时间 10 月 31 日 04:00」。\n"
            "3. 严禁把来源地（美中 CDT/CST、欧洲、北美）的日期直接当作用户的「今天」；"
            "同一页面里不同日期行要逐条换算后再下结论。\n"
            "4. 优先使用官方/权威来源（lolesports.com、liquipedia.net、wikipedia.org 等）核对；"
            "不得编造赛程、对阵、时间或比分；检索不到就明确说没有找到。\n"
            "5. 若检索结果提示「没有比赛/无赛程」，先检查是不是日期口径（当地 vs 北京）"
            "造成的误判，别急着下「今天没有」的结论。"
        )

    async def _call_gemini_native(
        self, provider: Any, model: str, query: str
    ) -> tuple[Any, int, str, str]:
        """调用 Gemini 原生 generateContent，返回 (payload, status, endpoint, auth_mode)。"""
        endpoint, api_key = self._gemini_target(provider, model)
        native_body: dict[str, Any] = {
            "systemInstruction": {
                "parts": [{"text": self._gemini_system_instruction()}]
            },
            "contents": [{"parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
        }
        if self.gemini_thinking_budget > 0:
            native_body["generationConfig"] = {
                "thinkingConfig": {"thinkingBudget": self.gemini_thinking_budget}
            }
        body = json.dumps(native_body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        auth_mode = "none"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            auth_mode = "bearer"

        async def attempt(
            url: str, request_headers: dict[str, str]
        ) -> tuple[int, Any]:
            try:
                status, data, _ = await asyncio.wait_for(
                    self._post_json(
                        url, body, request_headers, self.gemini_timeout_seconds
                    ),
                    timeout=self.gemini_timeout_seconds,
                )
                return status, data
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise SearchError(
                    f"Gemini 原生搜索超时（>{self.gemini_timeout_seconds}s，model={model}）。",
                    debug={
                        "backend": "gemini_native",
                        "endpoint": endpoint,
                        "grounding": "none",
                    },
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
                raise SearchError(
                    f"Gemini 原生端点调用失败（model={model}）："
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                    debug={
                        "backend": "gemini_native",
                        "endpoint": endpoint,
                        "grounding": "none",
                    },
                ) from exc

        status, data = await attempt(endpoint, headers)
        if status in {401, 403} and api_key:
            # 部分反代只认 ?key=；Bearer 被拒时用同一端点重试一次。
            key_url = f"{endpoint}?key={urllib.parse.quote(api_key, safe='')}"
            status, data = await attempt(key_url, {"Content-Type": "application/json"})
            auth_mode = "key"
        if status != 200:
            detail = (
                data
                if isinstance(data, str)
                else json.dumps(data, ensure_ascii=False)
            )
            raise SearchError(
                f"Gemini 原生端点返回 HTTP {status}：{detail[:200]}",
                debug={
                    "backend": "gemini_native",
                    "endpoint": endpoint,
                    "status": status,
                    "grounding": "none",
                },
            )
        if not isinstance(data, dict):
            raise SearchError(
                "Gemini 原生端点返回了非 JSON 对象响应。",
                debug={
                    "backend": "gemini_native",
                    "endpoint": endpoint,
                    "status": status,
                    "grounding": "none",
                },
            )
        return data, status, endpoint, auth_mode

    async def _search_gemini_native(
        self,
        provider_id: str,
        provider: Any,
        model: str,
        text: str,
        limit: int,
    ) -> SearchOutcome:
        """执行一次 Gemini 原生搜索并解析 groundingMetadata。"""
        started = time.monotonic()
        payload, status, endpoint, auth_mode = await self._call_gemini_native(
            provider, model, text
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            outcome = self._parse_gemini_native_response(
                payload,
                provider_id,
                model,
                limit,
                status=status,
                endpoint=endpoint,
                auth_mode=auth_mode,
            )
        except SearchError as exc:
            debug = dict(exc.debug)
            debug.setdefault("provider_id", provider_id)
            debug.setdefault("model", model)
            debug.setdefault("backend", "gemini_native")
            debug.setdefault("elapsed_ms", elapsed_ms)
            debug.setdefault("status", status)
            debug.setdefault("endpoint", endpoint)
            raise SearchError(str(exc), debug=debug) from exc
        if self.vertex_redirect_resolve:
            resolved = await self._resolve_outcome_vertex(outcome)
            if resolved:
                logger.info(
                    "[model_websearch] vertex 跳转还原成功 %d 条", resolved
                )
                # S12：解析时 URL 还全是 vertexaisearch 跳转（等价于全部非官方），
                # 还原出真实域名后必须按官方优先再排一次，并同步 sources 与 index。
                outcome.results = self._reorder_results_official(outcome.results)
                outcome.sources = self._sync_native_sources(outcome.results)
        outcome.elapsed_ms = elapsed_ms
        outcome.instructions_mode = "native"
        return outcome

    @staticmethod
    def _sync_native_sources(
        results: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """S12：native 的 sources 与 results 严格对齐（保留 title/url/resolved）。"""
        return [
            {
                "title": row.get("title", ""),
                "url": row["url"],
                "resolved": bool(row.get("resolved", False)),
            }
            for row in results
        ]

    def _parse_gemini_native_response(
        self,
        payload: Any,
        provider_id: str,
        model: str,
        limit: int,
        *,
        status: int = 200,
        endpoint: str = "",
        auth_mode: str = "",
    ) -> SearchOutcome:
        """解析 Gemini 原生响应：queries / chunks / supports / answer → SearchOutcome。

        grounding 证据缺失时的策略：
        - 无 candidates 或 groundingMetadata → 直接抛 SearchError（grounding=none）；
        - 有 queries 但 chunks 为空 → 按 `empty_result_policy`：notice 返回空结果 + notice，
          error 抛「原生搜索没有返回可用结果」；
        - queries 与 chunks 都为空 → 无论策略都抛错（这次搜索没有可用证据）。
        """
        debug_base: dict[str, Any] = {
            "provider_id": provider_id,
            "model": model,
            "backend": "gemini_native",
            "endpoint": endpoint,
            "status": status,
            "auth_mode": auth_mode,
        }
        data = _as_dict(payload)
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise SearchError(
                "Gemini 原生搜索没有返回候选结果（candidates 为空）。",
                debug={**debug_base, "grounding": "none"},
            )
        candidate = _as_dict(candidates[0])
        finish_reason = str(candidate.get("finishReason") or "")
        debug_base["finish_reason"] = finish_reason
        grounding = _as_dict(candidate.get("groundingMetadata"))
        if not grounding:
            raise SearchError(
                "Gemini 原生搜索没有返回 groundingMetadata"
                "（该反代可能未透传 Gemini grounding，请确认 /v1beta 支持）。",
                debug={**debug_base, "grounding": "none"},
            )

        queries = [
            str(item).strip()
            for item in (grounding.get("webSearchQueries") or [])
            if str(item).strip()
        ]

        chunks: list[dict[str, str]] = []
        seen_chunks: set[str] = set()
        for raw_chunk in grounding.get("groundingChunks") or []:
            web = _as_dict(_as_dict(raw_chunk).get("web"))
            uri = str(web.get("uri") or "").strip()
            if not uri or uri in seen_chunks:
                continue
            seen_chunks.add(uri)
            chunks.append({"title": str(web.get("title") or "").strip(), "url": uri})

        supports: list[tuple[set[int], str]] = []
        for raw_support in grounding.get("groundingSupports") or []:
            support = _as_dict(raw_support)
            segment = _as_dict(support.get("segment"))
            indices = {
                int(index)
                for index in (support.get("groundingChunkIndices") or [])
                if isinstance(index, int)
            }
            supports.append((indices, str(segment.get("text") or "")))

        def _snippet_for(position: int) -> str:
            for indices, segment_text in supports:
                if position not in indices:
                    continue
                cleaned = re.sub(r"\s+", " ", segment_text).strip()
                if cleaned:
                    return cleaned[:120]
            return ""

        raw_results: list[dict[str, str]] = []
        for position, chunk in enumerate(chunks):
            url = chunk["url"]
            if not url.startswith(_URL_ALLOWED):
                continue
            raw_results.append(
                {
                    "title": chunk["title"],
                    "url": url,
                    "snippet": _snippet_for(position),
                    # S11：native 的 snippet 来自 groundingSupports 的答案句子，不是网页正文。
                    "snippet_source": "model_answer_segment",
                    # S12：vertex 跳转在还原前不算「来源已确定」。
                    "resolved": not self._is_vertex_uri(url),
                }
            )
        # S12：官方/权威域名优先（稳定排序），再按原逻辑去重/限长。
        results: list[dict[str, str]] = self._sort_results_official(raw_results)

        parts = _as_dict(candidate.get("content")).get("parts")
        if not parts:
            # 本机反代把 parts 直接放在 candidate 上（而非官方 content.parts）。
            parts = candidate.get("parts") or []
        texts = [str(_as_dict(part).get("text") or "") for part in parts]
        raw_answer = "\n".join(text for text in texts if text).strip()
        # S11：按句截断（上限 answer_max_chars，默认 800），替代旧的硬截 500。
        answer = truncate_answer_sentences(raw_answer, self.answer_max_chars)

        notice = ""
        if not results:
            grounding_state = "queries_only" if queries else "empty"
            if (not queries and not chunks) or self.empty_result_policy == "error":
                # 既没有检索词也没有来源，等于这次搜索没有可用证据：无论策略都报错。
                raise SearchError(
                    "原生搜索没有返回可用结果。",
                    debug={**debug_base, "grounding": grounding_state},
                )
            notice = (
                "Gemini 原生搜索已执行，但没有返回可展示的 grounding 来源"
                "（groundingChunks 为空）；answer 仅供参考。"
            )

        deduped: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for row in results:
            url = row["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            deduped.append(row)
            if len(deduped) >= limit:
                break

        deduped_queries: list[str] = []
        seen_queries: set[str] = set()
        for query in queries:
            if query in seen_queries:
                continue
            seen_queries.add(query)
            deduped_queries.append(query)

        # sources 与最终 results 对齐（含 S12 的 resolved 标记）。
        deduped_sources: list[dict[str, str]] = [
            {
                "title": row.get("title", ""),
                "url": row["url"],
                "resolved": bool(row.get("resolved", False)),
            }
            for row in deduped
        ]

        ref_uuid = str(uuid.uuid4())[:4]
        for index, row in enumerate(deduped, 1):
            row["index"] = f"{ref_uuid}.{index}"

        usage_meta = _as_dict(data.get("usageMetadata"))
        return SearchOutcome(
            results=deduped,
            answer=answer,
            queries=deduped_queries,
            sources=deduped_sources,
            provider_id=provider_id,
            model=model,
            elapsed_ms=0,
            evidence="gemini_grounding",
            backend="gemini_native",
            instructions_mode="native",
            notice=notice,
            usage={
                "input": usage_meta.get("promptTokenCount"),
                "output": usage_meta.get("candidatesTokenCount"),
                "total": usage_meta.get("totalTokenCount"),
            },
        )

    # ------------------------------------------------------ Responses 后端
    async def _search_via_responses(
        self,
        provider_id: str,
        provider: Any,
        model: str,
        text: str,
        limit: int,
        *,
        timeout_seconds: int,
        instructions_mode: str,
        backend: str,
    ) -> SearchOutcome:
        """现有 Responses API 路径（非 Gemini 与 Gemini fallback 共用）。

        请求体与 v0.2.2 一致；`instructions_mode=hybrid` 时换成“先自然语言回答、
        末尾输出 JSON”的指令（findings 实测 Gemini 在该形态下会返回结构化证据）。
        """
        responses = self._responses_api(provider, provider_id, model)
        instructions = (
            self._hybrid_instructions(limit)
            if instructions_mode == "hybrid"
            else self._instructions(limit, self.include_answer)
        )
        request = {
            "model": model,
            "input": text,
            "instructions": instructions,
            "tools": [
                {
                    "type": "web_search",
                    "search_context_size": self.search_context_size,
                }
            ],
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                responses.create(**request), timeout=timeout_seconds
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            raise SearchError(
                f"搜索超时（>{timeout_seconds}s，provider={provider_id} model={model}）。",
                debug={
                    "provider_id": provider_id,
                    "model": model,
                    "elapsed_ms": elapsed_ms,
                    "backend": backend,
                    "instructions_mode": instructions_mode,
                },
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
            error = self._map_error(exc, provider_id, model)
            error.debug.setdefault("provider_id", provider_id)
            error.debug.setdefault("model", model)
            error.debug.setdefault(
                "elapsed_ms", int((time.monotonic() - started) * 1000)
            )
            error.debug.setdefault("backend", backend)
            error.debug.setdefault("instructions_mode", instructions_mode)
            raise error from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            outcome = self._parse_response(response, provider_id, model, limit)
        except SearchError as exc:
            # 失败日志的关键上下文：谁（provider/model）、多久、响应里到底有什么类型。
            debug = dict(exc.debug)
            debug.update(
                {
                    "provider_id": provider_id,
                    "model": model,
                    "elapsed_ms": elapsed_ms,
                    "backend": backend,
                    "instructions_mode": instructions_mode,
                }
            )
            debug.update(_response_output_debug(response))
            message = f"{exc}（诊断：{_format_failure_context(debug)}）"
            raise SearchError(message, debug=debug) from exc

        # S12：官方/权威域名优先（Responses 与 Gemini fallback 同样适用；
        # `resolved` 标记仍只加在 native 结果上，因 Responses 没有 vertex 跳转）。
        outcome.results = self._reorder_results_official(outcome.results)
        outcome.elapsed_ms = elapsed_ms
        outcome.backend = backend
        outcome.instructions_mode = instructions_mode
        outcome.evidence = "responses_structured"
        usage = _as_dict(getattr(response, "usage", None))
        outcome.usage = {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "total": usage.get("total_tokens"),
        }
        return outcome

    def _hybrid_instructions(self, limit: int) -> str:
        """fallback 用的混合式指令：先自然语言回答，末尾再输出 JSON 结果。"""
        return (
            "你是网页搜索后端。必须先调用 web_search 工具检索最新信息。"
            "先用中文给出简短回答并引用来源；最后另起一行输出 JSON："
            '{"results":[{"title":"标题","url":"source url","snippet":"不超过80字摘要"}],'
            '"answer":""}。'
            f"results 最多 {limit} 条。"
            + self._date_rules_instruction()
            + "不要编造检索结果。"
        )

    # ------------------------------------------------------ vertex 跳转还原
    async def _resolve_outcome_vertex(self, outcome: SearchOutcome) -> int:
        """把结果/来源里的 vertexaisearch 跳转还原成真实 URL（最多前 5 条）。"""
        urls: list[str] = []
        seen: set[str] = set()
        for row in list(outcome.results) + list(outcome.sources):
            url = str(row.get("url") or "")
            if url in seen or not self._is_vertex_uri(url):
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) >= _VERTEX_MAX_ITEMS:
                break
        if not urls:
            return 0
        logger.info("[model_websearch] vertex 还原尝试 %d 条", len(urls))
        mapping = await self._resolve_vertex_urls(urls)
        if not mapping:
            return 0
        for row in list(outcome.results) + list(outcome.sources):
            url = str(row.get("url") or "")
            if url in mapping:
                row["url"] = mapping[url]
        # S12：还原后重新标记来源是否已确定（仍为 vertex 跳转 = False）。
        for row in list(outcome.results) + list(outcome.sources):
            row["resolved"] = not self._is_vertex_uri(str(row.get("url") or ""))
        return len(mapping)

    async def _resolve_vertex_urls(self, urls: list[str]) -> dict[str, str]:
        """并发还原多条 vertex 跳转；单条失败/超时保留原 URL。"""

        async def resolve_one(uri: str) -> tuple[str, str]:
            try:
                location = await asyncio.wait_for(
                    asyncio.to_thread(self._resolve_vertex_location, uri),
                    timeout=_VERTEX_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - 还原失败保留原 URL
                logger.info(
                    "[model_websearch] vertex 还原失败 url=%s err=%s",
                    uri[:100],
                    type(exc).__name__,
                )
                return uri, ""
            return uri, (location or "")

        pairs = await asyncio.gather(*(resolve_one(uri) for uri in urls))
        return {uri: location for uri, location in pairs if location}

    def _resolve_vertex_location(self, uri: str) -> str:
        """不跟随重定向，只取 vertexaisearch 首跳 3xx 的 Location（真实来源 URL）。"""
        if not self._is_vertex_uri(uri):
            return ""
        opener = urllib.request.build_opener(_NoRedirectHandler)
        request = urllib.request.Request(
            uri, headers={"User-Agent": "astrbot-plugin-model-websearch/0.2.3"}
        )
        try:
            with opener.open(request, timeout=_VERTEX_TIMEOUT_SECONDS) as response:
                location = response.headers.get("Location") or ""
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                return ""
            location = exc.headers.get("Location") or ""
        except Exception:  # noqa: BLE001 - 网络/站点问题都只当还原失败
            return ""
        return location if self._is_safe_public_http_url(location) else ""

    @staticmethod
    def _is_vertex_uri(url: str) -> bool:
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            return False
        return host == _VERTEX_HOST

    @staticmethod
    def _is_safe_public_http_url(url: str) -> bool:
        """只接受 http(s) 且非私网/环回地址，避免还原出内网 URL（SSRF 防护）。"""
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").lower()
        if not host or host == "localhost" or host.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        )

    @staticmethod
    def _url_breakdown(outcome: SearchOutcome) -> tuple[int, int]:
        """统计结果 URL 里直链与 vertex 跳转各多少条（日志用）。"""
        direct = 0
        vertex = 0
        for row in outcome.results:
            url = str(row.get("url") or "")
            if NativeSearchClient._is_vertex_uri(url):
                vertex += 1
            elif url.startswith(_URL_ALLOWED):
                direct += 1
        return direct, vertex

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

    def _date_rules_instruction(self) -> str:
        """S6：Responses/fallback 共用的日期时区规则 + 最少结果条数要求。"""
        return (
            f"时间基准：{format_time_context()}。"
            "用户说「今天/明天/这周」时按北京时间（UTC+8）理解；"
            "涉及赛事/活动日期必须同时给出当地日期与北京日期，"
            "不得把来源地日期当作用户今天，不得编造检索结果或赛程。"
            "优先官方/权威来源（lolesports.com、liquipedia.net、wikipedia.org 等）。"
            "至少返回 3–5 条不同来源的结果，不要只返回 1 条就收工。"
        )

    def _instructions(self, limit: int, include_answer: bool = False) -> str:
        rules = self._date_rules_instruction()
        if include_answer:
            return (
                "你是网页搜索后端。必须先调用 web_search 工具检索最新信息，"
                "再基于检索结果整理成单个 JSON 对象：\n"
                '{"results":[{"title":"标题","url":"来源链接","snippet":"摘要"}],'
                '"answer":"基于检索结果的简短回答（中文，不超过200字）"}\n'
                f"results 最多 {limit} 条，snippet 不超过 120 字。"
                + rules
                + "不要输出 JSON 以外的任何文字，不要编造检索结果。"
            )
        return (
            "你是网页搜索后端。必须先调用 web_search 工具检索最新信息。"
            "只返回检索到的网页结果，不要写总结、不要解释。输出单个 JSON：\n"
            '{"results":[{"title":"标题","url":"source url","snippet":"不超过80字的摘要"}],'
            '"answer":""}\n'
            f"results 最多 {limit} 条；不要输出 JSON 以外的任何文字。"
            + rules
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
    def _cache_key(
        self,
        provider_id: str,
        model: str,
        query: str,
        limit: int,
        backend: str,
        time_bucket: str = "",
    ) -> str:
        return "|".join(
            [
                provider_id,
                model,
                query.strip().lower(),
                str(limit),
                self.search_context_size,
                backend,
                "vertex=1" if self.vertex_redirect_resolve else "vertex=0",
                # S1 注入的时间上下文按天失效：同一天内缓存可用，跨天自动 miss。
                f"day={time_bucket or '-'}",
                "retry=1" if self.suspicious_result_retry else "retry=0",
            ]
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
