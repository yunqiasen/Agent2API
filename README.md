# Agent2API

WorkBuddy / TraeWork / CodeBuddy（国际）三平台 **hy3 模型反向代理 + 号池控制面板**。
对外暴露标准 OpenAI 兼容接口，对内做 SSO 登录、令牌自动续期、号池调度、额度监控与调用鉴权。

> 接口契约以仓库内 `tests/` 的回归测试和当前容器实测为准；上游模型、额度与签到结果会随账号和服务端状态变化。

---

## 1. 快速开始

```bash
# 依赖：Python 3.10+，flask / requests / waitress
pip install flask requests waitress
```

启动前先构建前端（裸跑 Flask 不会自动编译 React）：

```bash
cd frontend
npm install
npm run build
cd ..
python app.py
```

启动后：

| 入口 | 地址 |
|---|---|
| 账号贡献页（默认首页） | http://127.0.0.1:8530/ |
| 管理后台登录 | http://127.0.0.1:8530/login |
| OpenAI Base URL | http://127.0.0.1:8530/v1 |
| 健康检查 | http://127.0.0.1:8530/health |

裸跑首次初始化默认管理员密码为 `admin`、默认调用 Key 为 `Agent2API`；Docker Compose 部署必须先在 `.env` 中显式设置，避免公开默认凭据。

调用示例：

```bash
curl http://127.0.0.1:8530/v1/chat/completions \
  -H "Authorization: Bearer Agent2API" \
  -H "Content-Type: application/json" \
  -d '{"model":"hy3","messages":[{"role":"user","content":"你好"}]}'
```

流式加 `"stream": true` 即可，返回标准 SSE。

---

## 2. 三平台对照（实测确认）

| 平台 | key | 登录域 | platform 参数 | 模型网关 | 会话头 |
|---|---|---|---|---|---|
| WorkBuddy | `workbuddy` | `www.codebuddy.cn` | `workbuddy` | `copilot.tencent.com` | `X-WorkBuddy-Session-Id` |
| TraeWork | `trae` | `www.trae.cn` | `trae` | `trae-api-cn.mchost.guru` | SOLO 专用头 |
| CodeBuddy 国际 | `codebuddy` | `www.codebuddy.ai` | `ide` | `www.codebuddy.ai` | `X-CodeBuddy-Session-Id` |

### 关键实测结论

1. **WorkBuddy 与 CodeBuddy CN 使用同一套账号体系，本项目只保留 WorkBuddy 渠道。**
   两者共用国内账号、额度和 `copilot.tencent.com` 网关；旧注册表里的 `codebuddy_cn` 引用会在启动和模型刷新时迁入 `workbuddy`，避免重复号池，同时不丢历史模型。

2. **TraeWork 是独立渠道。**
   Trae 使用 SOLO 专用请求头、请求体和 `trae-api-cn.mchost.guru` 网关，账号与国内 WorkBuddy 额度分开。

3. **CodeBuddy 国际站是完全独立的账号与网关。**
   国际账号使用 `www.codebuddy.ai`，与国内 WorkBuddy、TraeWork 的账号和额度分开。

4. **上游只支持流式。**
   `stream=false` 一律返回 `400 {"code":11101,"msg":"Non-stream chat request is currently not supported"}`。
   → 本服务**始终以 `stream=true` 请求上游**；客户端要非流式时，由本层把 SSE 聚合成标准 `chat.completion` 响应体。

5. **国际站强制首条 system 消息。**
   `messages[0].role` 必须是 `system`，否则一律 `400 Parse message failed: 11101:invalid request`。
   实测矩阵：仅 user ❌ / 仅 system ✅ / system+user ✅ / user+system（顺序颠倒）❌ / 空 system + user ✅ / 多轮但无 system ❌ / system+多轮 ✅。
   → `providers.normalize_body()` 会在命中该平台时自动插入一条 system 消息（内容取设置里的「兜底 system 提示词」）。CN 网关无此限制，补齐也无副作用。

6. **刷新令牌走请求头，不是 body。**
   `POST {api_base}/v2/plugin/auth/token/refresh`，头里带 `Authorization: Bearer {access}` + `X-Refresh-Token: {refresh}`，body 为 `{}`。三平台一致。

