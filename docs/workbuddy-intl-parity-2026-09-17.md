# WorkBuddy 国际接入补齐与验收（2026-09-17）

## 本轮修复

四渠道仍为 `workbuddy` / `trae` / `codebuddy` / `workbuddy_intl`，沿用现有号池、自动续期、Key、模型开关、优先级、额度和日志结构。

| 链路 | 修复后的行为 |
|---|---|
| JSON/Token 导入、网页登录、公共加入、Trae 回调 | 保存凭证后自动读取该账号官方目录，返回独立的目录同步结果 |
| 同渠道多个账号 | 单账号目录增量合并，原子写入；保留其他账号模型、已验证兼容模型、模型开关及优先级 |
| 目录失败 | 账号仍已入库，错误与额度/续期错误分别报告；保留旧目录，不停用账号 |
| 卡片 | 模型下拉旁增加“刷新模型”按钮；导入后旧卡片一并更新目录，保留仍存在的选择；测活仍锁定当前账号 |
| Chat 非流式 | 按 choice/tool index 聚合工具 ID、名称及参数分片，保留 finish_reason 和真实 usage |
| Responses | 指定工具名保持准确；支持多轮工具结果回传；推理独立成项，不混入正文；事件带 type/sequence_number，完成事件使用 response 包装，output_index 唯一 |
| 输出截断 | 返回 incomplete 与原因，空 SSE 不再伪装为成功 |
| WorkBuddy 国际请求适配 | developer→system；兼容 flat tools、max_completion_tokens；none 移除新工具定义；DeepSeek 多轮缺失 reasoning_content 时兼容补齐 |
| 跨渠道切换 | Trae 请求转换不修改原始 messages，后续国际渠道保留原工具格式和提示词 |
| OAuth | 11217 才是等待；其他终止业务码明确报错；本地授权 300 秒过期；同 state 串行交换，成功后不重复入库 |
| 前端授权 | 串行轮询、真实时间截止；关闭/换渠道/取消后丢弃旧回包；公共加入 done 进入完成页；重新授权清除旧续期失败/退避 |
| 额度呈现 | 公共加入的额度读取失败显示“额度尚未同步”，而非伪造 0 / 0 |

WorkBuddy 国际仍不展示签到按钮；未验证的国际图像能力不当作已支持。测活失败不会自动关闭模型或账号。

## 接口与使用

- 号池 → 添加账号 → **WorkBuddy 国际** → 扫码或网页登录。
- 或在“导入 Token”选择同一渠道，粘贴明文 access/refresh JSON；支持参考项目的 flat JSON 与 `auth/account` 结构。原始加密客户端文件不是这里的输入格式。
- 成功后自动同步模型；读取失败可在卡片模型下拉旁点击刷新图标。目录出现仅表示上游列出，实际可用性用“测活”验证。
- 在模型页为第四渠道设置模型开关与优先级；调用方沿用 `/v1/chat/completions` 或 `/v1/responses`，指定渠道则使用绑定该渠道的 API Key。

新增管理接口：

```text
POST /api/accounts/<id>/models/sync
```

需要管理员会话，返回 `ok/account_id/platform/count/added/models/error`；同步失败仍返回旧 `models`，`ok=false`。账号不存在为 404，未登录为 401。

导入结果新增 `model_sync_results` 数组；网页登录、公共加入及 Trae 回调新增 `model_sync`。额度错误继续使用 `quota_error`/`refresh_errors`，不会与目录失败混为一谈。

## 测试证据

- Python 回归 **233 passed**，其中真实 Chromium + 临时 SQLite 的界面测试 5 项。
- 前端轮询单元测试 **7 passed**：慢请求不重叠、取消后忽略旧回包、成功/done/失败终态、真实时间超时、HTTP 鉴权错误停止重试。
- TypeScript `tsconfig.app.json` 类型检查与 Vite 构建通过。保留现有 bundle >500 kB 提示；Node MockTimers 有实验性 API 提示。
- 界面验证：重新导入后旧卡片目录刷新、卡片目录错误/重试、选择模型并测活、授权失败重试、公共加入完成态及额度未知提示。测试未使用正式账号。

重跑：

