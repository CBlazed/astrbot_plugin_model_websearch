"""构造注入给模型的 FunctionTool。

用 FunctionTool + handler 的形态（AstrBot 4.29.3 执行器支持 handler），
避免额外注册全局工具；工具只在 on_llm_request 里按需注入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:  # 离线单测只导入描述常量时允许没有 AstrBot；运行期一定有。
    from astrbot.core.agent.tool import FunctionTool
except ModuleNotFoundError:  # pragma: no cover - 离线单测 stub 场景
    FunctionTool = None  # type: ignore[assignment]

from .client import SEARCH_TOOL_NAME

if TYPE_CHECKING:  # pragma: no cover
    from .main import ModelWebSearchPlugin


# S3：直接约束主模型怎么拼 query —— 用自然语言/用户原话，不自己换算日期、不加 site:。
TOOL_DESCRIPTION = (
    "使用当前聊天模型自带的原生网页搜索（OpenAI Responses API web_search；"
    "Gemini 走原生 /v1beta grounding）检索实时信息。适合查询新闻、最新事实、网页资料、"
    "需要来源引用的内容。一次调用会返回多条结果（标题/链接/摘要）；"
    "结果通常已足够，请优先直接作答，不要连续重复调用本工具。"
    "参数要求：query 用中文自然语言（优先直接用用户原话或问题本身），"
    "不要自行把「今天/明天」换算成绝对日期，不要添加 site: 操作符，"
    "不要把来源地（美中/欧洲）的日期写成今天；时间基准由插件在调用时自动补充北京时间。"
    "建议 max_results 取 8–10 条，避免只看到 1–2 条就下结论。"
    "返回 JSON：results、answer（可为空）、queries（实际检索词）、sources（来源引用）。"
)

TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "必填。搜索查询：用中文自然语言，优先直接使用用户原话/问题本身；"
                "不要自行把「今天」换算成具体日期，不要加 site: 操作符。"
            ),
        },
        "max_results": {
            "type": "integer",
            "description": (
                "可选。返回结果条数，1-10；建议 8–10 条，留足候选再判断。"
            ),
        },
    },
    "required": ["query"],
}


def build_web_search_tool(plugin: "ModelWebSearchPlugin") -> FunctionTool:
    """构造本插件的网页搜索工具（每次请求新建，绑定当前插件实例）。"""
    if FunctionTool is None:  # pragma: no cover - 运行期不会发生
        raise ImportError("构造 FunctionTool 需要 AstrBot 运行环境（astrbot.core.agent.tool）")

    async def handler(event: Any, query: str, max_results: int = 5) -> str:
        return await plugin.handle_search_tool(event, query, max_results)

    return FunctionTool(
        name=SEARCH_TOOL_NAME,
        description=TOOL_DESCRIPTION,
        parameters=TOOL_PARAMETERS,
        handler=handler,
    )