7. **上游没有 `responses` 接口，但本代理提供。** `/v1/responses`、`/v2/responses` 上游均 404。
   → 本服务在反代层**把 OpenAI Responses API 转译为 `chat/completions`**（`responses.py`）：
   入参 `input`/`instructions`/`tools`/`tool_choice`/`text.format` 翻译为 messages/tools；
   上游 SSE 流实时转写成 Responses 事件（或聚合成 `response` 对象返回非流式）。
   详见第 5 节。

8. **程序不内置对外模型目录，模型列表来自官方 CLI 目录与运行时真实探测。** WorkBuddy / CodeBuddy 可读取 CLI 白名单，TraeWork 读取 `get_detail_param`；官方目录不可用或未覆盖的模型再用最小请求验证。探测逻辑集中在 `discovery.py`：
   - `PROBE_SEED` 只是一份「要尝试探测的候选 ID 清单」（文本/图像/音频都涵盖），**绝不是对外目录**；探测不到的候选会被自动丢弃，探测到的才进入注册表；
   - 每次「刷新模型目录」或首次部署启动，会合并各平台所有启用账号能读取到的官方 CLI 目录，再对候选做最小请求，把确认结果写入 **数据库注册表**（`db.get_model_registry()` / `discovered_models` 设置），并记录其 `type`（chat / image / audio）与可用 `platforms`；
   - 客户端请求一个注册表外的模型时，请求链路会触发 `live_probe_model` 实时探测回退，命中即写入注册表；探测不到则记入负面缓存并在限定 TTL 内直接返回 503，避免反复盲打上游；
   - 对外 `/v1/models`、面板「模型」页、以及请求调度（chat / 图像端点分流）**全部读这张注册表**，代码里不写死任何模型名。
   已探测固化的真实模型示例（仅供理解，不作为代码内常量）：
   - **文本**：`hy3`、`glm-5.2/5.1/5.0`、`glm-5v-turbo`、`minimax-m3`、`kimi-k3/k-2.7/k-2.6/k-2.5`、`auto`、`deepseek-v4-flash`、`deepseek-v3.2`
   - **图像**：`hunyuan-image-v3.0-art`、`hunyuan-image-v3.0`（走独立的 `/v2/images/generations` 端点）
   - **音频**：`hunyuan-tts` 等（类型标注 audio，具体可用 ID 以运行时探测为准）
   - `kimi-k2.7-code` 三网关均返回 `11102`（上游未注册），不进入目录、请求时直接 503 拦截。
   展示目录不等于每个平台都支持全部模型，实际请求由注册表里的 `platforms` 决定（见下）。

9. **请求按模型类型与注册表平台分流，不盲选账号。** 未绑定平台的 Key 使用数据库注册表里该模型的 `platforms`（由 `proxy.model_route()` 读取，必要时触发 `live_probe_model` 实时探测）：
   - 文本模型 `hy3 / glm-* / minimax-m3 / kimi-k3 / kimi-k2.5 / kimi-k2.6` 可走 **三平台**，优先国内 WorkBuddy，国际 CodeBuddy 作故障/额度兜底；
   - `auto / deepseek-v4-flash / deepseek-v3.2 / kimi-k2.7` 仅国内两网关（国际站返回 `11102`）；
   - 图像模型 `hunyuan-image-*` **只走国内网关的 `/v2/images/generations`**（国际站 `11102`；chat 网关返回 `11103 aiart 不支持`）；
   - 音频模型按 `audio` 类型标注，走 chat 网关（端点能力由上游决定，探测确认即收录）。
   Key 显式绑定平台时绑定优先，不会越权切到其它平台。上游返回 `11102 / 11103 / service info not found / model [...] not found` 这类明确的「模型未注册」错误时，会先继续尝试该渠道的其它账号（账号套餐可能不同），该渠道都失败后再切换候选渠道；普通参数 400 原样返回，避免掩盖客户端错误。模型 ID 在注册表里无可用平台（且实时探测也失败）时，服务返回清晰 503，绝不在所有账号中随机碰运气。

10. **图像生成走独立端点 `/v1/images/generations`。** 图像模型（混元图像系列）与对话模型不共用 chat 网关，请求体为 OpenAI 兼容格式 `{"model","prompt","n","size",...}`，上游返回 `data.data[].url` 与 `usage.credit`。任务队列上限（`13212 JobNumExceed`）按可重试限流处理。

