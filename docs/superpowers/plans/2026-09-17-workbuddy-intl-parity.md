# WorkBuddy 国际接入补齐 Implementation Plan

> **For agentic workers:** 使用 superpowers:executing-plans 按任务执行；每项先重现失败，再实现与验证。用户已批准修复，保留本地所有既有修改，本轮不提交推送。

**Goal:** 第四渠道接入后立即拥有可维护、可诊断的模型目录，Chat/Responses 工具链完整，网页登录正确处理终态。

**Architecture:** 沿用四渠道共用号池与凭证生命周期。在 discovery 增加单账号增量目录同步，在 providers 做 WorkBuddy 国际请求适配，在 proxy/responses 修复共享工具响应。React 复用可取消的串行登录轮询。

**Tech Stack:** Flask、SQLite、requests、pytest、React、TypeScript、Vite、Docker。

## 约束

- 原三渠道、模型开关、显式 Key 绑定、逐模型优先级保持原语义。
- 单账号目录只增量加入：历史模型、其他账号可见模型和已验证兼容模型保留。
- 目录读取失败与账号接入结果分开；错误脱敏，测活失败不改变账号启用状态。
- WorkBuddy 国际不伪造签到/图像能力；不自动复制正式国际账号。
- OAuth 新浏览器授权和真实上游已有凭证测试分开记录。

## Task 1：账号目录闭环

**Files:** `discovery.py`、`providers.py`、`app.py`、`frontend/src/features/accounts/accounts.tsx`、`frontend/src/lib/api.ts`、`tests/test_account_catalog_sync.py`。

- [x] 四渠道导入、腾讯网页登录/公共加入、Trae 回调写失败测试：
  ```python
  assert response.get_json()['model_sync']['ok']
  assert client.get('/api/platforms/workbuddy_intl/models').get_json()['models']
  ```
- [x] `.venv-test/bin/python -m pytest -q tests/test_account_catalog_sync.py`，确认未同步导致失败。
- [x] 共享 `discovery.sync_account_catalog(row)` 调用当前账号上游目录，增量原子合并；返回 ok/account_id/platform/count/added/error，使用有限 timeout。
- [x] 所有接入入口返回 model_sync（批量为 model_sync_results）；增加管理员 `POST /api/accounts/<id>/models/sync` 重试。
- [x] 卡片增加“刷新模型”与目录错误状态；重导入及手动同步后刷新所有现有卡片目录，保持已选模型。
- [x] 跑目录回归及现有测试；检查导入成功但目录失败、并发并集、模型开关不变。

## Task 2：工具协议闭环

**Files:** `proxy.py`、`responses.py`、`providers.py`、`tests/test_tool_protocol.py`。

- [x] 先测 SSE 分片聚合：`message['tool_calls'][0]['function']['arguments'] == '{"city":"Shanghai"}'`；多工具按 index 独立聚合。
- [x] 先测 Responses 指定工具：`converted['tool_choice']['function']['name'] == 'weather'`。
- [x] 先测 WorkBuddy 国际 function/flat tools/none/max_completion_tokens/DeepSeek 历史 reasoning，原请求不被污染。
- [x] 运行 `.venv-test/bin/python -m pytest -q tests/test_tool_protocol.py`，逐项确认 RED 后修复。
- [x] 测 Chat/Responses 流/非流完整工具调用及第二轮 tool 结果，usage 与计数正常；推理内容不混入正文，output_index 不冲突。

## Task 3：OAuth 终态与前端轮询

**Files:** `providers.py`、`app.py`、`frontend/src/lib/login-poll.ts`、`frontend/src/features/accounts/accounts.tsx`、`frontend/src/features/join/join-page.tsx`、登录回归测试。

- [x] `11217` 是 pending，其他业务错误终止；state 超过 300 秒返回明确 expired，done 响应保持旧契约。
- [x] 先测试终态错误不继续返回 pending；资料请求带渠道 UA；凭证完整后再入库。
- [x] 前端串行轮询，关闭/换渠道/返回后取消，旧回包不写 UI；done 进入完成态，额度与目录失败独立提示。

## Task 4：验收与部署

- [x] 全量 `.venv-test/bin/python -m pytest -q`；`cd frontend && npm exec tsc -- --noEmit && npm run build`。
- [x] 独立临时 DB 浏览器验收：导入→目录自动出现→测活；失败重试；慢轮询与关闭；原四渠道设置。
- [x] 已有国际凭证按 WorkBuddy 国际 profile 做小额真实目录/Chat/Responses/工具验证，正式账号归属不动。
- [x] 更新 README、维护记录，明确模拟与上游实测范围。
- [x] SQLite 在线备份、保留旧镜像；构建后仅更新 agent2api 容器。
- [x] 验证 localhost/Tailscale health、源码 hash、正式账号数、原模型开关/优先级保留。

验收证据与部署结果：[WorkBuddy 国际补齐记录](../../workbuddy-intl-parity-2026-09-17.md)。新浏览器真人授权不冒充已验收；详见记录中的边界。
