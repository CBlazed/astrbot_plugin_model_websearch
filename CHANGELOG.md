# 更新日志

版本号取自 `metadata.yaml` 的 `version`；AstrBot 以该字段展示插件版本，因此发布前先改它再打 tag。

## 0.2.4 — 2026-10-31

搜索准确率与延迟优化。

### 新增

- query 预处理：注入当前北京时间/日期/星期，剥离 `site:`，保留用户原话；可配开关。
- Gemini 原生 `systemInstruction`：统一日期/时区口径，禁止把来源地日期当作本地今天，禁止编造，优先权威来源。
- Gemini 原生 `thinkingBudget`（默认 128，0=关闭）：降低搜索延迟，保持回答正确率。
- 可疑空结果自动复核一次；native answer 按句截断（`answer_max_chars`）。
- 来源排序：官方/权威域名优先；native 结果带 `resolved` 标记。

### 改进

- Responses/fallback 增加日期时区规则；工具描述约束 query 写法。
- 缓存 key 纳入日期 bucket；加载日志补充关键配置。
- 统一 `provider_mode=fixed` 时的搜索模型选择与错误提示。

### 工程

- 版本 v0.2.4；新增配置均有默认值，已有配置无需迁移。
- 离线单测 86 项全绿；打包校验 8 文件。

## 0.2.3 — 2026-10-31

### 新增

- **Gemini 原生搜索后端（方案 B）**：模型名含 `gemini` 时默认不再依赖 Responses 兼容层，
  直接调用 Gemini 原生 `POST {root}/v1beta/models/{model}:generateContent`（root 由 provider
  的 `base_url` 去掉 `/v1` 得到），body 带 `tools=[{"google_search":{}}]`；解析
  `candidates[0].groundingMetadata`（`webSearchQueries` / `groundingChunks[].web` /
  `groundingSupports`）与 `content.parts[*].text`，构造与旧版一致的 `results` / `sources` /
  `queries` / `answer`，并标记 `backend=gemini_native`、`evidence=gemini_grounding`。
  snippet 从 `groundingSupports` 的 segment 提取；兼容官方 `content.parts` 与本机反代
  直接放在 candidate 上的 `parts` 两种形态。鉴权先用 `Authorization: Bearer`，
  401/403 再试 `?key=`（URL 编码）。
- **原生失败回退**：`gemini_native_fallback_to_responses`（默认开）在原生 HTTP 错误/超时/
  无 `groundingMetadata`/解析失败时回退 Responses，并换用混合式 instructions（先自然语言
  回答并引用来源，末尾再输出 JSON），仍要求结构化搜索证据；日志标注
  `backend=responses_fallback` 并保留原生失败原因。
- **零结果策略** `empty_result_policy`（默认 `notice`）：搜索已执行但 grounding 来源为空时
  正常返回空 `results` + `notice` 提示 + `answer`；设为 `error` 保持 0.2.2 的抛错行为。
- **vertex 跳转还原** `vertex_redirect_resolve`（默认关）：把结果/来源里
  `vertexaisearch.cloud.google.com` 的跳转用不跟随重定向的首跳 3xx `Location` 还原为真实
  URL；仅该域名、最多前 5 条、2s/条、并发，拒绝非 http(s) 与私网/环回地址，失败保留原链接。
- **Gemini 超时** `gemini_timeout_seconds`（默认 90）：原生与 fallback 的有效超时；
  非 Gemini 仍用原 `timeout_seconds`。实测 Gemini argon 单次可达 53.9s，45s 不够。
- **后端选择** `gemini_search_backend`（`auto`/`native`/`responses`，默认 `auto`）。

### 改进

- 失败日志补充结构化上下文：`provider=` / `model=` / `elapsed=` / `output=[类型序列]`
  （数量）/ `status=` / `incomplete_details=`，Gemini 原生路径再加 `backend=` /
  `endpoint=` / `grounding=` / `finish_reason=`；错误文本仍以「错误：…」前缀可读，
  但不抄响应正文、不含 Key。
- 成功日志新增 `backend=` / `instructions_mode=` / `evidence=` / `url_kinds=direct:N vertex:M`。
- 缓存 key 纳入后端（`gemini_native` / `responses` / `responses_fallback`）与
  `vertex=0/1`，避免不同后端/链接形态互相命中。
- 非 Gemini 模型仍走原 Responses + JSON 指令路径，请求体、解析与对外返回结构不变。

### 工程

- `metadata.yaml` 与 `main.py` 注册版本同步到 v0.2.3。
- 离线单测从 30 项扩到 49 项：Gemini 原生成功、candidate.parts 形态、Bearer→key 重试、
  原生缓存、无 grounding 上下文、fallback 成功/关闭、空 grounding 的 notice/error 策略、
  vertex 还原与安全过滤、强制 Responses 后端、非 Gemini 不受影响、配置默认/钳制/指令文本。
- 新增配置项均有默认值，已有配置无需迁移。

## 0.2.2 — 2026-10-30

### 变更

- 仓库与文档维护整理；发布产物内容与 v0.2.1 保持一致。
- `metadata.yaml` 与 `main.py` 注册版本同步到 v0.2.2；插件运行逻辑与 0.2.1 相同。

## 0.2.1 — 2026-10-30

### 变更

- CI：`actions/setup-python` 升到 v6，消除 GitHub Actions 的 Node 20 弃用警告（发布流水线不再依赖即将下线的运行时）。
- README 新增「配套项目」区块，与 model-console 仓库互相链接（联动说明见「与模型控制台（model-console）的联动」）。
- 插件运行逻辑与 0.2.0 相同；本版本只含仓库工程与文档改动。

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
