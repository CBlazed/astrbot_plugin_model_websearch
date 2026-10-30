# 模型原生网页搜索 · astrbot_plugin_model_websearch

[![ci](https://github.com/CBlazed/astrbot_plugin_model_websearch/actions/workflows/ci.yml/badge.svg)](https://github.com/CBlazed/astrbot_plugin_model_websearch/actions/workflows/ci.yml)
[![release](https://github.com/CBlazed/astrbot_plugin_model_websearch/actions/workflows/release.yml/badge.svg)](https://github.com/CBlazed/astrbot_plugin_model_websearch/actions/workflows/release.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.29%2C%20%3C5-0b7285)](metadata.yaml)

**下载**：[Releases 插件包](https://github.com/CBlazed/astrbot_plugin_model_websearch/releases/latest)

把 AstrBot「AI 能力 → 网页搜索」的检索后端换成**当前聊天模型自带的原生联网搜索**
（OpenAI Responses API `tools=[{"type":"web_search"}]`；Gemini 走原生 `/v1beta` +
`google_search` 的 grounding 后端），不必再单独配置 Tavily / DeepSeek / Baidu 等第三方搜索 API。

适用前提：聊天模型 provider 提供 OpenAI **Responses API** 且支持 `web_search` 工具
（本机 example-provider 的 `model-a` / `model-c` / `model-d` 已验证可用）；
Gemini 只需 provider 暴露 `/v1beta/models/{model}:generateContent`（本机反代已验证）。

## 解决什么问题

AstrBot 4.29.x 的内置网页搜索提供商都是“另配一份搜索 API Key”，于是同一台机器上会出现两套 Key、
两套计费、两套限额。而很多聊天模型（GPT、Gemini、DeepSeek v4-pro 等）本身已经带联网搜索，
重复配置既是成本也是故障源。

本插件让搜索直接走**聊天模型自己的搜索能力**：没有额外 Key、没有额外账单，
搜索结果、引用与模型回答在同一上下文里，不跨服务搬运。

## 功能特性

- **配置零耦合**：不改 AstrBot 核心代码，升级 AstrBot 不会丢功能；只读 profile 的
  `provider_settings.web_search` / `websearch_provider` / `websearch_native_provider_id`。
- **可指定搜索模型**：聊天用一个模型、搜索用另一个（例如聊天用 luna、搜索用 gemini）。
- **Gemini 原生 grounding（方案 B）**：模型名含 `gemini` 时默认直接调用 Gemini 原生
  `/v1beta/models/{model}:generateContent` + `google_search`，解析 `groundingMetadata`
  作为真实搜索证据；失败可按配置回退到 Responses 路径（混合式指令）。
- **时间/时区约束（0.2.4）**：调用前注入北京时间上下文、剥离 `site:`，Gemini 原生带
  systemInstruction、Responses 指令带日期规则，减少「把美中日期当今天」的误答。
- **可疑结果复核（0.2.4）**：结果像「没有比赛/无赛程」且无明确对阵时自动重试 1 次，
  仍不可靠则返回带 notice 的结果，避免把「今天没赛」直接转述给用户。
- **来源质量（0.2.4）**：结果（native 与 Responses/fallback）官方/权威域名优先排序；
  native 额外带 `resolved` 标记区分真实来源与 vertex 跳转；answer 按句截断（默认 800）。
- **同一轮限次**：默认每轮最多真实搜索 2 次，达到上限后把已有结果交回模型并提示直接作答，
  避免一轮里反复联网把响应拖到一分钟以上。
- **冲突处理**：同一次请求里如果还有其它搜索工具（`web_search_*`、`anysearch_*` 等），
  可按策略移除、保留或让位，不影响其它插件自身。
- **缓存 + 限流**：相同查询默认缓存 300s（命中不计频率）；每会话默认 6 次/分钟。
- **失败不炸对话**：任何失败都以 `错误：…` 文本返回给模型，不抛异常。
- **手动兜底**：`/msearch 关键词` 不经模型决策直接搜索，便于联调。

## 与模型控制台（model-console）的联动

[**astrbot-model-console**](https://github.com/CBlazed/astrbot-model-console) 是本插件的配套可视化管理工具
（Windows 便携包，在浏览器里改 AstrBot 模型与网页搜索配置，预览 → 备份 → 写入）。两者是**协同设计**的：

### 1. 控制台写、插件读的三个 profile 字段

| 字段 | 谁写 | 本插件怎么用 |
|---|---|---|
| `provider_settings.web_search` | 控制台「启用网页搜索」开关 | 为 `true` 且下一项为 `model_native` 时才注入搜索工具 |
| `provider_settings.websearch_provider` | 控制台「网页搜索提供商」下拉 | 值为哨兵 `model_native` 时启用本插件；AstrBot 核心对未知值不会添加内置搜索工具 |
| `provider_settings.websearch_native_provider_id` | 控制台「搜索使用模型」 | 非空时作为搜索模型 provider；空 = 跟随当前聊天模型 |

**搜索模型优先级**（从高到低）：profile 的 `websearch_native_provider_id` → 插件配置的
`provider_mode=fixed` 的 `provider_id` → 当前会话聊天模型。
显式指定但 provider 不存在时**直接报错**，不会静默回落（避免“以为在用 gemini，实际在用 luna”）。

### 2. 控制台只在检测到本插件时才显示「模型原生」选项

控制台通过 `GET /api/v1/plugins` 读取插件清单：

- 未安装本插件 → 下拉里只有内置搜索提供商；
- 已安装但未启用 → 「跟随当前聊天模型（模型原生搜索）」置灰并提示；
- 已安装且启用 → 出现该选项，并可继续设置「搜索使用模型」。

这一步是安全降级：远端 HTTP 设备读不到插件清单时也只显示内置提供商。控制台后端在预览/应用时
会**重新读一次**插件状态，拒绝写入不可用的 `model_native`，不信任过期的页面快照。

### 3. 控制台的两个「测试」按钮

| 按钮 | 端点 | 作用 |
|---|---|---|
| 测试 Key | `POST /search-key-test` | 内置搜索提供商（Tavily/DeepSeek/Baidu…）的 Key 是否可用；与本插件无关 |
| 测试模型原生搜索 | `POST /search-native-test` | 真实发一次 Responses `web_search`，看模型是否**真的**调用了搜索、返回几次 `web_search_call` 与几条引用 |

后者是排障的关键：有些模型（如 `deepseek-flash`）对 `tools=[{"type":"web_search"}]` 返回 200 但
**一次搜索都不执行**，只看 HTTP 状态会误判“支持”。

### 4. 联动使用流程

1. 安装本插件（见下节），在 AstrBot 插件管理里启用；
2. 打开 model-console，选中目标 profile；卡片底部「网页搜索」组里打开「启用网页搜索」，
   提供商选「跟随当前聊天模型（模型原生搜索）」；
3. 需要“聊天/搜索用不同模型”时，在「搜索使用模型」里选一个支持原生搜索的 provider；
4. 点「测试模型原生搜索」确认真的会联网，然后「预览 → 应用」写入配置。

### 5. 不用控制台也能用

手工改 AstrBot 的 profile 配置文件，把 `provider_settings.web_search` 设为 `true`、
`websearch_provider` 设为 `model_native` 即可；`websearch_native_provider_id` 可选。

## 安装

要求：AstrBot **>= 4.29, < 5**。

### 方式一：Release 插件包（推荐）

1. 从 [Releases](https://github.com/CBlazed/astrbot_plugin_model_websearch/releases/latest) 下载
   `astrbot_plugin_model_websearch-v*.zip`；
2. AstrBot WebUI → **插件管理 → 从文件安装**，上传该 zip；
3. 启用插件并重载。

### 方式二：解压到插件目录

把 zip 解压出的 `astrbot_plugin_model_websearch/` 整个目录放到 AstrBot 的
`data/plugins/` 下，然后重启 AstrBot 或在 WebUI 里重载插件。

### 方式三：从源码

```bash
git clone https://github.com/CBlazed/astrbot_plugin_model_websearch
# 把仓库目录复制/软链为 <AstrBot>/data/plugins/astrbot_plugin_model_websearch
```

## 快速开始

1. 安装并启用插件；
2. AstrBot 配置里把网页搜索改成 `model_native`（或用 model-console 勾选）；
3. 在群里问一个需要联网的问题，或直接发 `/msearch AstrBot 4.29 支持哪些内置搜索提供商`。

日志里会出现：

```text
[model_websearch] loaded: disabled=False mode=current fixed_id=- conflict=prefer_self cache_ttl=300 rate=6/min gemini_backend=auto gemini_fallback=True
[model_websearch] provider=example-provider/model-a model=model-a elapsed=6.6s results=8 queries=2 backend=responses instructions_mode=json evidence=responses_structured url_kinds=direct:8 vertex:0 usage={...}
[model_websearch] provider=example-provider/gemini-model-a model=gemini-model-a elapsed=28.6s results=10 queries=7 backend=gemini_native instructions_mode=native evidence=gemini_grounding url_kinds=direct:0 vertex:10 usage={...}
```

- `backend`：`gemini_native`（原生 grounding）/ `responses`（原 Responses 路径）/
  `responses_fallback`（原生失败后回退，日志另有 WARN）。
- `evidence`：`gemini_grounding` 或 `responses_structured`；`url_kinds` 统计结果里直链与
  vertexaisearch 跳转各多少条。

## 工作原理

1. AstrBot 本体配置：`provider_settings.web_search = true`，
   `provider_settings.websearch_provider = "model_native"`（哨兵值；4.29.3 核心对未知值不添加内置搜索工具）。
2. 插件在 `on_llm_request` 阶段检测到上述配置，向**本次请求**注入工具
   `web_search_model_native(query, max_results)`。
3. 模型调用工具时，插件解析要用的 provider（优先级见上文），先对**发给后端的 query** 做
   预处理（0.2.4，S1）：注入「当前北京时间 + 日期 + 星期（UTC+8）」前缀、剥离 `site:`
   操作符，用户原话保留；随后按 `gemini_search_backend` 分派后端：
   - Gemini（`auto` 且模型名含 gemini，或 `native`）：用 provider 的
     `base_url`/`api_key` 调 `{root}/v1beta/models/{model}:generateContent`，body 带
     `tools=[{"google_search":{}}]` 与 `systemInstruction`（北京时间基准/日期换算规则，S2）；
     `{root}` 由 base_url 去掉 `/v1` 得到，鉴权先用
     `Authorization: Bearer`，401/403 再试 `?key=`。
   - 其它模型：通过 `provider.client.responses.create(...)` 发起请求：
     `tools=[{"type":"web_search","search_context_size":"low"}]`（与 0.2.2 一致；
     instructions 自 0.2.4 起带日期规则与「至少 3–5 条结果」要求，S6）。
4. 返回 JSON：`results`（标题/链接/摘要）、`answer`（默认关闭，为空）、
   `queries`（实际检索词）、`sources`（来源引用）；Gemini 原生后端额外带
   `backend`/`evidence` 标记，`results` 带 `snippet_source="model_answer_segment"`
   与 `resolved` 标记（S11/S12）。
5. 结果疑似「没有比赛/无赛程」且没有明确对阵时，自动用「去 `site:` + 北京时间前缀 + 简化词」
   复核 1 次（S7，`suspicious_result_retry` 默认开）；仍不可靠时按 `empty_result_policy`
   返回带 notice 的结果。
6. 同一轮用户消息内最多真实搜索 `max_calls_per_turn` 次；达到上限后工具不再发起模型调用，
   而是把已有结果连同「请直接作答」提示返回给模型。

## 配置项

| 配置项 | 默认 | 说明 |
|---|---|---|
| 禁用网页搜索（本插件级） | 关 | 开启后不注入工具、`/msearch` 不可用；优先级高于本体开关 |
| 搜索模型来源 `provider_mode` | `current` | `current`=跟随当前会话聊天模型；`fixed`=使用下方 provider_id |
| 固定搜索模型 Provider ID | 空 | 仅 `fixed` 生效；**fixed 下留空会明确报错，不回落 current** |
| 返回结果条数 | 10 | 1–10 |
| 原生搜索上下文规模 | `low` | `low`/`medium`/`high`；越大越慢越贵 |
| 让搜索后端额外生成 answer | 关 | 默认只返回 results，最终回答由主模型写（更快更省） |
| 单次搜索超时（秒） | 45 | 超时以 `错误：搜索超时…` 返回给模型；非 Gemini 用本项 |
| Gemini 搜索后端 `gemini_search_backend` | `auto` | `auto`=模型名含 gemini 走原生 grounding；`native`=强制原生；`responses`=全部走 Responses |
| Gemini 原生失败回退 Responses | 开 | 原生 HTTP 错误/超时/无 `groundingMetadata` 时回退 Responses + 混合式指令，仍要求结构化证据 |
| Gemini 搜索超时（秒） | 90 | 原生与 fallback 的有效超时（Gemini 实测慢，45s 不够） |
| Gemini 原生 thinkingBudget | 128 | 仅原生请求：>0 发送 `generationConfig.thinkingConfig.thinkingBudget`；0=不发送。128 实测更快且保持正确率 |
| 零结果策略 `empty_result_policy` | `notice` | `notice`=空 results + notice 提示；`error`=抛「原生搜索没有返回可用结果」（0.2.2 行为） |
| 还原 vertexaisearch 链接 | 关 | 开启后把 vertex 跳转还原为真实 URL：仅该域名、最多前 5 条、2s/条、拒绝内网地址 |
| 查询注入北京时间上下文 `query_context_inject` | 开 | 发给后端的 query 前注入「当前北京时间 + 日期 + 星期（UTC+8）」，纠正「今天/明天」时区口径（S1） |
| 剥离 site: 操作符 `strip_site_operator` | 开 | 移除 query 里的 `site:域名/路径`，避免检索被锁死到单一页面（S1） |
| 可疑结果自动复核 `suspicious_result_retry` | 开 | 结果像「没有比赛/无赛程」且无明确对阵时，用北京时间前缀 + 简化 query 重试 1 次（S7） |
| native answer 截断上限 `answer_max_chars` | 800 | native answer 按句截断的上限（100–4000），替代旧的硬截 500（S11） |
| 相同查询结果缓存时间（秒） | 300 | 0=关闭；缓存命中不计频率；缓存 key 已含后端与 vertex 开关 |
| 每个会话每分钟最多搜索次数 | 6 | 0=关闭；只统计真实模型调用 |
| 同一轮消息最多真实搜索次数 | 2 | 0=不限制；达到上限后返回已有结果并提示直接作答（失败不计次） |
| 同类搜索工具冲突策略 | `prefer_self` | `prefer_self`/`keep_all`/`skip_self` |
| 额外视为冲突的工具名 | 空 | 逗号分隔；内置已覆盖 `web_search_*`、`anysearch_*` 等 |
| `/msearch` 长结果分段 | 开 | 分段长度、私聊/群聊开关 |

## 命令

```text
/msearch 关键词
/模型搜索 关键词
```

手动命令不受 AstrBot 本体网页搜索开关限制（方便联调），但仍受插件「禁用网页搜索」开关、
缓存与频率限制约束。长结果在 OneBot v11/NapCat 私聊默认自动分段，群聊默认不分段。

## 选择搜索模型

两种方式，任选其一：

- **profile 字段**（控制台「搜索使用模型」写入）：
  `provider_settings.websearch_native_provider_id`，空 = 跟随当前聊天模型；**优先级最高**。
- **插件配置 fixed**：`provider_mode=fixed` + `provider_id=example-provider/model-c`。

注意：不是所有模型都支持 Responses `web_search`。可用 model-console 的
「测试模型原生搜索」实测；例如 DeepSeek 官方目前仅 `deepseek-v4-pro` 支持，
`deepseek-flash` 会忽略该工具（HTTP 200，但 0 次搜索调用）。

## Gemini 原生搜索后端（方案 B）

为什么需要它：Gemini 通过 OpenAI Responses 兼容层时，**如果指令只许输出 JSON**，
本机反代常常只返回 `reasoning + message`，不带 `web_search_call`/`annotations`，
插件会判定“模型没有实际搜索”。Gemini 原生 `/v1beta` 端点会返回 Google 官方的
`groundingMetadata`，这是最接近“真搜索”的证据。

- **触发**：`gemini_search_backend=auto`（默认）时，解析出的模型名含 `gemini`（忽略大小写）
  就走原生；`native` 强制原生；`responses` 全部走原 Responses 路径。
- **端点推导**：`provider.client.base_url`（或配置里的 `api_base`）去掉尾部 `/v1` 得到 root，
  请求 `POST {root}/v1beta/models/{model}:generateContent`；模型名会做 URL 编码。
- **systemInstruction（0.2.4，S2）**：请求体带 `systemInstruction`，明确「今天/明天」按北京时间
  （UTC+8）理解、赛事日期必须同时给出当地日期与北京日期、不得把来源地日期当作本地今天、
  不得编造、优先官方来源。这是修正 Gemini 时区口径的关键，可用 `query_context_inject`
  连带控制时间前缀，但 systemInstruction 本身不单独提供开关。
- **thinkingBudget（0.2.4）**：原生请求可带 `generationConfig.thinkingConfig.thinkingBudget`，
  配置 `gemini_thinking_budget` 默认 128（0=不发送）。2026-10-31 对比测试中 3.8-flash-high
  从默认中位 12.6s 降到 6.0s，三题仍全对；只影响 Gemini 原生，Responses 路径不受影响。
- **鉴权**：先用 `Authorization: Bearer <key>`；若 401/403 再试 `?key=<key>`（URL 编码）。
  两条路都失败才报错，错误文本带 HTTP 状态与响应体前 200 字，不含 Key。
- **证据字段**：`candidates[0].groundingMetadata`
  - `webSearchQueries` → `queries`；
  - `groundingChunks[].web.{uri,title}` → `results`/`sources`（title 是域名，uri 通常是
    `vertexaisearch.cloud.google.com` 跳转）；results 先按官方/权威域名稳定排序（S12）；
  - `groundingSupports[].segment.text` → 对应 chunk 的 `snippet`（压缩空白、截 120 字），
    同时标记 `snippet_source="model_answer_segment"` —— **这是模型答案里的句子，不是网页正文**，
    不要把它当网页摘要引用（S11）；
  - `candidates[0].content.parts[*].text` → `answer`（0.2.4 起**按句截断**，上限
    `answer_max_chars` 默认 800，替旧硬截 500）；本机反代也可能把 `parts` 直接放在
    candidate 上，两种形态都兼容。
  返回的 JSON 会带 `backend=gemini_native`、`evidence=gemini_grounding`；`results`/`sources`
  每条带 `resolved`（`true`=真实来源 URL / `false`=仍是 vertex 跳转未还原）；旧 Responses
  路径的键集合保持不变。
- **回退（默认开）**：原生调用失败（HTTP 错误/超时/无 `groundingMetadata`/解析失败）时，
  对同一 query 回退到 Responses，并换用**混合式指令**（先自然语言回答并引用来源，末尾再输出
  JSON）——实测该形态下 Gemini 会返回 `web_search_call` + `annotations`。仍要求结构化证据；
  成功日志 `backend=responses_fallback`，失败日志带 `native_error`。
- **零结果策略**：搜索执行了但 `groundingChunks` 为空时，`notice`（默认）返回空 `results` +
  `notice` 提示与 `answer`；`error` 则抛 `错误：原生搜索没有返回可用结果`（0.2.2 行为）。
- **vertex 链接还原（默认关）**：开启后对结果/来源里的 `vertexaisearch.cloud.google.com`
  跳转发一次**不跟随重定向**的 GET，取首跳 3xx 的 `Location` 作为真实 URL。仅该域名、
  最多前 5 条、2s/条、并发执行；拒绝非 http(s) 与私网/环回地址，失败保留原跳转 URL。
  代价是每条约 1–2s，优先用 Responses 拿直链 annotations 更省。
- **超时**：原生与 fallback 都用 `gemini_timeout_seconds`（默认 90）；非 Gemini 仍用
  `timeout_seconds`（默认 45）。
- **缓存**：缓存 key 已含后端（`gemini_native`/`responses`/`responses_fallback`）与
  `vertex=0/1`；原生失败回退的成功结果按 `responses_fallback` 缓存，同一 query 下次直接命中。

排障时看日志字段：`backend=`、`instructions_mode=`、`evidence=`、`grounding=`
（`none`/`ok`/`queries_only`/`empty`）、`url_kinds=direct:N vertex:M`，以及失败时的
`provider=`/`model=`/`elapsed=`/`status=`/`endpoint=`。

## 搜索正确率改进（0.2.4）

0.2.3 上线后的实测暴露了一类「搜到了、但取错日期行」的错误（例如问「今天有哪些比赛/日程」，
主模型把日期预先换算成来源地时间并加了 `site:` 锁页，后端只返回来源地日期时段，漏掉当天实际场次）。
0.2.4 在插件内做如下防护：

| 项 | 做法 | 开关 |
|---|---|---|
| S1 query 预处理 | 发给后端的 query 前注入「当前北京时间 + 日期 + 星期」、剥离 `site:`，保留用户原话；日志 `query_guard original=… prepared=…` | `query_context_inject`（开）、`strip_site_operator`（开） |
| S2 Gemini systemInstruction | 北京时间基准；日期类问题必须同时给当地日期与北京日期；不得把来源地日期当今天；不得编造；优先权威来源 | 原生后端内默认生效 |
| S3 工具描述 | 要求主模型用中文自然语言/用户原话、不自行换算日期、不加 `site:`、建议 8–10 条结果 | — |
| S6 Responses/fallback 指令 | 同上日期规则 + 至少 3–5 条不同来源结果 | — |
| S7 可疑结果复核 | 结果/answer 出现 `no matches`/`无结果`/`无数据` 且无明确条目对比时，用去 `site:` 的简化 query + 北京时间前缀重试 **1 次**；仍不可靠按 `empty_result_policy` 返回 notice（`error` 且无结果时抛错）。日志 `retry=1 reason=… improved=…`。重试发生在同一次 `search()` 内，**不额外占用**每轮/每分钟配额 | `suspicious_result_retry`（开） |
| S10 query 质量告警 | query 同时含「今天/today」+ 外文绝对日期或 `site:` 时打 WARN（只告警不改行为） | — |
| S11 表现层 | native answer 按句截断（默认 800，替旧硬截 500）；native results 标 `snippet_source="model_answer_segment"` | `answer_max_chars` |
| S12 来源质量 | 结果（native 与 Responses/fallback）官方/权威域名优先稳定排序（Responses 重排后 `index` 序号跟随展示顺序）；native results/sources 带 `resolved` 标记（vertex 跳转还原前 `false`，还原成功或本身就是直链时 `true`）；开启 `vertex_redirect_resolve` 时，还原出真实域名后会**再按官方优先重排一次**并同步 sources/index（否则解析阶段全是 vertex 跳转、官方来源会被排到后面） | 排序/native 标记默认生效；链接还原仍看 `vertex_redirect_resolve` |

说明：
- S1 只改「发给后端的请求」，**不改变**工具返回给主模型的字段与结构；`queries` 仍是后端
  实际检索词。缓存 key 按北京日期分桶，同一天内命中、跨天自动失效。
- S7 的复核重试**每个 `search()` 最多 1 次**；重试仍失败只保留首次结果（或结果更多的那份），
  不会无限循环，也不改变原有「失败不计数」规则。
- S11 与 S12 的 `resolved` 标记只作用于 Gemini 原生后端；S12 的官方排序对 native 与
  Responses/fallback 都生效（Responses 不新增字段、结构与 0.2.3 一致，便于回滚与对比）。

## 冲突处理

搜索工具是「请求级注入」，冲突处理也只作用于**当前这一条请求**，不影响其它插件：

- `prefer_self`（默认）：注入前从本次请求的 ToolSet 移除识别到的其它搜索工具，并写日志说明移除了哪些；
- `keep_all`：全部保留，只告警；
- `skip_self`：发现其它搜索工具时不注入本插件工具，让位。

识别范围：AstrBot 内置的 16 个 `web_search_*` / 提取工具、`anysearch_*`，以及本插件工具名；
其它插件可用「额外视为冲突的工具名」补充。

## 安全

- 搜索查询会发送给**当前聊天模型的 provider**（本机部署通常是 `127.0.0.1:8080` 反向代理），
  不会额外发给第三方搜索服务。
- 日志只记 provider id、模型名、耗时、token 用量、query 预处理结果（原 query / 处理后
  query，各截前 200 字）与响应形态（类型/计数），不记 API Key、不抄响应正文；错误文本里的
  响应体只截前 200 字。
- 搜索结果文本来自外部网页，对模型来说是不可信输入；本插件不读本地文件、不访问内网地址。
  默认也不抓取 URL；开启 `vertex_redirect_resolve` 时只对 `vertexaisearch.cloud.google.com`
  做一次**不跟随重定向**的 GET 取 `Location`，并拒绝非 http(s) 与私网/环回目标。

## 失败行为

任何失败（provider 不支持 Responses API、超时、限流、鉴权失败、模型没有真正执行搜索）
都以 `错误：…` 文本返回给模型，不会抛出异常打断对话，也不会静默返回空结果冒充成功。

## 排障

| 现象 | 可能原因 / 处理 |
|---|---|
| 模型完全不搜索、也不报错 | `websearch_provider` 不是 `model_native`，或 `web_search=false`；检查 profile 的三个字段 |
| 日志有 `[model_websearch] loaded`，但请求里没有工具 | 插件被本插件级「禁用网页搜索」关掉，或冲突策略是 `skip_self` 且请求里有别的搜索工具（看日志移除/让位记录） |
| `错误：当前聊天模型…不支持 OpenAI Responses API` | 换支持 Responses 的模型，或改用插件 `fixed` 模式指定一个支持 Responses 的 provider |
| HTTP 200 但 `0 次 web_search_call` | 该模型不支持/忽略 `web_search`（如 `deepseek-flash`）；换模型 |
| `错误：搜索超时（>45s…）` | 模型侧搜索慢；调大超时或在控制台改用更快的搜索模型 |
| Gemini 返回 HTTP 404 / 无 `groundingMetadata` | 该反代没有暴露 `/v1beta` 原生层：保持 `auto` 让 fallback 生效，或把 `gemini_search_backend` 设为 `responses`；日志看 `backend=`、`grounding=none`、`endpoint=` |
| Gemini 结果 URL 全是 `vertexaisearch…` | 正常：`groundingChunks` 是跳转链接。开启 `vertex_redirect_resolve` 还原真实 URL（会慢），或改用 Responses 路径拿直链 annotations |
| `错误：Gemini 原生搜索超时（>90s…）` | argon 等模型较慢；调大 `gemini_timeout_seconds` 或换更快的搜索模型 |
| 日志出现 `backend=responses_fallback` | 原生后端失败已回退 Responses（WARN 行有原因）；想彻底走原生先排查 `/v1beta` 与 grounding |
| 同一轮只搜了 2 次就不搜了 | 正常行为：`max_calls_per_turn` 默认 2；需要更多可调大（会更慢） |
| `/msearch` 报“已被禁用” | 插件配置「禁用网页搜索」开着 |
| 搜索内容明显过时 | 缓存（默认 300s）命中；调小或设 0 |
| 日志出现 `query_guard 质量告警` | query 同时含「今天/today」+ 外文绝对日期或 `site:`（主模型自行换算日期/锁页）；插件仍会自动注入北京时间前缀并剥离 `site:`，如频繁出现可检查工具描述是否被其它提示词覆盖（S3/S10） |
| 日志出现 `触发可疑结果自动复核 retry=1` | 首次结果像「没有比赛/无赛程」且无明确对阵，已自动重试 1 次；看 `improved=True/False` 判断是否救回。不想重试可关 `suspicious_result_retry`（S7） |
| 结果 URL 带 `resolved: false` | 该链接仍是 vertexaisearch 跳转、未还原；开启 `vertex_redirect_resolve` 可还原真实 URL（S12） |
| 回答里的来源片段与网页正文不一致 | native `snippet` 来自模型答案的 grounding 片段（`snippet_source=model_answer_segment`），不是网页原文；需要正文请让模型访问链接（S11） |
| 「今天没有比赛」结论仍出现 | 先看日志 `query_guard prepared=` 是否含北京时间前缀、`retry=` 是否触发；再核对来源地日期与北京日期换算（S1/S2/S6/S7），必要时用 `/msearch 用户原话` 对照 |

## 开发与测试

仓库结构：

```text
main.py                  插件注册、on_llm_request 注入、/msearch
client.py                provider 解析、Responses 调用、Gemini 原生 grounding 后端、结果解析、缓存、限流、错误映射
tools.py                 构造 web_search_model_native FunctionTool
_conf_schema.json        插件配置 schema
metadata.yaml            插件元数据（AstrBot 版本要求、支持平台）
tests/test_offline.py    离线单测（不联网；含 Gemini 原生/fallback/vertex 与 0.2.4 正确率防护用例）
scripts/run-offline-tests.py   单测入口（AstrBot 用最小 stub 替代）
scripts/build-plugin-zip.py    打可安装 zip
scripts/release-notes.py       从 CHANGELOG 生成发布说明
```

本地跑测试与打包（不需要安装 AstrBot，只需要 Python 3.10+）：

```bash
python scripts/run-offline-tests.py       # 85 项离线单测
python scripts/build-plugin-zip.py        # 生成 dist/astrbot_plugin_model_websearch-v0.2.4.zip
python scripts/build-plugin-zip.py --check
```

CI（`.github/workflows/ci.yml`）在 Python 3.10 / 3.12 上跑同一套测试与打包校验；
推 `v*` tag 时 `.github/workflows/release.yml` 跑测试 → 校验 tag 与 `metadata.yaml` 版本一致 →
构建 zip → 创建 GitHub Release（资产含 zip 与 `.sha256.txt`）。发布说明缺失会直接失败。

## 配套项目

- [**astrbot-model-console**](https://github.com/CBlazed/astrbot-model-console) —— AstrBot 模型控制台（Windows 便携版）：
  在浏览器里集中查看/切换模型绑定与网页搜索设置（启用开关、提供商、内置搜索 API Key、搜索使用模型），
  改完先预览 → 备份 → 写入，自带「测试 Key」与「测试模型原生搜索」两个真实探测按钮。
  本插件的 `model_native` 提供商选项、`websearch_native_provider_id`（搜索使用模型）以及插件启停状态检测都是与它配套设计的，
  详见上文「[与模型控制台（model-console）的联动](#与模型控制台model-console的联动)」。

## 许可

[MIT](LICENSE) © 2026 CBlazed
