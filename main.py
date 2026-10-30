"""模型原生网页搜索插件。

把 AstrBot 的网页搜索能力切换到「当前聊天模型自带的原生搜索」：
- 当 profile 的 provider_settings.web_search=true 且 websearch_provider=="model_native" 时，
  在 on_llm_request 阶段向本次请求注入 web_search_model_native 工具。
- 工具内部通过当前会话聊天模型 provider 的 OpenAI Responses API（tools=[{"type":"web_search"}]）
  完成检索，返回 results / answer / queries / sources。
- Gemini 模型默认走 Gemini 原生 `/v1beta:generateContent` + `google_search`，
  解析 groundingMetadata 作为搜索证据（失败可按配置回退 Responses + hybrid 指令）。
- 同时提供 /msearch 手动命令，便于不依赖模型决策的联调与兜底。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.tool import ToolSet
from astrbot.core.star.filter.command import GreedyStr

from .client import (
    SEARCH_TOOL_NAME,
    SENTINEL_PROVIDER,
    NativeSearchClient,
    SearchError,
    SearchOutcome,
    build_turn_limit_text,
    get_turn_state,
)
from .tools import build_web_search_tool

PLUGIN_NAME = "astrbot_plugin_model_websearch"


@register(
    PLUGIN_NAME,
    "CBlazed",
    "让网页搜索直接调用当前聊天模型自带的原生联网搜索能力（Responses API web_search；"
    "Gemini 走原生 /v1beta grounding 后端），支持查询时间上下文注入、来源质量排序、"
    "可疑结果自动复核、固定搜索模型、本插件级禁用开关、同类搜索工具冲突处理、结果缓存与频率限制。",
    "v0.2.4",
    "",
)
class ModelWebSearchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config or {}
        self.client = NativeSearchClient(self.config)
        logger.info(
            "[model_websearch] loaded: disabled=%s mode=%s fixed_id=%s conflict=%s cache_ttl=%ss "
            "rate=%s/min gemini_backend=%s gemini_fallback=%s thinking_budget=%s query_inject=%s strip_site=%s "
            "retry=%s answer_max=%s",
            self.client.disabled,
            self.client.provider_mode,
            self.client.fixed_provider_id or "-",
            self.client.conflict_policy,
            self.client.cache_ttl_seconds,
            self.client.rate_limit_per_minute,
            self.client.gemini_search_backend,
            self.client.gemini_native_fallback_to_responses,
            self.client.gemini_thinking_budget,
            self.client.query_context_inject,
            self.client.strip_site_operator,
            self.client.suspicious_result_retry,
            self.client.answer_max_chars,
        )

    async def terminate(self) -> None:
        logger.info("[model_websearch] plugin stopped.")

    # ------------------------------------------------------------ 开关判断
    def _native_search_provider_id(self, event: AstrMessageEvent) -> str:
        """profile 级搜索模型覆盖：provider_settings.websearch_native_provider_id。

        空字符串表示跟随当前聊天模型。读取失败时返回空，不影响插件原有逻辑。
        """
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
        except Exception:  # noqa: BLE001
            return ""
        settings = cfg.get("provider_settings", {}) if hasattr(cfg, "get") else {}
        if not isinstance(settings, dict):
            return ""
        return str(settings.get("websearch_native_provider_id") or "").strip()

    def _core_switch_on(self, event: AstrMessageEvent) -> bool:
        """AstrBot 本体开关：web_search=true 且 provider 是哨兵 model_native。"""
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
        except Exception:  # noqa: BLE001 - 配置读取失败时保守关闭
            logger.warning("[model_websearch] 读取核心配置失败，跳过工具注入", exc_info=True)
            return False
        settings = cfg.get("provider_settings", {}) if hasattr(cfg, "get") else {}
        if not isinstance(settings, dict):
            return False
        enabled = bool(settings.get("web_search"))
        provider = str(settings.get("websearch_provider") or "").strip()
        return enabled and provider == SENTINEL_PROVIDER

    # -------------------------------------------------------- 请求级工具注入
    @filter.on_llm_request()
    async def inject_search_tool(self, event: AstrMessageEvent, req: Any) -> None:
        if self.client.disabled:
            return
        if not self._core_switch_on(event):
            return
        if getattr(req, "func_tool", None) is None:
            req.func_tool = ToolSet()

        existing = [tool.name for tool in req.func_tool.tools]
        conflicts = [name for name in existing if self.client.is_conflicting_tool(name)]

        if conflicts and self.client.conflict_policy == "skip_self":
            logger.info(
                "[model_websearch] 检测到其它搜索工具 %s，skip_self 策略下不注入本插件工具",
                conflicts,
            )
            return
        if conflicts and self.client.conflict_policy == "prefer_self":
            for name in conflicts:
                req.func_tool.remove_tool(name)
            logger.info(
                "[model_websearch] prefer_self：已从本次请求移除冲突搜索工具 %s",
                conflicts,
            )
        elif conflicts:
            logger.warning(
                "[model_websearch] 检测到其它搜索工具 %s，keep_all 策略下全部保留（可能重复检索）",
                conflicts,
            )

        req.func_tool.add_tool(build_web_search_tool(self))
        logger.debug(
            "[model_websearch] 已注入 %s（umo=%s）",
            SEARCH_TOOL_NAME,
            event.unified_msg_origin,
        )

    # ------------------------------------------------------------ 工具执行
    async def handle_search_tool(
        self,
        event: AstrMessageEvent,
        query: str,
        max_results: int = 5,
    ) -> str:
        if self.client.disabled:
            return "错误：模型原生网页搜索插件已被禁用（插件配置：禁用网页搜索=开）。"

        state = get_turn_state(event)
        limit_text = build_turn_limit_text(
            state, self.client.max_calls_per_turn, SEARCH_TOOL_NAME
        )
        if limit_text is not None:
            logger.info(
                "[model_websearch] 本轮搜索已达上限（%s 次），直接复用已有结果",
                state.calls,
            )
            return limit_text

        try:
            outcome = await self.client.search(
                self.context,
                event.unified_msg_origin,
                query,
                max_results,
                search_provider_id=self._native_search_provider_id(event),
            )
        except SearchError as exc:
            # 失败不计数，允许模型换一次 query 重试。
            logger.warning(
                "[model_websearch] 搜索失败：%s | debug=%s",
                exc,
                getattr(exc, "debug", None) or "-",
            )
            return f"错误：{exc}"

        state.calls += 1
        state.last = outcome
        return outcome.to_tool_text()

    # ------------------------------------------------------------ 手动命令
    @filter.command(
        "msearch",
        alias={"模型搜索"},
        desc="用当前聊天模型的原生网页搜索手动检索（便于测试与兜底）。",
    )
    async def msearch_command(self, event: AstrMessageEvent, query: GreedyStr):
        """用当前聊天模型的原生网页搜索手动检索。

        Args:
            query(string): 搜索关键词；help 查看用法。
        """
        text = (query or "").strip()
        if self.client.disabled:
            yield event.plain_result(
                "模型原生网页搜索已被禁用（插件配置 → 禁用网页搜索=开）。"
            )
            return
        if not text or text.lower() in {"help", "帮助", "?", "？", "用法"}:
            yield event.plain_result(self._usage_text())
            return

        try:
            outcome = await self.client.search(
                self.context,
                event.unified_msg_origin,
                text,
                search_provider_id=self._native_search_provider_id(event),
            )
        except SearchError as exc:
            logger.warning(
                "[model_websearch] /msearch 失败：%s | debug=%s",
                exc,
                getattr(exc, "debug", None) or "-",
            )
            yield event.plain_result(f"搜索失败：{exc}")
            return

        for chunk in self._split_for_platform(event, self._format_outcome(outcome)):
            yield event.plain_result(chunk)

    # ------------------------------------------------------------ 输出格式
    @staticmethod
    def _usage_text() -> str:
        return (
            "模型原生网页搜索用法：\n"
            "/msearch 关键词\n"
            "（别名：/模型搜索 关键词）\n"
            "需要 AstrBot 网页搜索提供商设为 model_native 且插件未禁用；"
            "当前聊天模型需支持 OpenAI Responses API 的 web_search。"
        )

    @staticmethod
    def _format_outcome(outcome: SearchOutcome) -> str:
        lines = [
            f"🔎 模型原生搜索 · {outcome.model} · {outcome.elapsed_ms / 1000:.1f}s",
        ]
        if outcome.queries:
            lines.append("检索：" + " | ".join(outcome.queries[:3]))
        for index, row in enumerate(outcome.results, 1):
            title = row.get("title") or "(无标题)"
            snippet = row.get("snippet") or ""
            block = f"{index}. {title}\n   {row['url']}"
            if snippet:
                block += f"\n   {snippet}"
            lines.append(block)
        if outcome.answer:
            lines.append("")
            lines.append("💬 " + outcome.answer)
        if not outcome.results:
            lines.append("（没有可展示的搜索结果，只有模型回答）")
        if outcome.notice:
            lines.append("提示：" + outcome.notice)
        return "\n".join(lines)

    def _split_for_platform(self, event: AstrMessageEvent, text: str) -> list[str]:
        size = self.client.command_chunk_size
        if size <= 0 or len(text) <= size:
            return [text]
        if event.get_platform_name() != "aiocqhttp":
            return [text]
        group_id = ""
        try:
            group_id = event.get_group_id() or ""
        except Exception:  # noqa: BLE001
            group_id = ""
        if group_id and not self.client.chunk_group:
            return [text]
        if not group_id and not self.client.chunk_private:
            return [text]
        return self._chunk_text(text, size)

    @staticmethod
    def _chunk_text(text: str, size: int) -> list[str]:
        limit = max(200, size - 16)
        chunks: list[str] = []
        current = ""
        for paragraph in text.split("\n\n"):
            if not paragraph:
                continue
            pieces = [paragraph]
            if len(paragraph) > limit:
                pieces = [paragraph[i : i + limit] for i in range(0, len(paragraph), limit)]
            for piece in pieces:
                separator = "\n\n" if current else ""
                if len(current) + len(separator) + len(piece) > limit:
                    if current:
                        chunks.append(current.strip())
                    current = piece
                else:
                    current = f"{current}{separator}{piece}" if current else piece
        if current.strip():
            chunks.append(current.strip())
        if len(chunks) <= 1:
            return [text]
        total = len(chunks)
        return [f"[{index}/{total}]\n{chunk}" for index, chunk in enumerate(chunks, 1)]
