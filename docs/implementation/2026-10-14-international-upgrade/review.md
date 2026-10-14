# 国际升级最终审查

- 审查基点：`6a69b9249f0c70a3e38d564df2199a89e8c527e8`。
- 输入并集：`git diff <基点>...HEAD`、`git diff --cached`、`git diff`、未跟踪文件完整内容。HEAD与基点相同，无新增提交或暂存；构建产物按对应源码及构建检查核对。
- 来源：父Issue #4、本地完整规格、CONTEXT.md、ADR-0002/0003；重点 #7 / T27–T44，并检查整个升级的接口衔接。
- #5/#6 已分别完成最终审查；此为 implement 持有的 #7 唯一最终审查及其修复闭环，不重复开启审查工作流。
- 环境未提供子代理：Standards与Spec分开自审，不将其表述为独立代理复核。

## Standards

无遗留的已确认规范问题。两个判断性代码异味已处理，不作为硬性规范违例：

1. **Possible Duplicated Code**：用量费用校验应与失败尝试记录共用。抽取 `proxy.usage_credit`，对真实零、未知、布尔和非有限数值采用同一规则。
2. **Possible Speculative Generality**：移除未使用的 `callable_accounts(model=...)` 参数和被统一分类替代的旧限流/重试判断函数；函数说明改为真实的“列出候选”语义。

## Spec

以下9组有效发现均已红绿复现并修复；无遗留有效发现。

| 优先级 | 规格依据 | 原问题与修复 | 主要验收 |
|---|---|---|---|
| P1 | T29/T34/T36：“全部合格候选耗尽”“最早可重试时间” | 同账号多个冷却范围应先取最大恢复时间，再在账号间取最小；显式重试预算结束不冒充全池失败；已有冷却与本次故障的混合原因一并保留；UTC/Z及显式偏移正确解析 | `test_retry_time_waits_for_all_limits_on_a_candidate`、`test_explicit_attempt_budget_does_not_claim_unused_accounts_failed`、`test_reset_message_respects_explicit_timezone`及混合错误回归 |
| P1 | T28/T39：“空流不伪装成功”“已发事件后不重放、不伪完成” | 仅role帧不算生成；多行SSE JSON正常合并和分类；纯文本error事件在首帧前/后均真实报错，后者不输出DONE或response.completed；预读连接中断及时关闭响应 | role-only、multiline、plain-error、prefetch-close及聊天/Responses流式用例 |
| P1 | T42：“未知费用不是确认零消耗” | null/空串/布尔/NaN/Infinity不认作实际费用；HTTP失败、预读空流及中途错误仍保留上游明确给出的费用，归属对应尝试和最终账号 | invalid-credit、failed-attempt-credit、usage-only、interrupted-stream-credit |
| P2 | T28/T37：“只在错误结构中解释”“参数错误不误标限流” | 图像队列错误按结构化code/type分类为429；参数错误文字偶然包含13212不产生冷却 | `test_image_queue_limit_uses_error_code_not_arbitrary_digits` |
| P1 | T42：“日志/API无完整凭证” | 错误code与message均脱敏；纯文本流错误先脱敏再回客户端，避免上游回显的刷新材料进入卡片/日志 | `test_error_code_echo_is_redacted_in_cooldowns_and_logs`、plain-error流式用例 |
| P2 | 管理API契约、T26 | 补齐 `/api/platforms` 的维护能力/种类、国际完成兼容字段；状态枚举与规格统一；国际claimed_credit保持null，实际奖励走独立证据字段 | `test_maintenance_contract_publishes_kinds_and_compatible_completion_fields` |
| P2 | T14/T24：“传输不明先核验”“失败后补跑” | 明确4xx或业务拒绝与传输不明分开。前者回到未完成、下次先查询再试；后者保留已提交待核验且不盲重放。活动POST关闭自动重定向重发 | rejected-activity及uncertain-submission回归 |
| P2 | T18/T21：“共享奖励只计一次”“只有新核实奖励计入新增” | 同一国际身份第二张卡片才查到的新奖励不被丢掉；批量汇总采用该身份本批最新成功核验阶段，金额按持久账本去重 | `test_batch_counts_reward_that_arrives_on_second_shared_card` |
| P2 | T26/T32：“状态真实可见”“冷却作用范围” | 纯国际筛选不再显示“今日可领0分”；混合列表明确普通签到范围；冷却卡片显示具体渠道或国际共享服务；实截图发现的摘要文字折行已修正并增加浏览器断言 | 真实浏览器：international-pool-summary、cooldown-settings-card |

## 开发阶段验证与边界

- 各修复均先运行失败用例，再实现并运行对应回归；最终完整Python/Playwright 541通过（90.52秒），前端7通过；4项本次维护/冷却浏览器用例亦单独通过。详细终检记录见 index.md。
- TypeScript、前端测试及构建通过；前端产物的单包体积提示为既有构建警告，无构建失败。
- 测试采用临时SQLite、可控上游网络与时钟，Playwright操作真实Flask应用。无真实账号生成、活动提交或令牌刷新。
- 没有生产部署、服务重启、正式配置变更、Git提交或推送；实号活动确认与实际奖励到账另行验收。

结论：Standards 2项判断性异味已处理、无硬性违例；Spec 9组问题已修复（最高P1）。两轴分别通过，不相互抵消。


## 发布前增量审查（用户授权“验收->提交->推送”）

此前已审查代码保持不变，本轮只补审两份 Dockerfile、打包回归测试与验收文档；不重复开启已完成的整轮审查。

### Standards

沿用显式运行时源码清单，不把测试、凭证、数据库或整个工作区打入镜像。回归测试仅装入最终镜像阶段声明的源码，以独立Python进程通过 `/health` 观察启动行为，临时库与网络替身隔离。无新增已确认规范问题。

### Spec

发现并修复1项P1：新增奖励/限流模块在源码运行正常，但两份 Dockerfile 的 COPY 清单遗漏，镜像启动会发生缺模块错误，影响T43/T44的可部署与兼容要求。两个打包用例先失败，补齐清单后通过；两种真实镜像的启动与HTTP冒烟进一步验证，无遗留发现。

最终完整Python/Playwright 543通过（96.31秒）、前端7通过、TS和构建通过；两种隔离容器均healthy。验收不含生产部署或实号奖励到账；用户本轮已明确授权验收后的Git提交推送。
