# 代码上游、参考项目与三渠道模型核对（2026-09-13）

核对对象：Agent2API；本地起始提交 `bad7798`。模型目录采集时间：**2026-09-13 10:09（Asia/Shanghai）**。
本次成功读取全部 16 个启用账号：WorkBuddy 7、TraeWork 6、CodeBuddy 国际 3。

本报告区分三件事：**官方目录有列出**、**抽样请求通过**、**当前对外开关开启**。
它们不是同一概念；没有把目录刷新当作所有账号、所有协议、所有模型都已测通。

机器可读快照：[model-catalog-2026-09-13.json](model-catalog-2026-09-13.json)。
快照只用于追溯；运行时模型仍以数据库注册表和管理员的渠道开关为准。

## 1. 代码上游与参考项目版本

| 来源项目 | 本次 HEAD / 日期 | 关系与结论 |
|---|---|---|
| [DearLicy/hy3api](https://github.com/DearLicy/hy3api) | `2d9784a` / 2026-08-24 | 直接代码上游，正是本仓库最初的基线；目前没有更晚的提交 |
| [itamzxm/ai-multi-platform-proxy](https://github.com/itamzxm/ai-multi-platform-proxy) | `a7183d0` / 2026-08-19 | Trae 适配移植来源，与本地参考版本一致；QClaw、Trae 正式版模型等是原有内容，不是本次新更新 |
| [jlcodes99/cockpit-tools](https://github.com/jlcodes99/cockpit-tools) | `cc4a3f4` / 2026-09-13 | 间接协议参考，非直接代码上游；最新发布 v1.3.50，有可借鉴的更新 |

这里保留 cockpit-tools 是为了追溯协议来源，不表示 Agent2API 从该仓库 fork 或依赖其运行。
[hy3api 原始 providers.py](https://github.com/DearLicy/hy3api/blob/2d9784a2cd6ae7fb5cc587ecd2acdcaedc4bde2e/providers.py#L10)
已经注明该参考关系。其源码有 WorkBuddy、CodeBuddy 和 Trae 的 OAuth 模块；本项目 Trae 的移植来源另为 ai-multi-platform-proxy。

Cockpit 对比基点为 2026-08-29 前最后一个提交 `7523ad7`（v1.3.34），仅用于分析参考实现的变更。
没有执行上游整仓覆盖，也没有改动现有三个渠道的划分。
README 已补全最初的 `DearLicy/hy3api` 来源，而不只列两个协议参考项目。

## 2. 值得借鉴的内容

| 优先级 | 参考项目变更 | Agent2API 对照与建议 |
|---|---|---|
| 高 | v1.3.35：新版 WorkBuddy / CodeBuddy OAuth、刷新、额度请求适配 | 值得单独跟进。包括 WorkBuddy API 主机切换到 `copilot.tencent.com`、登录 `version` / `loginSessionId`、刷新 `X-Auth-Refresh-Source`、个人额度 `OnlyValidPeriod`、企业额度回退及无限额度解释。本项目还保留部分旧请求形态；本次不顺带更换整条登录/计费链路 |
| 已覆盖主要要点 | v1.3.38 / PR #2194：计费请求补 User-Agent，JSON 导入保留 `expires_at` / `expiresAt` | 本项目 `_session()` 已设置 UA，导入接口也已兼容两种过期时间字段；不需要重复移植。刷新失败保留上一份成功额度也已有实现 |
| 中 | v1.3.50：Codex API Service 按账号限制并发会话，忙时换号、排队超时给出明确原因 | 可借鉴调度设计，并非已经适配本项目三渠道的实现。目前本项目的账号级锁主要保护 token 和签到，不是聊天并发限制；建议独立实现并发计数与释放测试 |
| 中 | v1.3.49–50：DeepSeek / Codex 等渠道的逐模型能力、上下文与推理参数维护 | 本次官方元数据确实返回上下文、识图、工具调用、推理档位和积分倍率，值得在模型页按渠道展示。本次保存快照，不把别的平台模型名照搬到三个渠道 |

证据：
- [v1.3.35 变更提交](https://github.com/jlcodes99/cockpit-tools/commit/d6bd17a)
- [计费 UA / 导入过期时间修复](https://github.com/jlcodes99/cockpit-tools/commit/eb92aef862bc143cb1316c887468b5eef7ad5088)
- [截至本次 HEAD 的变更日志](https://github.com/jlcodes99/cockpit-tools/blob/cc4a3f4a1573143f8551fa27825aec443e2df09f/CHANGELOG.zh-CN.md)

## 3. 本次修复的漏模型根因

旧 `providers.fetch_upstream_models()` 在控制台请求成功后直接返回，只有失败才读 `/v3/config`。
实测两个接口并非同一份目录：

- 控制台没有 `minimax-m2.7`，运行时配置里有；
- 部分账号的运行时配置还包含 `hy4-preview-f`，另一账号则仍列 `hy4-preview`；
- 只读第一个成功来源或只看一个账号，都有遗漏。

已修为：**每个账号合并两处官方 CLI 白名单，再合并同渠道所有启用账号的结果**。
任一来源失败保留另一来源，按 ID 去重；不把整个 `data.models` 元数据集合直接当成 CLI 白名单。
TraeWork 继续读取官方 SOLO `get_detail_param`，排除内部代理和 `custom_model_*` 占位项。

回归测试先复现旧代码漏收，再验证双源并集、单源异常、跨账号并集、既有开关与优先级保留。

## 4. 新增模型与实测结果

| 渠道 | 新增 ID | 验证 |
|---|---|---|
| WorkBuddy | `deepseek-v4.1-flash` | 正常回复；部署后的 `/v1/chat/completions` 也通过 |
| WorkBuddy | `minimax-m2.7` | 返回 `OK`、`finish_reason=stop`；该样本 `usage.credit=0.02` |
| WorkBuddy | `hy4-preview-f` | 两个账号均返回 `OK`，该两次 `usage.credit=0`；上游元数据标注 `x0.00 credits` |
| WorkBuddy | `kimi-k2.8-preview` | 目录已列出，但本次没有成功调用：6 个账号 `11102 / service info not found`，1 个账号 `14018 / 额度已用尽` |

`kimi-k2.8-preview` 换用当前 npm 官方 CLI 2.150.0 的 UA 并补齐客户端来源头后仍返回 11102。
这只说明当前这些账号、这条网关尚未测通该 ID，不据此推断官方所有客户端和所有账号的状态。
遵循项目已有约定，新模型默认开启；测活失败不自动禁用账号或模型，管理员仍可在模型分类中自行关闭。

`hy4-preview-f` 的免费结论限定于本次官方标注与样本，不承诺长期价格。
另外本次 CodeBuddy 国际的 `hy4-preview` 与 `hy4-preview-x` 样本分别返回 **0.01 credits**，
所以它们不应继续被当作固定免费模型；国际 `hy3` 的采样用量则为 0 credits。

## 5. 当前官方目录

| 渠道 | 官方目录 ID 数（账号并集） | 保留的已验证兼容 ID | 已有图像 ID | 最终渠道注册表 |
|---|---:|---:|---:|---:|
| WorkBuddy | 18 | 4 | 2 | 24 |
| TraeWork | 18 | 0 | 0 | 18 |
| CodeBuddy 国际 | 17 | 7 | 0 | 24 |

三渠道共享同名模型，去重后的数据库注册表为 **51** 个；按管理员开关过滤后，对外为 **17** 个。
TraeWork、CodeBuddy 国际的本次 CLI/SOLO 目录没有新增 ID。

### WorkBuddy

来源：`copilot.tencent.com/console/enterprises/personal/models` + `/v3/config` 的 `agents[name=cli].models`。

```text
auto
deepseek-v4-pro
deepseek-v4.1-flash
glm-5.1
glm-5.2
glm-5.3
glm-5.3-flash
glm-5v-turbo
hy3
hy3-x
hy4-preview
hy4-preview-f
kimi-k2.6
kimi-k2.7
kimi-k2.8-preview
kimi-k3-1
minimax-m2.7
minimax-m3
```

不在上述 CLI 白名单中、但本次仍有真实回复或推理流，因此保留的兼容 ID：

`deepseek-v3.2`、`deepseek-v4-flash`、`kimi-k2.5`、`kimi-k3`。

原有图像模型 `hunyuan-image-v3.0`、`hunyuan-image-v3.0-art` 保留，本轮没有重跑付费生图任务。

### TraeWork

来源：`trae-api-cn.mchost.guru/api/ide/v1/get_detail_param`，`function=solo_work_lite`。

```text
DeepSeek-V4-Flash
DeepSeek-V4-Flash-Official
DeepSeek-V4-Pro
DeepSeek-V4-Pro-Official
Doubao-Seed-2.0-Code
Doubao-Seed-2.1-Pro
Doubao-Seed-2.1-Turbo
Doubao-Seed-Evolving
glm-5
glm-5-turbo
glm-5.2
kimi-k2.6
kimi-k2.7-code
kimi-k3
minimax-m3
qwen-3.7-plus
qwen3.8-max
seed-code-pro-0430
```

### CodeBuddy 国际

来源：`www.codebuddy.ai/v3/config` 的 CLI 白名单；控制台目录本次仍返回 HTTP 500。

```text
balanced-model
deep-model
default-model
fast-model
gemini-3.5-flash
glm-5.2
glm-5.3
gpt-5.3-codex
gpt-5.4
gpt-5.5
gpt-5.6-luna
gpt-5.6-sol
gpt-5.6-terra
kimi-k2.6
kimi-k3
minimax-m3
primary-model
```

不在上述 CLI 白名单中、但本次仍有真实回复或推理流，因此保留的兼容 ID：

`auto`、`glm-5.1`、`glm-5v-turbo`、`hy3`、`hy4-preview`、`hy4-preview-x`、`kimi-k2.5`。

## 6. 元数据集合里的其它条目

官方 `data.models` 往往大于客户端默认白名单。机器可读快照也保存了这些公开元数据，
例如国际站还返回 `gemini-3.1-flash-lite` 以及以下图像/视频 ID：

```text
gemini-3.0-pro-image
gemini-3.1-flash-image
gemini-2.5-flash-image
hunyuan-image-v3.0
hunyuan-image-v2.0-general-edit
hunyuan-video-art
```

它们是后续协议与能力核验的候选，**不因出现在元数据里就登记成已通过的聊天模型**。
本轮未逐个验证这些额外媒体端点，也未开启新的媒体生成任务。
现有官方客户端还允许自定义模型及额外工具，这些与默认 CLI 白名单分开理解。

## 7. 部署与验证

- 121 项 Python 测试通过；本次未修改前端代码。
- 新 Docker 镜像已构建，`agent2api` 已重建并处于 healthy，端口仍是 8530。
- 管理员登录、模型管理接口、外部 `/v1/models`、`/health` 均返回 HTTP 200。
- 模型页与外部列表都读取当前注册表；实际对外 17 个 ID，与渠道开关过滤结果一致。
- 原 `model_platform_disabled` 和 `model_route_priority` 两项配置逐字比较保持一致；没有把已关闭模型重新打开。
- 保留本次验证仍响应的旧兼容 ID，未将“官方白名单不再列出”误判为服务已下架。
- 操作前已做 SQLite 在线备份，`integrity_check=ok`：
  `/data/backups/before-model-sync-20260913-100611.db`。
- 旧镜像保留为 `agent2api:before-model-sync-20260913`。

后续在后台 **模型 → 刷新模型目录** 即可走修复后的双源读取逻辑，原渠道开关继续生效。
API Key、管理员密码、签到逻辑和账号启停状态均保持原样；凭证与数据库备份不进入 Git。
