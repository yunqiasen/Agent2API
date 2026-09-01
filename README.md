# Agent2API

WorkBuddy / CodeBuddy CN / CodeBuddy（国际）三平台 **hy3 模型反向代理 + 号池控制面板**。
对外暴露标准 OpenAI 兼容接口，对内做 SSO 登录、令牌自动续期、号池调度、额度监控与调用鉴权。

> 全部接口行为均基于 **2026-08-05 真实账号实测**得出，测试脚本与原始输出保留在仓库中（`e2e_test.py` / `e2e_out.txt`）。

---

## 1. 快速开始

```bash
# 依赖：Python 3.9+，flask / requests / waitress
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
| 账号贡献页（默认首页） | http://127.0.0.1:8787/ |
| 管理后台登录 | http://127.0.0.1:8787/login |
| OpenAI Base URL | http://127.0.0.1:8787/v1 |
| 健康检查 | http://127.0.0.1:8787/health |

**默认管理员密码 `admin`，默认调用 Key `Agent2API`** —— 上线前请务必在面板里改掉。

调用示例：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
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
| CodeBuddy CN | `codebuddy_cn` | `www.codebuddy.cn` | `ide` | `copilot.tencent.com` | `X-CodeBuddy-Session-Id` |
| CodeBuddy 国际 | `codebuddy` | `www.codebuddy.ai` | `ide` | `www.codebuddy.ai` | `X-CodeBuddy-Session-Id` |

### 关键实测结论

1. **WorkBuddy 与 CodeBuddy CN 是同一套账号体系。**
   同一 Keycloak realm（`https://www.codebuddy.cn/auth/realms/copilot`）、同一 uid、同一份 2400 credits 额度、同一个 `copilot.tencent.com` 网关，只有 `platform` 参数与会话头名不同。两边的 token 可互换使用。

2. **CodeBuddy 国际站是完全独立的账号与网关。**
   国际 token 打 `copilot.tencent.com` → `401`；CN token 打 `www.codebuddy.ai` → 同样 `401`。国际站有自己的 250 credits 试用额度。

3. **上游只支持流式。**
   `stream=false` 一律返回 `400 {"code":11101,"msg":"Non-stream chat request is currently not supported"}`。
   → 本服务**始终以 `stream=true` 请求上游**；客户端要非流式时，由本层把 SSE 聚合成标准 `chat.completion` 响应体。

4. **国际站强制首条 system 消息。**
   `messages[0].role` 必须是 `system`，否则一律 `400 Parse message failed: 11101:invalid request`。
   实测矩阵：仅 user ❌ / 仅 system ✅ / system+user ✅ / user+system（顺序颠倒）❌ / 空 system + user ✅ / 多轮但无 system ❌ / system+多轮 ✅。
   → `providers.normalize_body()` 会在命中该平台时自动插入一条 system 消息（内容取设置里的「兜底 system 提示词」）。CN 网关无此限制，补齐也无副作用。

5. **刷新令牌走请求头，不是 body。**
   `POST {api_base}/v2/plugin/auth/token/refresh`，头里带 `Authorization: Bearer {access}` + `X-Refresh-Token: {refresh}`，body 为 `{}`。三平台一致。

6. **上游没有 `responses` 接口，但本代理提供。** `/v1/responses`、`/v2/responses` 上游均 404。
   → 本服务在反代层**把 OpenAI Responses API 转译为 `chat/completions`**（`responses.py`）：
   入参 `input`/`instructions`/`tools`/`tool_choice`/`text.format` 翻译为 messages/tools；
   上游 SSE 流实时转写成 Responses 事件（或聚合成 `response` 对象返回非流式）。
   详见第 5 节。