```bash
.venv-test/bin/pip install playwright
.venv-test/bin/python -m playwright install chromium
cd frontend
npm exec tsc -- -p tsconfig.app.json --noEmit
npm test
npm run build
cd ..
.venv-test/bin/python -m pytest -q
```

已有浏览器可通过 `AGENT2API_TEST_CHROMIUM` 指定可执行文件。未安装 Playwright 时，仅浏览器测试文件跳过；Python 协议和数据层测试继续执行。

## 真实 WorkBuddy 国际验证

保持正式 #19 的 `codebuddy` 归属，使用其当前有效国际凭证按 WorkBuddy profile 访问官方 `www.workbuddy.ai`；临时号池独立存储，未向正式第四渠道复制账号，未强制轮换凭证。

- 资料接口返回同一个 UID。
- OAuth 获取链接、等待授权轮询成功。**新的浏览器登录授权仍由账号持有人完成**；完整入库路径以可控上游的集成/界面回归验证，不将其标记为本轮真人授权成功。
- 官方 `/v2/enterprises/personal/models` 与 `/v3/config` 的 CLI 并集为 **22 项**：

```text
default-model
fast-model
balanced-model
primary-model
deep-model
hy4-preview
hy3
gpt-5.6-sol
gpt-5.6-terra
gpt-5.6-luna
gpt-5.5
gpt-5.4
gpt-5.3-codex
gemini-3.5-flash
glm-5.3
glm-5.2
kimi-k3
kimi-k2.6
deepseek-v4.1-flash
deepseek-v4.1-flash-sg
gpt-6-astra
kimi-k2.8-preview
```

目录是当时该账号的上游清单，**不等于 22 项均已逐一测活**。

| 模型 | 接口 | 返回方式 | 指定工具 → 第二轮回传 | 上游真实扣费合计 |
|---|---|---|---|---|
| glm-5.3 | Chat | 非流式、流式 | 两组均准确选择 echo_probe，参数完整，第二轮回复 OK | 四组 8 次请求合计 0.23 credits |
| glm-5.3 | Responses | 非流式、流式 | 两组均准确选择 echo_probe，参数完整，第二轮回复 OK | 同上 |
| deepseek-v4.1-flash | Chat 非流式 / Responses 流式 | 各一组 | 两组均准确调用工具并在第二轮回复 OK | 4 次请求合计 0 credits |

DeepSeek 本次真实返回没有 reasoning_content；推理分片与历史兼容由专项回归覆盖，不将本次零推理输出当成真实推理链验收。扣费只是本次 `usage.credit` 实测，不推断永久免费或固定价格。

## 参考来源

参考 [workbuddy2api-intl](https://github.com/ardeyouxipianyi/workbuddy2api-intl/tree/7c93b46ad045798c5da5953f0a5bc4e9e8a7b8f9) 的协议兼容做法；保留 [第三方声明](../THIRD_PARTY_NOTICES.md)。未引入其独立号池、静态模型曝光或提示词改写逻辑。

## 部署结果

2026-09-17 23:46（Asia/Shanghai）完成更新：

- `agent2api` 容器为 running / healthy。
- 本机 `http://127.0.0.1:8530/health` 与 Tailscale `http://100.126.43.55:8530/health` 均返回 ok。
- 两个入口实际加载新前端 `index-DyFaK6V3.js`；8 个后端模块与 3 个前端产物的 SHA256 全部匹配工作区。
- 原 16 个账号的 ID、渠道、UID、启用状态和备注保留；API Key ID、模型开关、优先级、模型注册表、管理员密码 hash 与部署前一致；SQLite integrity_check=ok。
- 生产 HTTP 管理接口返回 16 个账号；新单号目录同步接口已生效。
- 正式 WorkBuddy 国际仍为 **0 个账号、0 项目录**：本轮临时验证不自动复制旧国际账号，也不向正式目录填入测试结果。首次导入/网页登录后自动同步其目录，或在卡片点击刷新模型。
- 数据库在线备份：`/data/backups/before-intl-parity-20260917-233809.db`。
- 旧代码回滚镜像：`agent2api:pre-intl-parity-20260917-233809`。回滚代码时保留当前数据库，避免覆盖已轮换的凭证。
- 本轮未提交或推送；Git 基线仍为 `4042c09`。