11. **Token 与积分统计只认上游真实 `usage`。** 非流式请求从聚合后的最终 SSE 帧读取 `prompt_tokens` / `completion_tokens` / `total_tokens`；流式 chat 使用旁路解析器，在不改动透传字节的前提下捕获最终帧；Responses 流也会把原始 usage 回传给日志层。积分直接记录上游 `usage.credit`，包括有效的 `credit=0`，不再使用 `tokens × 固定单价` 人工换算。老版本产生的估算积分因无法无损恢复，保留日志但标记为非真实来源，不计入面板的真实积分合计与扣费明细；上游未返回 usage 时 token 记为 0（未知），不会按字符数伪造。

12. **上游偶发 SSL 抖动。** `copilot.tencent.com` 会间歇性 `SSLEOFError`。已在两处加重试：同账号网络层重试（`proxy.NET_RETRY = 2`）+ 登录/额度类幂等请求的 `urllib3.Retry`。

13. **上游 SSE 不带 charset。** requests 会退化成 ISO-8859-1 解码导致中文乱码。已强制 UTF-8 解码，流式透传则直接转发原始字节。

---

## 3. 功能

**号池**
- 三种方式加号：网页 SSO 登录、手填 access/refresh token、粘贴 Cockpit Tools 导出的 JSON（支持数组，按平台+uid 原位更新；旧渠道迁移会自动合并重复账号）
- 令牌自动续期：按 `expires_at` 提前 `refresh_ahead` 秒刷新，带账号级锁避免并发重复刷新；后台线程每 5 分钟巡检
- 调度策略：先按模型路由到国内/国际候选平台，再在平台内执行 `least_used`（默认）/ `round_robin` / `random`
- 失败自动换号重试；401 触发强制刷新后再试；HTTP 200 包装的 SSE 业务错误也会在返回客户端前识别并换号；单次请求最多尝试 9 个账号，按候选渠道均分，避免坏号池长期占满 Worker
- 额度展示严格使用上游实时口径：TraeWork 读取 `usage_summary.total_amount / consumed_amount` 并保留真实套餐列表；刷新失败保留上一份成功数据并报错，不用 0 覆盖；真实同步到 0 时明确显示 0，不再写成“未同步”
- 账号卡片使用事实标签：`已接入`、`未过期`、`本站调用`；TraeWork 未返回连签天数时隐藏该字段，不伪造“连续 0 天”
- 单号启停、删除、权重、备注
- 批量签到以上游为准：单号、批量、自动签到都逐号查询上游状态；上游未签才领取，已签才跳过。本地 `checkin_today` 只做页面缓存，不参与是否请求的判断；支持按当前渠道签到，前端与容器日志均显示逐号成功/跳过/失败明细
- TraeWork 每日签到：当前统一使用凭证中的 `uid` 作为 `X-Device-Id`，因为实测它同时适配扫码登录与 Token 导入账号；模型/额度请求仍会携带凭证里的 machine/device 字段。实测（2026-08-31）全号池签到链路均能得到上游已签到结果；9074 文案「参与用户太多」不能直接当作容量结论，面板会保留原始错误。
- 渠道×模型开关：模型页每个渠道卡片内按模型开/关（如关闭 WorkBuddy 的 glm-5.3）；
  开关全局生效——真实调用、Key 显式绑定、对外 `/v1/models` 清单一并拦截；
  三渠道全关的模型从清单隐藏；测活与模型探测不受开关影响

**调用鉴权**
- 自定义 API Key，支持 `Authorization: Bearer`、`x-api-key`、`?key=` 三种传法
- Key 可绑定平台 —— 该 Key 的请求只命中对应平台的账号；模型与绑定冲突时返回清晰错误，不会越权切平台
- Key 可单独启停、统计调用次数；无合法 Key 一律 401

**其它**
- 调用日志：时间 / 平台 / 账号 / 模型 / 流式 / 状态码 / 耗时 / 输入字符数 / 错误
- 面板内一键连通性测试（走完整反代链路）；HTTP 200 业务错误和 SSE error 事件不会再被标成成功
- 管理员密码修改（sha256 加盐存储）

---

## 4. 文件结构