7. **程序不内置任何模型目录，模型列表 100% 来自运行时真实探测。** 上游没有模型枚举接口（`/v3/config` 仅返回 `productFeatures`、`/v1/models` 直接 404、其它疑似枚举端点均实测 404），所以模型发现只能通过对候选 ID 发起最小请求来验证。探测逻辑集中在 `discovery.py`：
   - `PROBE_SEED` 只是一份「要尝试探测的候选 ID 清单」（文本/图像/音频都涵盖），**绝不是对外目录**；探测不到的候选会被自动丢弃，探测到的才进入注册表；
   - 每次「刷新模型目录」或首次部署启动，都会跨三平台对每个候选做最小请求，把真正可用的模型写入 **数据库注册表**（`db.get_model_registry()` / `discovered_models` 设置），并记录其 `type`（chat / image / audio）与可用 `platforms`；
   - 客户端请求一个注册表外的模型时，请求链路会触发 `live_probe_model` 实时探测回退，命中即写入注册表；探测不到则记入负面缓存并在限定 TTL 内直接返回 503，避免反复盲打上游；
   - 对外 `/v1/models`、面板「模型」页、以及请求调度（chat / 图像端点分流）**全部读这张注册表**，代码里不写死任何模型名。
   已探测固化的真实模型示例（仅供理解，不作为代码内常量）：
   - **文本**：`hy3`、`glm-5.2/5.1/5.0`、`glm-5v-turbo`、`minimax-m3`、`kimi-k3/k-2.7/k-2.6/k-2.5`、`auto`、`deepseek-v4-flash`、`deepseek-v3.2`
   - **图像**：`hunyuan-image-v3.0-art`、`hunyuan-image-v3.0`（走独立的 `/v2/images/generations` 端点）
   - **音频**：`hunyuan-tts` 等（类型标注 audio，具体可用 ID 以运行时探测为准）
   - `kimi-k2.7-code` 三网关均返回 `11102`（上游未注册），不进入目录、请求时直接 503 拦截。
   展示目录不等于每个平台都支持全部模型，实际请求由注册表里的 `platforms` 决定（见下）。

8. **请求按模型类型与注册表平台分流，不盲选账号。** 未绑定平台的 Key 使用数据库注册表里该模型的 `platforms`（由 `proxy.model_route()` 读取，必要时触发 `live_probe_model` 实时探测）：
   - 文本模型 `hy3 / glm-* / minimax-m3 / kimi-k3 / kimi-k2.5 / kimi-k2.6` 可走 **三平台**，优先国内 WorkBuddy / CodeBuddy CN，国际 CodeBuddy 作故障/额度兜底；
   - `auto / deepseek-v4-flash / deepseek-v3.2 / kimi-k2.7` 仅国内两网关（国际站返回 `11102`）；
   - 图像模型 `hunyuan-image-*` **只走国内网关的 `/v2/images/generations`**（国际站 `11102`；chat 网关返回 `11103 aiart 不支持`）；
   - 音频模型按 `audio` 类型标注，走 chat 网关（端点能力由上游决定，探测确认即收录）。
   Key 显式绑定平台时绑定优先，不会越权切到其它平台。仅当上游返回 `11102 / 11103 / service info not found / model [...] not found` 这类明确的「模型未注册」错误时才切换候选平台；普通参数 400 原样返回，避免掩盖客户端错误。模型 ID 在注册表里无可用平台（且实时探测也失败）时，服务返回清晰 503，绝不在所有账号中随机碰运气。

9. **图像生成走独立端点 `/v1/images/generations`。** 图像模型（混元图像系列）与对话模型不共用 chat 网关，请求体为 OpenAI 兼容格式 `{"model","prompt","n","size",...}`，上游返回 `data.data[].url` 与 `usage.credit`。任务队列上限（`13212 JobNumExceed`）按可重试限流处理。

10. **Token 与积分统计只认上游真实 `usage`。** 非流式请求从聚合后的最终 SSE 帧读取 `prompt_tokens` / `completion_tokens` / `total_tokens`；流式 chat 使用旁路解析器，在不改动透传字节的前提下捕获最终帧；Responses 流也会把原始 usage 回传给日志层。积分直接记录上游 `usage.credit`，包括有效的 `credit=0`，不再使用 `tokens × 固定单价` 人工换算。老版本产生的估算积分因无法无损恢复，保留日志但标记为非真实来源，不计入面板的真实积分合计与扣费明细；上游未返回 usage 时 token 记为 0（未知），不会按字符数伪造。

