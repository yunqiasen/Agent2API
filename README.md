# Agent2API

WorkBuddy / TraeWork / CodeBuddy 国际 / WorkBuddy 国际 四渠道 **多模型反向代理 + 号池控制面板**。
对外暴露标准 OpenAI 兼容接口，对内做 SSO 登录、令牌自动续期、号池调度、额度监控与调用鉴权。

> 接口契约以仓库内 `tests/` 的回归测试和当前容器实测为准；上游模型、额度与签到结果会随账号和服务端状态变化。

## 代码来源、协议参考与致谢

本项目基于 **DearLicy/hy3api** 二次开发。下面将直接代码上游、平台适配移植来源和协议参考分开列出，便于追溯实现依据。

| 关系 | 项目 | Agent2API 参考内容 | 对应代码 |
|---|---|---|---|
| **直接代码上游** | [DearLicy/hy3api](https://github.com/DearLicy/hy3api) | 最初的 Python 反代、号池、管理后台、模型发现与 Docker 部署基线 | `app.py`、`db.py`、`proxy.py`、`discovery.py`、`frontend/` |
| **Trae 适配移植来源** | [itamzxm/ai-multi-platform-proxy](https://github.com/itamzxm/ai-multi-platform-proxy) | TraeWork / Trae SOLO 的登录、Token 交换、请求头、请求体、SSE 解析、额度与签到接口适配 | [`trae.py`](trae.py) |
| **WorkBuddy 国际协议参考** | [ardeyouxipianyi/workbuddy2api-intl](https://github.com/ardeyouxipianyi/workbuddy2api-intl/tree/7c93b46ad045798c5da5953f0a5bc4e9e8a7b8f9) | 国际客户端登录、刷新头/响应、JWT 到期时间、模型 profile 及工具请求兼容；复用本项目 SQLite 号池和公共反代，不引入其独立网关或静态对外目录 | `providers.py`、`credentials.py`、`proxy.py`；[MIT 声明](THIRD_PARTY_NOTICES.md) |
| **国际维护与限流协议参考** | [zidanefaqih/codebuddy-intl-cpa](https://github.com/zidanefaqih/codebuddy-intl-cpa/tree/03001d4599cebcd7a348d9d793f5174a558ea2cb) | 国际凭证格式、每日活动/奖励分阶段与限流冷却思路；保留两个独立客户端及本项目调度 | `daily_rewards.py`、`providers.py`、`proxy.py`；[MIT 声明](THIRD_PARTY_NOTICES.md) |
| **间接协议参考，非直接代码上游** | [jlcodes99/cockpit-tools](https://github.com/jlcodes99/cockpit-tools) | hy3api 原实现注明的 WorkBuddy / CodeBuddy OAuth、平台常量、账号信息、Token 刷新及额度接口参考 | [`providers.py`](providers.py) |

**cockpit-tools 的定位**：Agent2API 不是从它 fork 的，它也不是本服务的运行依赖。
[hy3api 最初提交的 providers.py](https://github.com/DearLicy/hy3api/blob/2d9784a2cd6ae7fb5cc587ecd2acdcaedc4bde2e/providers.py#L10)
已经注明「流程与常量来自 cockpit-tools 源码」，这项来源说明由本项目继承，而非本次新增的依赖。
其仓库确实包含 [WorkBuddy OAuth](https://github.com/jlcodes99/cockpit-tools/blob/cc4a3f4a1573143f8551fa27825aec443e2df09f/crates/cockpit-core/src/modules/workbuddy_oauth.rs)、
[CodeBuddy OAuth](https://github.com/jlcodes99/cockpit-tools/blob/cc4a3f4a1573143f8551fa27825aec443e2df09f/crates/cockpit-core/src/modules/codebuddy_oauth.rs)
和 [Trae OAuth](https://github.com/jlcodes99/cockpit-tools/blob/cc4a3f4a1573143f8551fa27825aec443e2df09f/src-tauri/src/modules/trae_oauth.rs) 模块；
**本项目的 Trae 适配仍以 ai-multi-platform-proxy 为移植来源**，不因参考仓库也支持 Trae 而混同来源。

最近一次代码上游与参考项目核对：**2026-09-13**，详见 [更新核对报告与完整模型清单](docs/upstream-review-2026-09-13.md)。
第四渠道接入补齐与最新实测：**2026-09-17**，详见 [WorkBuddy 国际验收记录](docs/workbuddy-intl-parity-2026-09-17.md)（233 项 Python 回归、7 项前端轮询测试及真实双轮工具调用）。

最新官方客户端模型目录核对：**2026-10-08**，详见 [Buddy 三渠道目录修复记录](docs/buddy-model-catalog-2026-10-08.md)。当前目录：WorkBuddy 18项、WorkBuddy 国际25项、CodeBuddy 国际 CLI兼容线路21项；TraeWork维持21项。目录刷新只读取配置，不执行生成测试。

### 本项目的扩展与维护

Agent2API 在原有服务上整合、扩展并维护以下能力：

- WorkBuddy、TraeWork、CodeBuddy 国际、WorkBuddy 国际四渠道号池；
- 账号导入、Token 自动刷新、额度同步、签到和单账号测活；
- 模型动态发现、渠道×模型开关、模型调用优先级及渠道倍率展示；
- 调用密钥按渠道和模型多选分配权限，支持原 Key 编辑；
- OpenAI Chat Completions / Responses / Images 兼容接口；
- 管理后台、调用鉴权、请求日志和 Docker 持久化部署。

上述来源项目的版权、许可证和原有声明以各自仓库为准；分发包含参考或移植内容的版本时，请保留对应项目的来源说明和许可证要求。

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

## 2. 四渠道对照

| 平台 | key | 登录域 | platform 参数 | 模型网关 | 会话头 |
|---|---|---|---|---|---|
| WorkBuddy | `workbuddy` | `www.codebuddy.cn` | `workbuddy` | `www.workbuddy.cn` | `X-WorkBuddy-Session-Id` |
| TraeWork | `trae` | `www.trae.cn` | `trae` | `trae-api-cn.mchost.guru` | SOLO 专用头 |
| CodeBuddy 国际 | `codebuddy` | `www.codebuddy.ai` | `ide` | `www.codebuddy.ai` | `X-CodeBuddy-Session-Id` |
| WorkBuddy 国际 | `workbuddy_intl` | `www.workbuddy.ai` | `workbuddy-ai` | `www.workbuddy.ai` | `X-WorkBuddy-Session-Id` |

### 关键实测结论

1. **WorkBuddy 与 CodeBuddy CN 使用同一套账号体系，本项目只保留 WorkBuddy 渠道。**
   两者共用国内账号和额度；当前 WorkBuddy 客户端模型网关为 `www.workbuddy.cn`。旧注册表里的 `codebuddy_cn` 引用会在启动和模型刷新时迁入 `workbuddy`，避免重复号池，同时不丢历史模型。

2. **TraeWork 是独立渠道。**
   Trae 使用 SOLO 专用请求头、请求体和 `trae-api-cn.mchost.guru` 网关，账号与国内 WorkBuddy 额度分开。

3. **两个国际客户端共享国际账号体系，与国内 WorkBuddy、TraeWork 分开。**
   CodeBuddy 国际与 WorkBuddy 国际保留独立渠道、模型目录和调用统计。相同 UID/租户的国际额度汇总去重；同一 refresh 凭证跨渠道导入后统一串行轮换并同步保存，不同登录会话的 token 不相互覆盖。不自动复制旧账号到新渠道。

4. **上游只支持流式。**
   `stream=false` 一律返回 `400 {"code":11101,"msg":"Non-stream chat request is currently not supported"}`。
   → 本服务**始终以 `stream=true` 请求上游**；客户端要非流式时，由本层把 SSE 聚合成标准 `chat.completion` 响应体。

5. **国际站强制首条 system 消息。**
   `messages[0].role` 必须是 `system`，否则一律 `400 Parse message failed: 11101:invalid request`。
   实测矩阵：仅 user ❌ / 仅 system ✅ / system+user ✅ / user+system（顺序颠倒）❌ / 空 system + user ✅ / 多轮但无 system ❌ / system+多轮 ✅。
   → `providers.normalize_body()` 会在命中该平台时自动插入一条 system 消息（内容取设置里的「兜底 system 提示词」）。CN 网关无此限制，补齐也无副作用。

6. **刷新令牌走请求头，不是 body。**
   腾讯三个渠道使用 `POST {api_base}/v2/plugin/auth/token/refresh`，携带 `X-Refresh-Token`、`X-Auth-Refresh-Source: plugin`，不携带过期 Bearer；刷新 POST 禁止 HTTP 层自动重放。Trae 使用自己的 `ExchangeToken`。四渠道统一由 `proxy.ensure_token` 提前续期并持久化轮换结果。

7. **上游没有 `responses` 接口，但本代理提供。** `/v1/responses`、`/v2/responses` 上游均 404。
   → 本服务在反代层**把 OpenAI Responses API 转译为 `chat/completions`**（`responses.py`）：
   入参 `input`/`instructions`/`tools`/`tool_choice`/`text.format` 翻译为 messages/tools；
   上游 SSE 流实时转写成 Responses 事件（或聚合成 `response` 对象返回非流式）。
   详见第 5 节。

8. **程序不内置固定对外目录，刷新以各客户端当前官方配置为准。** WorkBuddy、WorkBuddy 国际和 CodeBuddy 国际 CLI兼容线路读取各自网关的 `/v3/config`；云配置覆盖旧基础目录，不再将旧目录直接并回。只取主 `cli` 白名单及明确的免费/付费线路，忽略图像、视频和内部模型元数据。TraeWork读取 `get_detail_param` 的 `solo_work_remote` 场景。
   - 账号导入/网页登录后自动增量同步该账号目录；失败保留旧目录，可在卡片点“刷新模型”重试。
   - 「刷新模型目录」合并各渠道所有启用账号的目录；该渠道全部读取成功才清理旧聊天模型，部分失败仅增量更新并明确报错；无可用账号时提示保留原目录。既有图像/音频登记不受聊天目录清理影响。
   - 刷新按钮与首次启动的目录初始化**只读取配置，不发起聊天、生图或音频生成**。正常同步的结果写入数据库注册表（`discovered_models`）。
   - 账号卡片“测活”仍是用户主动发起的真实模型调用，会按上游规则计费。外部请求未知模型时仍有实时探测回退；它属于该次外部请求链路，不是定时测速任务。
   - `/v1/models`、模型页和请求路由读取同一张注册表，渠道开关及优先级继续生效。
   - CodeBuddy 国际保留原有 CLI命名模型兼容方式，与官网IDE的主代理档位目录分开；详情见 [当前客户端目录说明](docs/buddy-model-catalog-2026-10-08.md)。
   目录可见只证明该客户端配置返回此ID，不代表所有账号已测活，也不代表全部免费。

9. **请求按模型类型、渠道开关与优先级分流，不盲选账号。** 未绑定渠道的 Key 在该模型已登记且开启的渠道中，按“逐模型优先级 → 默认优先级”选择，再进入对应号池。第四渠道沿用同一套机制；旧三渠道相对顺序保持，新渠道默认追加末尾。
   - 模型支持范围来自当前目录/实测，不以固定的“三平台同模型”矩阵推断；
   - 图像按 `image` 路由独立端点，国际凭证不发往国内图像网关；
   - 音频仍按现有 `audio` 分流，端点能力以实测为准。
   Key 显式绑定平台时绑定优先，不会越权切到其它平台。上游返回 `11102 / 11103 / service info not found / model [...] not found` 这类明确的「模型未注册」错误时，会先继续尝试该渠道的其它账号（账号套餐可能不同），该渠道都失败后再切换候选渠道；普通参数 400 原样返回，避免掩盖客户端错误。模型 ID 在注册表里无可用平台（且实时探测也失败）时，服务返回清晰 503，绝不在所有账号中随机碰运气。

10. **图像生成走独立端点 `/v1/images/generations`。** 图像模型（混元图像系列）与对话模型不共用 chat 网关，请求体为 OpenAI 兼容格式 `{"model","prompt","n","size",...}`，上游返回 `data.data[].url` 与 `usage.credit`。任务队列上限（`13212 JobNumExceed`）按可重试限流处理。

11. **Token 与积分统计只认上游真实 `usage`。** 非流式请求从聚合后的最终 SSE 帧读取 `prompt_tokens` / `completion_tokens` / `total_tokens`；流式 chat 使用旁路解析器，在不改动透传字节的前提下捕获最终帧；Responses 流也会把原始 usage 回传给日志层。积分直接记录上游 `usage.credit`，包括有效的 `credit=0`，不再使用 `tokens × 固定单价` 人工换算。老版本产生的估算积分因无法无损恢复，保留日志但标记为非真实来源，不计入面板的真实积分合计与扣费明细；上游未返回 usage 时 token 记为 0（未知），不会按字符数伪造。

12. **上游偶发 SSL 抖动。** 网络与额度查询保留有限重试；OAuth token 领取、刷新轮换及交互式单号目录同步不做 HTTP 层透明重放。登录轮询串行执行，失败/过期会停止并提示重试。

13. **上游 SSE 不带 charset。** requests 会退化成 ISO-8859-1 解码导致中文乱码。已强制 UTF-8 解码，流式透传则直接转发原始字节。

---

## 3. 功能

**号池**
- 接入后自动同步模型；卡片可刷新目录、选择模型测活。目录失败、额度失败和凭证续期失败独立反馈，成功重新授权会清除旧续期失败退避。
- 三种方式加号：网页 SSO 登录、手填 access/refresh token、粘贴 Cockpit Tools 导出的 JSON（支持数组，按平台+uid 原位更新；旧渠道迁移会自动合并重复账号）
- 令牌自动续期：后台每 5 分钟检查，默认提前 900 秒；兼容秒/毫秒、上游期限和 JWT `exp`，历史缺失时间自动补齐。未知有效期且有 refresh 的账号每小时尝试续期，失败指数退避。新旧令牌原子保存，账号卡片单独展示最后续期成功时间/失败原因，额度成功不会覆盖续期失败。详情见 [凭证保活说明](docs/credential-refresh-2026-09-13.md)。
- 调度策略：先按模型路由到国内/国际候选平台，再在平台内执行 `least_used`（默认）/ `round_robin` / `random`
- 失败自动换号重试；401 触发强制刷新后再试；HTTP 200 包装的 SSE 业务错误也会在返回客户端前识别并换号；默认遍历同渠道的全部合格账号，再按该模型的渠道优先级切换；全池失败直接返回真实错误，不等待冷却
- 额度展示严格使用上游实时口径：TraeWork 读取 `usage_summary.total_amount / consumed_amount` 并保留真实套餐列表；刷新失败保留上一份成功数据并报错，不用 0 覆盖；真实同步到 0 时明确显示 0，不再写成“未同步”
- 账号卡片使用事实标签：`已接入`、`未过期`、`本站调用`；TraeWork 未返回连签天数时隐藏该字段，不伪造“连续 0 天”
- 单号启停、删除、权重、备注
- 账号卡片开关仅控制模型调用：关闭后不再参与外部 API 调度，但单号签到、全部签到、自动签到/补签及凭证自动刷新继续执行；删除账号才会移出维护范围。
- 批量签到以上游为准：单号、批量、自动签到都逐号查询上游状态；上游未签才领取，已签才跳过。本地 `checkin_today` 只做页面缓存，不参与是否请求的判断；状态查询失败或领取后的状态回读失败会直接记为失败，不把本地卡片伪造成“已签到”；支持按当前渠道签到，前端将“新签到 / 已签跳过 / 失败”分开显示，容器日志记录每个账号的 status / claim / status_after_claim 阶段
- TraeWork 模型目录及聊天请求统一使用当前 Work 场景 `solo_work_remote`，动态获取模型，不沿用旧 `solo_work_lite` 目录；2026-10-07 六个账号实测新增 `step-5-preview`、`deepseek-v4.1-flash`、`glm-5.3`。目录可见不等于模型推理已测，详见 [目录修复记录](docs/trae-remote-catalog-2026-10-07.md)。
- TraeWork 每日签到：当前统一使用凭证中的 `uid` 作为 `X-Device-Id`，因为实测它同时适配扫码登录与 Token 导入账号；模型/额度请求仍会携带凭证里的 machine/device 字段。实测（2026-08-31）全号池签到链路均能得到上游已签到结果；9074 文案「参与用户太多」不能直接当作容量结论，面板会保留原始错误。
- 渠道×模型开关：模型页每个渠道卡片内按模型开/关（如关闭 WorkBuddy 的 glm-5.3）；
  开关全局生效——真实调用、Key 显式绑定、对外 `/v1/models` 清单一并拦截；
  所有渠道全关的模型从清单隐藏；测活与模型探测不受开关影响

**模型倍率**
- 「模型」页各渠道条目显示 **免费 / ×倍率 / 未公布**；点击徽标查看官方原值、来源、同步时间和各账号样本。
- 精确零倍率才标免费；缺失、异常或同渠道账号返回冲突时显示未公布。刷新失败保留旧数据并标记，成功响应缺少价格则更新为未公布。
- 同一 ID 在不同渠道价格独立；顶部聚合列表没有统一价格，公共 `/v1/models` 不增加价格字段。
- 复用官方目录响应提取倍率，查看/刷新不调用生成接口；Trae 未核验的价格字段显示未公布。倍率只是提示，不改渠道优先级或实际积分账本。

**调用鉴权**
- 自定义 API Key，支持 `Authorization: Bearer`、`x-api-key`、`?key=` 三种传法
- Key 可绑定单渠道或全池，并选择「不限模型 / 指定模型」；指定模式多选且至少 1 项，新模型不自动加入白名单。旧 Key 升级后保持不限模型。
- 在「API Key → 新建调用密钥」选渠道和模型；已有 Key 在卡片点击「编辑」。编辑保留原密钥字符串、启停状态及调用统计。
- 模型页关闭的模型，在 Key 多选区灰显且鼠标/键盘均不可选；全池按模型 ID 去重，只要任一支持渠道开启即可选择，单渠道绑定不借用其他渠道的开关。
- 历史选择被全局关闭后保留为停用标签，实际调用同步停用；可以单独移除标签，也可以在模型页重新开启后恢复原 ID。切换渠道产生失效选择时，先移除或恢复再保存。
- 支持按名称/ID搜索；目录读取和保存失败保留表单内容，保存时后端重新校验。渠道与模型权限原子更新，失败保持原权限。重复自定义 Key 返回409。
- `/v1/models` 只列出 Key 名单、绑定渠道及全局开关的交集；仍为原 OpenAI 字段结构，不附带倍率。
- 聊天、Responses、图像及别名入口共同执行模型权限校验。指定 Key 的越界请求返回403 `model_not_allowed`，在路由与探测前拦截；默认模型及系统内部替换后的模型也要在许可范围内。
- 指定模式的空名单、损坏配置保持收紧并在管理页提示；明确切到不限模式会清空白名单。不限模型 Key 的既有外部未知模型探测行为保持，指定 Key 不探测名单外/已移出目录的模型。
- Key 可单独启停、统计调用次数；无合法 Key 一律 401

管理接口 `POST /api/keys` 和 `PATCH /api/keys/{id}`（管理员会话）示例参数：

```json
{"platform":"workbuddy","model_scope":"selected","allowed_models":["glm-5.3","deepseek-v4.1-flash"]}
```

以上 ID 以模型页当前已收录且开启的条目为准；新建可另填 `name` 和 `api_key`。旧创建请求省略权限字段时为不限，PATCH 省略的字段保持原值；`{"model_scope":"all"}` 明确清空名单并切回不限。仅启停 Key 的 PATCH 不清理历史停用选择。

**其它**
- 调用日志：时间 / 平台 / 账号 / 模型 / 流式 / 状态码 / 耗时 / 输入字符数 / 错误
- 面板内一键连通性测试（走完整反代链路）；HTTP 200 业务错误和 SSE error 事件不会再被标成成功
- 管理员密码修改（sha256 加盐存储）

---

## 4. 文件结构

```
Agent2API/
├── app.py               Flask 应用：OpenAI 接口 + 面板 API + 后台维护线程
├── credentials.py       到期时间解析与凭证错误脱敏
├── providers.py         平台适配层：SSO、刷新、额度、请求头、body 规范化
├── proxy.py             反代引擎：令牌保活、号池调度、流式转发、非流式聚合
├── responses.py         Responses API 翻译层：input/tools → chat，SSE 实时转写
├── model_pricing.py     官方倍率快照、价格状态与来源信息
├── key_permissions.py   Key模型范围校验、可见目录过滤与调用权限
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
提供下列 Responses API 兼容能力（不是完整的 OpenAI 托管工具/会话存储服务）。
把 `base_url` 指到 `http://<host>:8530/v1`，API Key 使用面板创建的 Key。

**支持的入参翻译**

- `input`：字符串 → 单条 user 消息；数组 → 逐条翻译
  - `message` 项（`role` + `content`）→ 普通 message
  - `content` 支持 `input_text` / `input_image`（`image_url`）parts
  - `function_call` 项（历史工具调用）→ assistant 带 `tool_calls`
  - `function_call_output` 项 → `tool` 角色消息
  - `system` 角色消息 → system message
- `instructions` → 置顶的 system message
- `tools` / `tool_choice` → chat 的 `tools` / `tool_choice`
  - 指定函数对象先保留标准 Chat 语义，腾讯渠道转换为准确的函数名字串，多个工具时也不再丢失选择；`none` 移除新工具定义。
  - 保留工具 `strict`、调用 ID 与参数分片；历史 `function_call`/`function_call_output` 原样关联，不补造缺失的工具结果。
- `text.format` → `response_format`（如 `{"type":"json_object"}`）
- `max_output_tokens` → `max_tokens`；`temperature` / `top_p` 等其余字段透传
- `previous_response_id` / `include` / `store` / `truncation` 等：本层不持久化对话，忽略

**返回格式**

- 非流式：`stream:false`（或省略）→ 返回标准 `response` 对象
  （`output` 含 `message`、`function_call` 与独立 `reasoning` 项，附 `usage`）
- 流式：`stream:true` → 返回 Responses 格式的 SSE 事件流：
  `response.created` → `response.in_progress` → `response.output_item.added`
  → `response.content_part.added` → `response.output_text.delta`（逐片）
  → `response.output_text.done` → `response.content_part.done`
  → `response.output_item.done` → `response.completed`
  （工具调用场景额外产生 `response.output_item.added`(function_call) /
  `response.function_call_arguments.delta` / `.done`）。事件 JSON 含 `type`/`sequence_number`，创建/完成事件通过 `response` 字段携带对象；截断返回 `response.incomplete` 与原因，推理不混入正文。

调用示例（非流式）：

```bash
curl http://127.0.0.1:8530/v1/responses \
  -H "Authorization: Bearer Agent2API" \
  -H "Content-Type: application/json" \
  -d '{"model":"hy3","input":"用一句中文介绍你自己","stream":false}'
```

> 说明：是否支持工具调用以当前上游模型的实际响应为准，不按模型名称推断。
> 2026-09-17 已用 WorkBuddy 国际的 glm-5.3、deepseek-v4.1-flash 实测双轮工具调用。其他模型的工具能力仍以实际响应为准。

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


### 国际每日奖励（2026-10-14）

CodeBuddy 国际与 WorkBuddy 国际沿用卡片「签到」、当前渠道「全部签到」及现有自动签到设置。提交活动、上游活动确认、奖励到账分开记录：HTTP成功不代表到账，不固定承诺每天30分。关闭模型调用仍维护凭证与奖励。

卡片显示活动日期（Asia/Shanghai）、最后查询时间、待核验/待到账、阶段错误及奖励记录。金额按有唯一ID的实际发放记录核验，余额差额不作为收益；缺金额、日期或类型的记录保持待核验。一般平台奖励只显示为平台奖励，不强行归入今天的每日活动。历史已耗尽/过期记录保留，跨客户端共享账本只计一次。

活动提交不做无条件重放；明确拒绝后先查询再重试，传输结果不明则保留待核验。维护过程不调用聊天或生图。开发验收使用隔离账号库及可控上游，实号奖励是否到账以部署后的实际记录为准。


### 四渠道限流换号

- 聊天、Responses、已支持的图像接口及无 `/v1` 前缀别名，共用同一选号与限流机制。
- HTTP429、腾讯错误结构6004/图像队列13212、Trae明确的429限流独立分类；Trae4008/1005是额度或权益不足，不当作短时限流。正常回答中出现数字或“rate limit”不触发冷却。
- 先尝试同渠道全部合格账号，再按同模型优先级切渠道。Key绑定、模型白名单、渠道模型开关、账号调用开关始终约束范围。401成功续期后同账号最多补试一次。
- 冷却优先读取有效 `Retry-After` 或上游恢复时间；缺省60秒。在「设置 → 号池与签到 → 限流默认冷却（秒）」调整，支持1–86400整数。不是让当前请求原地等这么久。
- 冷却持久化，到期自动恢复候选资格，不调用模型测速。只在明确的模型级/共享国际服务级限制时缩小或共享范围；范围不明时只影响当前服务的该账号。
- 账号卡片显示冷却范围、错误及恢复时间；调用日志展示请求ID、逐次尝试、最终账号及真实/未知费用。维护成功不清除模型冷却。
- 已向调用方输出模型事件后，遇错按原协议报错结束，不换号拼接另一份回答；单账号测活保持原账号。

本轮实现与验收记录见 [国际维护与限流升级执行记录](docs/implementation/2026-10-14-international-upgrade/index.md)。开发验收与正式部署、实号到账分别记录。
