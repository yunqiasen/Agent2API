# codebuddy-intl-cpa：客户端归属与 Agent2API 对照

调查日期：2026-10-14（Asia/Shanghai）。

- 对象：[zidanefaqih/codebuddy-intl-cpa](https://github.com/zidanefaqih/codebuddy-intl-cpa)，版本 `0.9.6`，commit `03001d4599cebcd7a348d9d793f5174a558ea2cb`。
- 本地对照：Agent2API commit `6a69b92`，目录 `/home/div/1_Project_dir/AI/hy3api`。
- 方法：沿登录、凭证地区判断、刷新、聊天、模型目录、成长奖励、换号路径读源码；只读核对生产账号渠道。
- 范围：没有安装或运行插件，没有使用账号发起模型请求、签到或活跃上报，没有修改生产代码、设置及凭证。本轮未验证国际 OAuth 完整登录与次日奖励到账。

## 结论

该插件的国际调用主线路是 **CodeBuddy 国际 CLI 协议**。代码同时识别 CodeBuddy 国际与 WorkBuddy 国际凭证，但把国际聊天、刷新、模型目录及计费/成长中心请求统一送到 `www.codebuddy.ai`。

它不是一个新的第五客户端。Agent2API 已有 `codebuddy`（CodeBuddy 国际）和 `workbuddy_intl`（WorkBuddy 国际）两条对应渠道；当前国际账号在 WorkBuddy 国际渠道，编号 #22。

“每日 30 积分”对应独立的成长中心活跃奖励，不是国内普通签到。Agent2API 当前尚未实现这套奖励路径。插件源码中的次日 30 积分到账属于作者记录的历史实测，不是本轮对用户账号的到账验证。

## 1. 实际对接哪个客户端

| 源码事实 | 含义 | 一手证据 |
|---|---|---|
| `providerName = "workbuddy"` | 内部标识沿用 workbuddy，单看目录名容易混淆 | [main.go:80–102](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/main.go#L80-L102) |
| 国际网关 `https://www.codebuddy.ai`，UA `CLI/2.63.2 CodeBuddy/2.63.2` | 实际使用 CodeBuddy 国际 CLI 链路 | [main.go:84–92](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/main.go#L84-L92) |
| `isGlobalDomain` 接受 codebuddy.ai、workbuddy.ai 及各自子域 | 两类国际凭证均归为 Global；这是源码地区判断，不是本轮实号联通测试 | [billing.go:19–25](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/billing.go#L19-L25) |
| 聊天、刷新、模型路径经 `upstreamBaseFor` 统一使用上述国际网关 | 没有分别保持两个国际客户端的网关 | [main.go:595–614](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/main.go#L595-L614) |
| `billingBaseGlobal = "https://www.codebuddy.ai"` | 计费与成长中心也走 CodeBuddy 国际域名 | [management.go:18–23](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/management.go#L18-L23) |

部分旧注释仍把国际网关写成 workbuddy.ai，与当前常量不一致。本结论采用实际赋值和调用关系。凭证兼容也不等于两个官方客户端的模型、界面和功能完全相同。

该项目是 CPA 的 Go/CGO 原生共享库插件，构建产物 `workbuddy.so`，依赖 CPA 插件 ABI；Agent2API 若借鉴需适配现有 Python 渠道架构，而不是直接安装这个库。证据：[Makefile:1–16](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/Makefile#L1-L16)、[main.go:75–77](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/main.go#L75-L77)。

## 2. OAuth 宣传与实现的区别

已有国际凭证的解析、识别及按国际网关刷新均有实现。但插件内置的新登录入口仍固定从国内 `copilot.tencent.com` 的 `platform=CLI` 开始：

- 入口常量：[main.go:97–100](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/main.go#L97-L100)。
- `handleStartLogin` 没有读取地区选择，直接请求上述常量并返回服务端 `authUrl`：[oauth.go:68–86](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/oauth.go#L68-L86)。
- 令牌轮询、账号信息沿用国内入口：[oauth.go:108–150](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/oauth.go#L108-L150)。
- 已有国际凭证刷新则走国际 base：[main.go:598–610](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/main.go#L598-L610)。

本轮未验证服务端返回链接及后续重定向。结论仅为“新登录起点固定国内”，既不宣称国际 OAuth 已经实测打通，也不据此断言一定失败。

## 3. 每日 30 积分实际做了什么

独立的 `dailybonus.go` 先读成长中心当天活跃状态；已有记录就跳过，否则上报一条聊天活跃事件。**该奖励函数没有实际调用聊天生成接口。** 上报后状态记为 `reported`，含义是“已上报”，不是“30 积分已到账”。

证据：[dailybonus.go:172–229](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/dailybonus.go#L172-L229)、[dailybonus.go:270–318](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/dailybonus.go#L270-L318)。

作者在源码记录：2026-09-28 至 09-29 的测试中，6 个只上报事件的国际账号及 1 个登录客户端的账号次日获得 30 积分奖励包，有效期 30 天。这是**作者历史记录**，本轮没有在用户账号重复验证，也不代表今天所有账号均有相同资格或到账规则。证据：[dailybonus.go:13–24](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/dailybonus.go#L13-L24)。

默认自动开启，每天本地时间 08/12/16/20 点检查，仅处理国际账号；当天活跃状态采用接口返回的热力图。证据：[dailybonus.go:26–36](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/dailybonus.go#L26-L36)、[dailybonus.go:62–79](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/dailybonus.go#L62-L79)、[dailybonus.go:285–290](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/dailybonus.go#L285-L290)。

## 4. 429 故障转移的增量

插件识别 HTTP 429、业务码 6004 及限流文字，为当前账号设置冷却后尝试其他账号。优先采用上游 reset 时间，解析失败默认冷却 30 分钟；向客户端输出流式内容后不再换号重放。硬额度耗尽与普通限流分开处理。

证据：[failover.go:30–46](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/failover.go#L30-L46)、[failover.go:81–129](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/failover.go#L81-L129)、[retry.go:391–469](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/retry.go#L391-L469)。

Agent2API 已有基础失败换号：401/403/429/5xx 可重试，同一请求排除已尝试账号。当前源码未见该插件式的 6004 专门分类与跨请求限流冷却。因此不是从零缺失故障转移。证据：[proxy.py:852](/home/div/1_Project_dir/AI/hy3api/proxy.py#L852)、[proxy.py:1070](/home/div/1_Project_dir/AI/hy3api/proxy.py#L1070)、[db.py:583](/home/div/1_Project_dir/AI/hy3api/db.py#L583)。

## 5. 我们已经有哪些对应渠道

| Agent2API 渠道 | 内部 ID | 登录 platform | API base | 当前账号 |
|---|---|---|---|---|
| CodeBuddy 国际 | `codebuddy` | `ide` | `www.codebuddy.ai` | 0 个；渠道实现已存在 |
| WorkBuddy 国际 | `workbuddy_intl` | `workbuddy-ai` | `www.workbuddy.ai` | 1 个，#22，启用 |

渠道定义：[providers.py:63](/home/div/1_Project_dir/AI/hy3api/providers.py#L63)。账号数量来自 2026-10-14 运行容器 `agent2api` 的 `/data/agent2api.db` 只读查询，没有读取或展示令牌：

```sql
SELECT platform, count(*) AS account_count
FROM accounts
WHERE platform IN ('codebuddy', 'workbuddy_intl')
GROUP BY platform;

SELECT id, platform, enabled
FROM accounts
WHERE platform IN ('codebuddy', 'workbuddy_intl')
ORDER BY id;
```

返回仅有 `workbuddy_intl: 1`、`id=22, enabled=1`。

我们保留各客户端的网关及模型目录；同国际 UID/租户的续期锁、同 refresh 凭证的轮换同步及额度去重已有实现。证据：[db.py:514](/home/div/1_Project_dir/AI/hy3api/db.py#L514)。

两个国际渠道当前 `supports_checkin=False`，指普通签到；当前源码没有国际成长奖励实现。研究文档新增前，源码全文搜索 `daily_bonus`、`chat_request_send`、`/v2/report`、`/activity/growth/heatmap` 无命中。应区分“普通签到未开放”与“成长中心另有奖励路径”，而不是概括为国际版永久没有日常奖励。

## 6. 借鉴边界

- **不是新增渠道问题**：复用现有两条国际渠道的凭证、号池、开关、额度及状态展示。
- **额度状态可核验**：活跃已上报、服务端已记录、奖励包已到账，是三个不同状态。作者历史记录不代替当前账号的正式权益与到账核验。
- **换号有参考价值**：业务码分类、限流冷却、流式输出后的重放边界。
- **自动删除策略不宜照搬**：插件默认开启 lifecycle，国际账号额度耗尽会选 `delete`，与项目保留账号继续维护的要求冲突。证据：[policy.go:37–46](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/policy.go#L37-L46)、[policy.go:115–124](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/policy.go#L115-L124)。
- **模型列表不是更新证明**：插件读旧目录、筛选 `agents.cli`，另有静态列表；Agent2API 已优先读取各客户端 `/v3/config`。硬编码模型名不等于官方当前完整目录或实号可用。证据：[models.go:276–299](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/models.go#L276-L299)、[models.go:341–349](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/models.go#L341-L349)、[models.go:550–577](https://github.com/zidanefaqih/codebuddy-intl-cpa/blob/03001d4599cebcd7a348d9d793f5174a558ea2cb/workbuddy/models.go#L550-L577)、[providers.py:785](/home/div/1_Project_dir/AI/hy3api/providers.py#L785)。

本次只完成研究与记录，没有进入功能实现、部署、提交或推送。

## 7. 追问核验：两个客户端是否共享积分

2026-10-14 13:52:41（Asia/Shanghai），使用现有 WorkBuddy 国际 #22 的当前凭证，分别只读查询两个国际网关的 `/v2/billing/meter/get-user-resource`。使用同一 UID、租户、domain 与产品代码；没有模型调用、令牌轮换、签到或活跃上报。

| 网关 | 返回 code | 额度包数 | 周期总额 | 周期余额 |
|---|---:|---:|---:|---:|
| `www.codebuddy.ai` | 0 | 3 | 160 | 160 |
| `www.workbuddy.ai` | 0 | 3 | 160 | 160 |

比较了额度包标识、套餐代码、名称、周期额度、套餐余额、创建/到期时间与状态，不是只比较合计数字。两边规范化结果 SHA-256 均为 `4730f9d1a6075dbdc5bfcb12f3e8e1a52549727d52d7f359c382a66d83c8e2a8`，额度包标识字段存在且对照一致。

结论：#22 经两个客户端网关读到的是同一份国际个人积分账本，160 + 160 是重复展示，并非 320。共享条件是同一国际账号及同一租户，不是不同账号之间共享余额。

术语需明确：官方有 CodeBuddy 国际和 WorkBuddy 国际两个客户端；该插件只注册一个 `workbuddy` provider，接收两者的国际凭证后统一走 CodeBuddy 国际网关，不是各自独立维护两个渠道。明文凭证仍须符合插件支持的 Token JSON 格式；不表示所有客户端加密存储文件都可原样导入。

## 8. 规格阶段补充：国际活动状态接口只读核查

2026-10-14 14:52:37（Asia/Shanghai），用现有国际凭证分别GET两个国际网关的活动热力图接口。两边均返回HTTP200、业务码0，数据含cells、range、today和updated_at；各有365个日期记录，当日日期为2026-10-14，当日score为0。

这说明两个客户端各自的国际网关都存在可读取的活动状态接口，也提示实现应优先采用服务端明确的today字段，不照搬“最后一格就是今天”的假设。这不是活动提交或奖励到账验证；本次没有刷新凭证、提交活动、领取奖励或发起模型生成。