11. **上游偶发 SSL 抖动。** `copilot.tencent.com` 会间歇性 `SSLEOFError`。已在两处加重试：同账号网络层重试（`proxy.NET_RETRY = 2`）+ 登录/额度类幂等请求的 `urllib3.Retry`。

12. **上游 SSE 不带 charset。** requests 会退化成 ISO-8859-1 解码导致中文乱码。已强制 UTF-8 解码，流式透传则直接转发原始字节。

---

## 3. 功能

**号池**
- 三种方式加号：网页 SSO 登录、手填 access/refresh token、粘贴 Cockpit Tools 导出的 JSON（支持数组，按 uid 去重）
- 令牌自动续期：按 `expires_at` 提前 `refresh_ahead` 秒刷新，带账号级锁避免并发重复刷新；后台线程每 5 分钟巡检
- 调度策略：先按模型路由到国内/国际候选平台，再在平台内执行 `least_used`（默认）/ `round_robin` / `random`
- 失败自动换号重试；401 触发强制刷新后再试；明确的模型未注册 400 才切换候选平台
- 额度展示：总量 / 已用 / 剩余 / 套餐数，支持单号刷新与一键全量刷新
- 单号启停、删除、权重、备注
- 批量签到按日期判断：`checkin_today` 只在 `checkin_last_date` 或最近状态刷新日期为当天时有效，跨日自动视为未签；支持按当前渠道批量签到，前端显示成功/跳过/失败明细
- TraeWork 每日签到：`X-Device-Id` 优先用凭证自带的 device_id（扫码/回调导入时随登录
  注册的原生标识），手填 token 等无 device_id 场景回退 uid。实测（2026-08-31）两
  者签到均 code 0；9074 文案「参与用户太多」的真实含义是设备头不合法，与容量无关
- 渠道×模型开关：模型页每个渠道卡片内按模型开/关（如关闭 WorkBuddy 的 glm-5.3）；
  开关全局生效——真实调用、Key 显式绑定、对外 `/v1/models` 清单一并拦截；
  三渠道全关的模型从清单隐藏；测活与模型探测不受开关影响

**调用鉴权**
- 自定义 API Key，支持 `Authorization: Bearer`、`x-api-key`、`?key=` 三种传法
- Key 可绑定平台 —— 该 Key 的请求只命中对应平台的账号；模型与绑定冲突时返回清晰错误，不会越权切平台
- Key 可单独启停、统计调用次数；无合法 Key 一律 401

**其它**
- 调用日志：时间 / 平台 / 账号 / 模型 / 流式 / 状态码 / 耗时 / 输入字符数 / 错误
- 面板内一键连通性测试（走完整反代链路）
- 管理员密码修改（sha256 加盐存储）

---

## 4. 文件结构

```
hy3api/
├── app.py               Flask 应用：OpenAI 接口 + 面板 API + 后台维护线程
├── providers.py         平台适配层：SSO、刷新、额度、请求头、body 规范化
├── proxy.py             反代引擎：令牌保活、号池调度、流式转发、非流式聚合
├── responses.py         Responses API 翻译层：input/tools → chat，SSE 实时转写
├── db.py                SQLite 数据层：settings/accounts/api_keys/logs/pending_login
├── frontend/             React + Vite + shadcn 风格控制面板源码
│   ├── src/               页面与 UI 组件
│   ├── package.json       前端依赖与构建脚本
│   └── dist/              npm run build 生成的生产静态文件
├── run_bg.py              后台启停辅助（Windows）
├── e2e_test.py            端到端实测脚本
├── e2e_out.txt            最近一次实测原始输出（27/27 通过）
├── test_responses.py      Responses 翻译层单元测试（38/38 通过）
├── e2e_responses.py       /v1/responses 端到端实测
├── probe_cb2.py / probe_cb3.py 国际站接口差异探测脚本
├── Dockerfile             默认运行镜像（需要先构建 frontend/dist）
├── Dockerfile.fullbuild   多阶段镜像（容器内自动构建前端）
├── docker-compose.yml     Compose 部署配置
├── .dockerignore          排除 node_modules、数据库和开发产物
└── hy3api.db              SQLite 数据库（自动创建，已被 .gitignore / .dockerignore 排除）
```

