# 更新日志

版本号取自 `metadata.yaml` 的 `version`；AstrBot 以该字段展示插件版本，因此发布前先改它再打 tag。

## 0.2.0 — 2026-10-30

### 新增

- 优先读取 AstrBot profile 的 `provider_settings.websearch_native_provider_id`：由 **model-console（模型控制台）** 的「搜索使用模型」写入，实现“聊天用一个模型、搜索用另一个模型”；该字段为空时仍回退到插件自身的 `provider_mode`（current/fixed）。
- 与 model-console 的联动说明写进 README：控制台只在检测到本插件已安装并启用时才显示「跟随当前聊天模型（模型原生搜索）」；控制台的「测试模型原生搜索」按钮会真实调用一次 Responses `web_search`。
- 仓库工程化：README / LICENSE / CHANGELOG / CI / tag 触发的 Release 构建（产出可直接安装的插件 zip）。

### 修复

- 单测不再要求预先安装 AstrBot：`scripts/run-offline-tests.py` 为 `astrbot.api` 注入只含 logger 的 stub；测试导入同时兼容“AstrBot 插件目录”和“仓库根目录源码”两种模块名。

## 0.1.0 — 2026-10-30

首个可用版本。

### 功能

- 把 AstrBot「AI 能力 → 网页搜索」的检索后端切到**当前聊天模型自带的原生搜索**：配置 `web_search=true` 且 `websearch_provider="model_native"` 时，在 `on_llm_request` 注入 `web_search_model_native` 工具。
- 工具内部通过聊天模型 provider 的 OpenAI Responses API（`tools=[{"type":"web_search"}]`）检索，返回 `results` / `queries` / `sources`（`answer` 默认关闭，由主模型写答案）。
- 搜索模型来源：`current` 跟随当前会话模型；`fixed` 固定为指定 provider（留空明确报错，不静默回落）。
- 同一轮用户消息内最多真实搜索 `max_calls_per_turn` 次（默认 2），达到上限后把已有结果返回给模型并提示直接作答，避免一轮多次联网把响应拖到一分钟以上。
- 同类搜索工具冲突策略：`prefer_self`（默认，仅从本次请求移除其它搜索工具）/ `keep_all` / `skip_self`；识别内置 `web_search_*`、`anysearch_*` 等工具名，可追加自定义冲突工具名。
- 结果缓存（默认 300s，命中不计频率）与会话级限流（默认 6 次/分钟）。
- `/msearch`、`/模型搜索` 手动命令，便于不依赖模型决策的联调；长结果在 OneBot v11/NapCat 私聊可自动分段。
- 失败一律以 `错误：…` 文本返回给模型，不抛异常打断对话；日志只记 provider id、模型名、耗时与 token 用量，不记 Key。
