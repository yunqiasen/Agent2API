# 四渠道凭证自动续期

## 行为

- WorkBuddy、TraeWork、CodeBuddy 国际、WorkBuddy 国际共用保活流程；腾讯渠道调用各自域名的 `/v2/plugin/auth/token/refresh`，Trae 继续用 ExchangeToken。
- `auto_refresh=1` 时维护线程每 300 秒扫描启用账号；请求前也检查。默认提前 900 秒，设置沿用现有面板。
- 缺失数据库期限时从 JWT exp 补齐；该解码仅用于续期时间提示，绝不当作服务端鉴权验证。秒/毫秒统一处理。
- 无 access 到期信息但有 refresh 时最多每小时尝试一次；失败从 60 秒退避至最多 3600 秒。仍有效的旧 access 可继续用，续期错误单独保留。
- 手动强制刷新失败明确返回错误；无 refresh 的账号提示重新登录/完整导入；额度查询成功不会清除续期失败记录。
- 轮换后的 access、refresh、expiry、成功时间在同一事务保存。同一国际账号的相同 refresh 会话跨卡片同步；不同会话不互相覆盖。
- 旧快照等待锁后优先使用已轮换结果；刷新 HTTP POST 不做透明网络重放，避免重复消耗一次性 refresh。
- 同渠道 token-only 重导入保留旧 refresh；JWT 显示身份/会话不同则要求同时提供对应 refresh。
- 持久化字段：last_refresh_at、last_refresh_attempt、refresh_error、refresh_failures、refresh_retry_at。界面显示未过期/未知期限与续期状态，二者分开。
- 日志 `refresh_trace` 记录账号 ID、渠道、成功/失败、到期时间。token 与 JWT 在错误中脱敏。

## 第四渠道

`workbuddy_intl` 使用 WorkBuddy 国际官方客户端标识，登录、导入、公共加入、卡片、Key 绑定、测活、动态目录、开关、优先级、Chat/Responses 流与非流、日志/usage 均复用现有流程。
旧三渠道排序不变，新渠道默认追加末尾；同名模型遵守全局开关，所有渠道关闭后对外隐藏，管理员测活仍可执行。
国际账号共享额度时汇总只算一次，以最新额度快照为准；调用统计仍按实际渠道记录。未验证的图像/签到能力不伪造成功，也不会把国际凭证发往国内图像网关。
参考仓库的静态列表不直接当成可用目录；模型以当前账户的官方目录/真实探测为准。

## 核验

回归覆盖：历史 expiry=0、未知有效期、force 失败/无 refresh、退避、并发、轮换落库、同国际会话同步、不同会话隔离、重导入、错误脱敏、四渠道优先级/开关/Key、Chat 与 Responses 流/非流与真实 usage 统计。

真实续期结果在部署验证后追加；自动刷新不意味着上游撤销或过期的 refresh token 能恢复，遇到此类错误须重新登录。

### 2026-09-13 实测结果

- 全量回归 **169 passed**；TypeScript 检查通过；Vite 构建通过（保留 bundle >500 kB 提示）。
- 独立浏览器测试号池：四渠道筛选、续期成功/错误显示、WorkBuddy 国际登录选项、模型分类、四位优先级交换及保存后重载通过，无页面 JS 错误。未改生产优先级作 UI 测试。
- 正式号池 **16/16** 完成真实凭证交换且新 access/refresh 已落库：WorkBuddy 7、TraeWork 6、CodeBuddy 国际 3。所有账号期限已补齐，续期错误为空。
- WorkBuddy/CodeBuddy 国际续期后以账号接口确认 UID。Trae #5 的 GetUserInfo 接口返回 403，但 ExchangeToken 成功且持久化完成，随后新凭证额度查询成功、glm-5.2 回复 pong；其余 Trae 使用新凭证额度查询验证成功。资料接口错误和刷新结果明确分开，未据此误报刷新失败。
- **WorkBuddy 国际真正的刷新端点已验证**：#19 保留原 codebuddy 归属，单次通过 workbuddy_intl 适配器执行 refresh，结果经统一生命周期落库；新凭证在 workbuddy.ai 验证同 UID，并获取 20 个目录项、glm-5.3 完整回复 OK（HTTP 200，stop，usage.credit=0.01）。没有复制账号或创建虚假第四渠道记录。
- 新渠道 OAuth start 与 pending 轮询实测通过；本轮未代替用户完成新的浏览器登录授权。原始加密桌面 .info 不属于已验证导入格式，支持明文 flat JSON 与 auth/account JSON。
- 原模型开关与模型注册表 SHA256 与部署前完全一致；旧优先级的相对顺序保留，workbuddy_intl 追加最后。第四渠道当前没有正式账号，登录/导入后按其真实目录同步，不自动曝光测试目录。
- 容器 agent2api healthy；127.0.0.1:8530 与 Tailscale 100.126.43.55:8530 的 /health 均成功，容器与工作区核心源码 hash 一致。
- 凭证刷新后到期时间完全使用上游返回/JWT，部分旧会话没有延长到期日期，绝不把“交换成功”说成“无限续命”。上游撤销、绝对会话期限仍可能要求重新登录。

### 回滚与备份

升级前 SQLite 备份：容器 `/data/backups/before-four-channel-refresh-20260913-172555.db`。
刷新后 SQLite 备份：容器 `/data/backups/after-refresh-20260913-174509.db`。
旧代码镜像：`agent2api:pre-refresh-20260913`。

**回滚代码时保留刷新后的数据库，勿恢复刷新前的旧 token 快照，否则可能丢失已经轮换的新凭证。**
本轮未提交或推送 Git。

### 2026-09-17 接入闭环补齐

后续已修复账号接入后的目录同步、工具响应聚合、OAuth 终态/过期及前端重叠轮询，并在重新授权时清除旧续期失败退避。当前行为及独立的真实验收见 [补齐记录](workbuddy-intl-parity-2026-09-17.md)，上文 9 月 13 日数据作为历史快照保留。