数据库路径可用环境变量 `HY3API_DB` 覆盖；持久化目录用 `HY3API_DATA_DIR`（容器内指向 `/data` 卷）。会话密钥用 `HY3API_SECRET`（不设则首次启动自动生成并持久化到库里，重启不掉线）。详见第 8、9 节。

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
（把 `base_url` 指到 `http://<host>:8787/v1`、API Key 用面板里的 Key 即可）。

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
curl http://127.0.0.1:8787/v1/responses \
  -H "Authorization: Bearer Agent2API" \
  -H "Content-Type: application/json" \
  -d '{"model":"hy3","input":"用一句中文介绍你自己","stream":false}'
```

> 说明：是否真正触发函数调用取决于上游模型本身（如 `hy3`/混元当前不会主动调用工具），
> 但 `input` → `messages` 与 `tool_calls` → `function_call` 的双向翻译均已实现并通过单测。

---

## 6. 实测结果（2026-08-05）

`python e2e_test.py` —— **27 / 27 全部通过**，使用三个真实账号：

| 组 | 内容 | 结果 |
|---|---|---|
| T1 | 健康检查、面板首页 | PASS |
| T2 | 未登录 401、错误密码 401、`admin` 登录、登录后放行 | PASS |
| T3 | 三平台账号导入 + 额度拉取（2400 / 2400 / 250 credits） | PASS |
| T4 | 三账号刷新令牌 + 额度 | PASS |
| T5 | 无 Key 401、错误 Key 401、默认 Key 放行、自定义 Key 创建 | PASS |
| T6 | 非流式聚合（返回标准 `chat.completion`，中文正常） | PASS |
| T7 | 流式 SSE（19 帧增量，内容完整） | PASS |
| T8 | Key 绑定平台后只命中该平台账号，日志归属正确 | PASS |
| T9 | 三平台逐一真实对话：workbuddy / codebuddy_cn / codebuddy 全部返回 | PASS |
| T10 | 调用统计累计、设置读写 | PASS |

---

## 7. 安全提醒

- 默认密码 `admin` 与默认 Key `hy3api` 仅为开箱即用，**首次登录后立刻修改**
- 面板与 API 无 TLS，公网部署请放在 Nginx/Caddy 反代之后并启用 HTTPS
- 数据库明文存储 access/refresh token，注意文件权限
- 号池账号额度有限，建议给不同调用方分配独立 Key 以便追踪与吊销

---

## 8. 环境变量

所有配置项均可通过环境变量注入，容器化与裸跑通用。优先级：**命令行参数 `--port` / `--host` > 环境变量 > 内置默认值**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `HY3API_HOST` | `0.0.0.0` | 监听地址 |
| `HY3API_PORT` | `8787` | 监听端口 |
| `HY3API_THREADS` | `16` | waitress 工作线程数 |
| `HY3API_DB` | 代码目录 `hy3api.db` | 直接指定 SQLite 文件路径（最高优先级） |
| `HY3API_DATA_DIR` | 空 | 数据库目录，文件为 `<dir>/hy3api.db`（容器持久化用） |
| `HY3API_SECRET` | 自动生成并持久化 | 会话签名密钥；不设则首次启动生成并存入库，重启不掉线 |
| `HY3API_ADMIN_PASSWORD` | `admin` | 首次初始化管理员密码；库已存在时不生效 |
| `HY3API_DEFAULT_KEY` | `hy3api` | 首次初始化默认调用 Key；库已存在时不生效 |

> `HY3API_ADMIN_PASSWORD` 与 `HY3API_DEFAULT_KEY` **只在数据库尚不存在（全新 volume）时用于初始化**。一旦已在面板里改过密码 / Key，环境变量不再覆盖，避免把正在运行的实例凭据冲掉。

---

## 9. Docker 部署

镜像基于 `python:3.12-slim`，以非 root 用户（`hy3api`）运行，SQLite 数据通过卷挂载持久化，内置纯标准库的健康检查（`HEALTHCHECK` 探活 `/health`，无需额外依赖）。

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

- 账号贡献页：http://\<host\>:8787/
- 管理后台登录：http://\<host\>:8787/login
- API Base URL：http://\<host\>:8787/v1
- 健康检查：http://\<host\>:8787/health

### 9.3 自定义凭据与端口

编辑 `docker-compose.yml` 的 `environment`：

```yaml
environment:
  HY3API_ADMIN_PASSWORD: "改成你自己的强密码"
  HY3API_DEFAULT_KEY: "sk-your-own-key"
  HY3API_PORT: "8787"
  HY3API_THREADS: "16"
  # HY3API_SECRET: "固定随机长字符串（可选，重启更稳）"