```
Agent2API/
├── app.py               Flask 应用：OpenAI 接口 + 面板 API + 后台维护线程
├── providers.py         平台适配层：SSO、刷新、额度、请求头、body 规范化
├── proxy.py             反代引擎：令牌保活、号池调度、流式转发、非流式聚合
├── responses.py         Responses API 翻译层：input/tools → chat，SSE 实时转写
├── db.py                SQLite 数据层：settings/accounts/api_keys/logs/pending_login
├── frontend/             React + Vite + shadcn 风格控制面板源码
│   ├── src/               页面与 UI 组件
│   ├── package.json       前端依赖与构建脚本
│   └── dist/              npm run build 生成的生产静态文件
├── tests/                 Python 回归测试（pytest）
├── Dockerfile             默认运行镜像（需要先构建 frontend/dist）
├── Dockerfile.fullbuild   多阶段镜像（容器内自动构建前端）
├── docker-compose.yml     Compose 部署配置
├── .env.example           Docker 首次初始化变量模板
├── .dockerignore          排除数据库、凭证、依赖与构建缓存
└── agent2api.db           SQLite 数据库（自动创建，不进入 Git）
```

数据库路径可用环境变量 `AGENT2API_DB` 覆盖；持久化目录用 `AGENT2API_DATA_DIR`（容器内指向 `/data` 卷）。会话密钥用 `AGENT2API_SECRET`（不设则首次启动自动生成并持久化到库里，重启不掉线）。详见第 8、9 节。

---

## 5. OpenAI 兼容接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 返回对外模型列表 |
| POST | `/v1/chat/completions` | 支持 `stream` true/false |
| POST | `/v1/responses` | OpenAI Responses API（由反代层转译为 `chat/completions`） |

`/models`、`/chat/completions`、`/responses`（不带 `/v1`）为等价别名。

### 5.1 Responses API（`/v1/responses`）

上游（WorkBuddy / CodeBuddy）只暴露 `chat/completions` 且只支持流式。本服务在反代层
把 OpenAI Responses API 翻译过去，因此**任何使用 Responses API 的 SDK / 工具都能直接对接**
（把 `base_url` 指到 `http://<host>:8530/v1`、API Key 用面板里的 Key 即可）。

**支持的入参翻译**

- `input`：字符串 → 单条 user 消息；数组 → 逐条翻译
  - `message` 项（`role` + `content`）→ 普通 message
  - `content` 支持 `input_text` / `input_image`（`image_url`）parts
  - `function_call` 项（历史工具调用）→ assistant 带 `tool_calls`
  - `function_call_output` 项 → `tool` 角色消息
  - `system` 角色消息 → system message
- `instructions` → 置顶的 system message
- `tools` / `tool_choice` → chat 的 `tools` / `tool_choice`
  - 注意：上游仅接受**字符串** `tool_choice`（`auto`/`none`/`required`），
    不支持 OpenAI 的对象形式 `{type:"function",...}`，本层会**降级为 `required`**（单工具场景等价）。
- `text.format` → `response_format`（如 `{"type":"json_object"}`）
- `max_output_tokens` → `max_tokens`；`temperature` / `top_p` 等其余字段透传
- `previous_response_id` / `include` / `store` / `truncation` 等：本层不持久化对话，忽略

**返回格式**

- 非流式：`stream:false`（或省略）→ 返回标准 `response` 对象
  （`output` 含 `message` 项与 `function_call` 项，附 `usage`）
- 流式：`stream:true` → 返回 Responses 格式的 SSE 事件流：
  `response.created` → `response.in_progress` → `response.output_item.added`
  → `response.content_part.added` → `response.output_text.delta`（逐片）
  → `response.output_text.done` → `response.content_part.done`
  → `response.output_item.done` → `response.completed`
  （工具调用场景额外产生 `response.output_item.added`(function_call) /
  `response.function_call_arguments.delta` / `.done`）

调用示例（非流式）：

```bash
curl http://127.0.0.1:8530/v1/responses \
  -H "Authorization: Bearer Agent2API" \
  -H "Content-Type: application/json" \
  -d '{"model":"hy3","input":"用一句中文介绍你自己","stream":false}'
```

> 说明：是否真正触发函数调用取决于上游模型本身（如 `hy3`/混元当前不会主动调用工具），
> 但 `input` → `messages` 与 `tool_calls` → `function_call` 的双向翻译均已实现并通过单测。

---

## 6. 实测结果（2026-09-01）

