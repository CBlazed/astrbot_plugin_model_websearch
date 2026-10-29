# 模型原生网页搜索 · astrbot_plugin_model_websearch

[![ci](https://github.com/CBlazed/astrbot_plugin_model_websearch/actions/workflows/ci.yml/badge.svg)](https://github.com/CBlazed/astrbot_plugin_model_websearch/actions/workflows/ci.yml)
[![release](https://github.com/CBlazed/astrbot_plugin_model_websearch/actions/workflows/release.yml/badge.svg)](https://github.com/CBlazed/astrbot_plugin_model_websearch/actions/workflows/release.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.29%2C%20%3C5-0b7285)](metadata.yaml)

**下载**：[Releases 插件包](https://github.com/CBlazed/astrbot_plugin_model_websearch/releases/latest)

把 AstrBot「AI 能力 → 网页搜索」的检索后端换成**当前聊天模型自带的原生联网搜索**
（OpenAI Responses API `tools=[{"type":"web_search"}]`），不必再单独配置
Tavily / DeepSeek / Baidu 等第三方搜索 API。

适用前提：聊天模型 provider 提供 OpenAI **Responses API** 且支持 `web_search` 工具
（本机 <provider> 的 `<model>` / `<model-3>` / `<model-4>` 已验证可用）。

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
[model_websearch] loaded: disabled=False mode=current fixed_id=- conflict=prefer_self cache_ttl=300 rate=6/min
[model_websearch] provider=<provider>/<model> model=<model> elapsed=6.6s results=8 queries=2 usage={...}
```

## 工作原理

1. AstrBot 本体配置：`provider_settings.web_search = true`，
   `provider_settings.websearch_provider = "model_native"`（哨兵值；4.29.3 核心对未知值不添加内置搜索工具）。
2. 插件在 `on_llm_request` 阶段检测到上述配置，向**本次请求**注入工具
   `web_search_model_native(query, max_results)`。
3. 模型调用工具时，插件解析要用的 provider（优先级见上文），通过它的
   `provider.client.responses.create(...)` 发起请求：
   `tools=[{"type":"web_search","search_context_size":"low"}]`。
4. 返回 JSON：`results`（标题/链接/摘要）、`answer`（默认关闭，为空）、
   `queries`（实际检索词）、`sources`（来源引用）。
5. 同一轮用户消息内最多真实搜索 `max_calls_per_turn` 次；达到上限后工具不再发起模型调用，
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
| 单次搜索超时（秒） | 45 | 超时以 `错误：搜索超时…` 返回给模型 |
| 相同查询结果缓存时间（秒） | 300 | 0=关闭；缓存命中不计频率 |
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
- **插件配置 fixed**：`provider_mode=fixed` + `provider_id=<provider>/<model-3>`。

注意：不是所有模型都支持 Responses `web_search`。可用 model-console 的
「测试模型原生搜索」实测；例如 DeepSeek 官方目前仅 `deepseek-v4-pro` 支持，
`deepseek-flash` 会忽略该工具（HTTP 200，但 0 次搜索调用）。

## 冲突处理

搜索工具是「请求级注入」，冲突处理也只作用于**当前这一条请求**，不影响其它插件：

- `prefer_self`（默认）：注入前从本次请求的 ToolSet 移除识别到的其它搜索工具，并写日志说明移除了哪些；
- `keep_all`：全部保留，只告警；
- `skip_self`：发现其它搜索工具时不注入本插件工具，让位。

识别范围：AstrBot 内置的 16 个 `web_search_*` / 提取工具、`anysearch_*`，以及本插件工具名；
其它插件可用「额外视为冲突的工具名」补充。

## 安全

- 搜索查询会发送给**当前聊天模型的 provider**（本机部署通常是 `127.0.0.1:<端口>` 反向代理），
  不会额外发给第三方搜索服务。
- 日志只记 provider id、模型名、耗时、token 用量，不记 API Key。
- 搜索结果文本来自外部网页，对模型来说是不可信输入；本插件不读本地文件/内网地址，也不抓取 URL。

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
| 同一轮只搜了 2 次就不搜了 | 正常行为：`max_calls_per_turn` 默认 2；需要更多可调大（会更慢） |
| `/msearch` 报“已被禁用” | 插件配置「禁用网页搜索」开着 |
| 搜索内容明显过时 | 缓存（默认 300s）命中；调小或设 0 |

## 开发与测试

仓库结构：

```text
main.py                  插件注册、on_llm_request 注入、/msearch
client.py                provider 解析、Responses 调用、结果解析、缓存、限流、错误映射
tools.py                 构造 web_search_model_native FunctionTool
_conf_schema.json        插件配置 schema
metadata.yaml            插件元数据（AstrBot 版本要求、支持平台）
tests/test_offline.py    离线单测（不联网）
scripts/run-offline-tests.py   单测入口（AstrBot 用最小 stub 替代）
scripts/build-plugin-zip.py    打可安装 zip
scripts/release-notes.py       从 CHANGELOG 生成发布说明
```

本地跑测试与打包（不需要安装 AstrBot，只需要 Python 3.10+）：

```bash
python scripts/run-offline-tests.py       # 26 项离线单测
python scripts/build-plugin-zip.py        # 生成 dist/astrbot_plugin_model_websearch-v0.2.0.zip
python scripts/build-plugin-zip.py --check
```

CI（`.github/workflows/ci.yml`）在 Python 3.10 / 3.12 上跑同一套测试与打包校验；
推 `v*` tag 时 `.github/workflows/release.yml` 跑测试 → 校验 tag 与 `metadata.yaml` 版本一致 →
构建 zip → 创建 GitHub Release（资产含 zip 与 `.sha256.txt`）。发布说明缺失会直接失败。

## 许可

[MIT](LICENSE) © 2026 CBlazed