```

> 凭据只在**首次启动（volume 为空）**时生效。改完 `docker-compose.yml` 后需重建容器：`docker compose down && docker compose up -d`。

### 9.4 数据持久化

`docker-compose.yml` 已声明命名卷 `hy3api-data` 挂载到容器 `/data`（即 `HY3API_DATA_DIR=/data`）。号池账号、API Key、调用日志、设置、会话密钥全部落在此卷，**容器重建 / 升级不丢数据**。

```bash
docker volume inspect hy3api_hy3api-data     # 查看数据卷在宿主机的真实路径
```

### 9.5 自行构建镜像

```bash
# 默认：打包现成的 frontend/dist
docker build -t hy3api:latest .

# 备选：在镜像的 Node 阶段自动构建前端
# docker build -f Dockerfile.fullbuild -t hy3api:latest .

docker run -d --name hy3api -p 8787:8787 -v hy3api-data:/data \
  -e HY3API_DATA_DIR=/data \
  -e HY3API_ADMIN_PASSWORD=你的密码 \
  -e HY3API_DEFAULT_KEY=sk-your-own-key \
  hy3api:latest
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
curl -fsS http://127.0.0.1:8787/health
```

进入面板用 `HY3API_ADMIN_PASSWORD` 登录；首次加号请在面板内走 SSO 或粘贴 token / Cockpit JSON（真实 token 不会进入镜像，`.dockerignore` 已排除数据库与测试脚本）。

### 9.8 安全

- 容器本身无 TLS，公网暴露务必放在 Nginx / Caddy 之后并启用 HTTPS。
- 数据库内含明文 access / refresh token，卷权限需控制好，不要分享数据卷。
- 推荐给不同调用方分配独立 Key 以便追踪与吊销。

### 9.9 容器化适配验证

本机未安装 Docker，已用 `docker_env_test.py` 完整模拟 `Dockerfile` / `docker-compose.yml` 注入的环境变量做等价验证：**13 / 13 通过**（原始输出见 `docker_env_out.txt`）：

- **全新 volume 首次启动**：端口、`HY3API_DATA_DIR`、管理员密码、默认 Key 全部来自环境变量；默认 `admin` / `hy3api` 失效；内部密钥不通过 API 泄露；HEALTHCHECK 探活代码可用。
- **模拟 `docker restart`（volume 保留）**：登录态保持（session 密钥已持久化）、密码仍为持久化值。
- **面板内改密后再次重启**：环境变量不覆盖已有密码（仅首次初始化生效）。
- **启动横幅**：打印数据库真实路径。

```bash
python docker_env_test.py        # 复跑容器化适配验证
```