本地回归测试：`.venv-test/bin/pytest tests/ -q`。真实上游模型、额度和签到结果以当前号池实测为准。

| 组 | 内容 | 结果 |
|---|---|---|
| T1 | 本机 `/health`、Tailscale `100.126.43.55:8530/health` | PASS |
| T2 | 管理员登录、未登录拦截、登录后管理 API 放行 | PASS |
| T3 | 现有数据库 16 个账号：WorkBuddy 7 / TraeWork 6 / CodeBuddy 国际 3 | PASS |
| T4 | SQLite `integrity_check=ok`、同渠道 UID 重复 0、旧渠道记录/日志 0 | PASS |
| T5 | `/v1/models` 当前对外 13 个模型，渠道开关与 Key 绑定共同生效 | PASS |
| T6 | `hy3` 非流式真实对话返回 `OK` | PASS |
| T7 | TraeWork `glm-5.2` Responses 流返回文本增量与 `response.completed` | PASS |
| T8 | 全部签到命中 13 个可签到账号：WorkBuddy 7 / TraeWork 6，失败 0 | PASS |
| T9 | 模型调用优先级保存后回读 JSON 一致 | PASS |
| T10 | 容器重建后健康、账号、模型、设置与历史数据保持 | PASS |
| T11 | 114 项 Python 回归、TypeScript 类型检查、Vite 构建、容器健康检查 | PASS |

---

## 7. 安全提醒

- Docker Compose 要求在 `.env` 显式设置管理员密码和默认 Key；只有直接裸跑且未提供环境变量时才会用 `admin` / `Agent2API` 初始化。现有数据库始终以库内值为准。
- 面板与 API 无 TLS，公网部署请放在 Nginx/Caddy 反代之后并启用 HTTPS
- 数据库明文存储 access/refresh token，注意文件权限
- 号池账号额度有限，建议给不同调用方分配独立 Key 以便追踪与吊销

---

## 8. 环境变量

所有配置项均可通过环境变量注入，容器化与裸跑通用。 `HY3API_*` 旧变量名仍兼容。优先级：**命令行参数 `--port` / `--host` > 环境变量 > 内置默认值**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT2API_HOST` | `0.0.0.0` | 监听地址 |
| `AGENT2API_PORT` | `8530` | 监听端口 |
| `AGENT2API_THREADS` | `16` | waitress 工作线程数 |
| `AGENT2API_DB` | 代码目录 `agent2api.db` | 直接指定 SQLite 文件路径（最高优先级） |
| `AGENT2API_DATA_DIR` | 空 | 数据库目录，文件为 `<dir>/agent2api.db`（容器持久化用） |
| `AGENT2API_SECRET` | 自动生成并持久化 | 会话签名密钥；不设则首次启动生成并存入库，重启不掉线 |
| `AGENT2API_ADMIN_PASSWORD` | `admin` | 首次初始化管理员密码；库已存在时不生效 |
| `AGENT2API_DEFAULT_KEY` | `Agent2API` | 首次初始化默认调用 Key；库已存在时不生效 |

> `AGENT2API_ADMIN_PASSWORD` 与 `AGENT2API_DEFAULT_KEY` **只在数据库尚不存在（全新 volume）时用于初始化**。一旦已在面板里改过密码 / Key，环境变量不再覆盖，避免把正在运行的实例凭据冲掉。

---

## 9. Docker 部署

镜像基于 `python:3.12-slim`，以非 root 用户（`agent2api`）运行，SQLite 数据通过卷挂载持久化，内置纯标准库的健康检查（`HEALTHCHECK` 探活 `/health`，无需额外依赖）。

### 9.1 前置条件

- Docker Engine 24+
- Docker Compose v2（`docker compose` 子命令）

### 9.2 选择构建方式

项目提供两种镜像构建方式：

| 方式 | 适用场景 | 操作 |
|---|---|---|
| 默认 `Dockerfile` | 推荐；已带可用 `frontend/dist`，构建快、服务器无需 Node | 上传完整项目后直接 `docker compose up -d --build`；如果改过前端源码，先在本地执行 `npm install && npm run build` |
| `Dockerfile.fullbuild` | 只上传源码、希望服务器自动编译前端 | 在 `docker-compose.yml` 的 `build` 下取消 `dockerfile: Dockerfile.fullbuild` 注释，再执行 `docker compose up -d --build` |

**默认方式（当前目录已经包含最新 `frontend/dist`，可直接部署）：**

```bash
# 仅当改过 frontend/src 时，先重新生成 dist
cd frontend
npm install
npm run build
cd ..

# 首次部署：创建本地环境文件并填写三个值
cp .env.example .env
# 编辑 .env：管理员密码、调用 Key、会话密钥均不可留空

# 构建并启动
docker compose up -d --build
```

**服务器自动构建方式：**

```yaml
# docker-compose.yml
build:
  context: .
  dockerfile: Dockerfile.fullbuild
```

```bash
docker compose up -d --build
```

> `Dockerfile.fullbuild` 会下载 Node 22 镜像并安装 npm 依赖，首次构建更慢、也更依赖服务器网络。它使用 `npm install` 而不是 `npm ci`，以便在 Linux 构建阶段重新解析 Tailwind/Rollup 的平台原生可选依赖。

启动后：

- 账号贡献页：http://\<host\>:8530/
- 管理后台登录：http://\<host\>:8530/login
- API Base URL：http://\<host\>:8530/v1
- 健康检查：http://\<host\>:8530/health

### 9.3 自定义凭据与端口

编辑本地 `.env`（该文件已被 Git 忽略）：

```dotenv
AGENT2API_ADMIN_PASSWORD=改成你自己的强密码
AGENT2API_DEFAULT_KEY=sk-your-own-key
AGENT2API_SECRET=固定随机长字符串
```

> 管理员密码和默认 Key 只在**首次启动（volume 为空）**时用于初始化；已有数据库以面板和数据库中的值为准。

### 9.4 数据持久化

`docker-compose.yml` 已声明命名卷 `agent2api-data` 挂载到容器 `/data`（即 `AGENT2API_DATA_DIR=/data`）。号池账号、API Key、调用日志、设置、会话密钥全部落在此卷，**容器重建 / 升级不丢数据**。

```bash
docker volume inspect hy3api_agent2api-data     # 查看数据卷在宿主机的真实路径

# 如果主机已安装 Tailscale，直接使用 Tailscale IP 访问
curl http://$(tailscale ip -4):8530/health
```

### 9.5 自行构建镜像

```bash
# 默认：打包现成的 frontend/dist
docker build -t agent2api:latest .

# 备选：在镜像的 Node 阶段自动构建前端
# docker build -f Dockerfile.fullbuild -t agent2api:latest .

docker run -d --name agent2api -p 8530:8530 -v agent2api-data:/data \
  -e AGENT2API_DATA_DIR=/data \
  -e AGENT2API_ADMIN_PASSWORD=你的密码 \
  -e AGENT2API_DEFAULT_KEY=sk-your-own-key \
  agent2api:latest
```

### 9.6 运维

```bash
docker compose ps            # 状态（含 healthcheck 结果）
docker compose logs -f       # 实时日志（启动横幅、请求记录）
docker compose restart       # 重启，数据不丢（session 密钥已持久化）
docker compose down          # 停止并移除容器（数据卷保留）
```

### 9.7 启动后验证

```bash
curl -fsS http://127.0.0.1:8530/health
```

进入面板用 `AGENT2API_ADMIN_PASSWORD` 登录；首次加号请在面板内走 SSO 或粘贴 token / Cockpit JSON（真实 token 不会进入镜像，`.dockerignore` 已排除数据库与凭证文件）。

### 9.8 安全

- 容器本身无 TLS，公网暴露务必放在 Nginx / Caddy 之后并启用 HTTPS。
- 数据库内含明文 access / refresh token，卷权限需控制好，不要分享数据卷。
- 推荐给不同调用方分配独立 Key 以便追踪与吊销。

### 9.9 容器化适配验证

容器化验证以当前 `docker compose` 实例为准：构建后检查健康状态、`/health`、`/api/health`、登录态与 `/v1/models`；本地开发也可用同一组命令复跑：

- **容器启动**：端口、数据目录和健康检查均使用 `AGENT2API_*` 配置。
- **数据持久化**：命名卷保留账号、Key、设置、日志和会话密钥。
- **升级重建**：重建镜像不替换已有数据库。

```bash
docker compose ps
curl -fsS http://127.0.0.1:8530/health
curl -fsS http://127.0.0.1:8530/api/health
```
