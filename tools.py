"""构造注入给模型的 FunctionTool。

用 FunctionTool + handler 的形态（AstrBot 4.29.3 执行器支持 handler），
避免额外注册全局工具；工具只在 on_llm_request 里按需注入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrbot.core.agent.tool import FunctionTool

from .client import SEARCH_TOOL_NAME

if TYPE_CHECKING:  # pragma: no cover
    from .main import ModelWebSearchPlugin


def build_web_search_tool(plugin: "ModelWebSearchPlugin") -> FunctionTool:
    """构造本插件的网页搜索工具（每次请求新建，绑定当前插件实例）。"""

    async def handler(event: Any, query: str, max_results: int = 5) -> str:
        return await plugin.handle_search_tool(event, query, max_results)

    return FunctionTool(
        name=SEARCH_TOOL_NAME,
        description=(
            "使用当前聊天模型自带的原生网页搜索（OpenAI Responses API web_search）"
            "检索实时信息。适合查询新闻、最新事实、网页资料、需要来源引用的内容。"
            "一次调用会返回多条结果（标题/链接/摘要）；结果通常已足够，"
            "请优先直接作答，不要连续重复调用本工具。"
            "返回 JSON：results、answer（可为空）、queries（实际检索词）、sources（来源引用）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "必填。搜索关键词或自然语言问题。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "可选。返回结果条数，1-10，默认使用插件配置。",
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )
