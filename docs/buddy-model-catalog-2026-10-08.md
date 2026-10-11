# Buddy 三渠道模型目录修复（2026-10-08）

## 根因

- 国内 WorkBuddy 使用旧 CodeBuddy 网关/profile；当前客户端5.7.6改读 `www.workbuddy.cn`。
- 旧代码并集旧基础目录和 `/v3/config`，使已移除的模型重新出现。官方 `CloudProductProvider` 实际用云配置覆盖基础代理模型。
- 原刷新按钮和空目录启动路径调用 `run_discovery()`，会产生聊天、图像探针，并复活旧候选。本次移除这两处生成探测，保留用户主动测活和外部未知模型请求链路。
- 官方 `ModelRateLimitCap.lines` 的完整 `freeId/paidId` 线路需要展开；旧代码漏掉部分付费线路。存在 paid ID 不代表本次已核验价格。

## 当前客户端依据

| 渠道 | 只读目录 | User-Agent |
|---|---|---|
| WorkBuddy | `https://www.workbuddy.cn/v3/config` | `WorkBuddy/5.7.6 WorkBuddy/5.7.6 CLI/2.156.0` |
| WorkBuddy 国际 | `https://www.workbuddy.ai/v3/config` | `WorkBuddy/5.7.6 WorkBuddy AI/5.7.6 CLI/2.156.0` |
| CodeBuddy 国际 CLI兼容线路 | `https://www.codebuddy.ai/v3/config` | `CLI/2.162.0 CodeBuddy/2.162.0` |

来源为 [WorkBuddy 国内官网](https://www.workbuddy.cn/)、[国际官网](https://www.workbuddy.ai/)、[CodeBuddy 国际官网](https://www.codebuddy.ai/) 当前下载包及官方CLI包；只静态解包，未安装或执行客户端。

现场静态证据保存在 `/tmp/buddy-client-audit/`：
- 国内/国际下载元数据 `cn-darwin-update.json`、`intl-darwin-update.json`：5.7.6；`codebuddy-darwin-update.json`：IDE4.12.1。
- `cn-asar/main/code-cache.js`：`CloudProductProvider`、`injectPaidLineModels`、`buildUserAgent`。
- 客户端 `app.asar.unpacked/cli/product.json`：基础配置、网关及内置CLI版本。
- `codebuddy-current/CodeBuddy.app/Contents/Resources/app/extensions/genie/product.json` 与 `out/extension/index.js`：IDE使用 `CodeBuddyIDE/4.12.1`；同凭证主 `craft/ask/plan` 返回5档：`default-model`、`fast-model`、`balanced-model`、`primary-model`、`deep-model`。
- `cli/package/`：官方CLI2.162.0。本项目继续使用这一兼容线路，不把CLI的21项误称为IDE有21个命名选项。

下载包元数据中的SHA256字段与实下载文件不一致，本次未宣称通过该字段的校验；依据为官方HTTPS来源的静态源码与当前接口响应交叉核对。

## 只读现场目录结果

证据快照：容器数据卷 `/data/buddy-verified-refresh-20261008.json`，不含凭证。只请求配置，未调用推理或媒体生成。

### WorkBuddy：18个聊天模型

读取账号：#1（17项）, #8（17项）, #10（18项）, #11（18项）, #16（17项）, #17（18项）。

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
minimax-m3
space-bunny
```

新增：无；原先已有当前条目。

清理旧条目：`deepseek-v3.2`、`deepseek-v4-flash`、`kimi-k2.5`、`kimi-k3`、`minimax-m2.7`。

### WorkBuddy 国际：25个聊天模型

读取账号：#22（25项）。

```text
balanced-model
deepseek-v4.1-flash
deepseek-v4.1-flash-sg
default-model
fast-model
gemini-3.5-flash
glm-5.2
glm-5.3
glm-5.3-flash
gpt-5.4
gpt-5.5
gpt-5.6-luna
gpt-5.6-sol
gpt-5.6-terra
gpt-6-astra
gpt-6-luna
gpt-6-sol
gpt-6.1-sol
grok-4.7
hy3
hy4-preview
kimi-k2.6
kimi-k2.8-preview
kimi-k3
primary-model
```

新增：无；原先已有当前条目。

清理旧条目：`deep-model`、`gpt-5.3-codex`。

### CodeBuddy 国际 CLI兼容线路：21个聊天模型

读取账号：#22（21项）。

当前没有独立CodeBuddy账号；本次以既有国际账号#22凭证只读核验CodeBuddy网关，未复制账号、未修改渠道归属。后续刷新无该渠道账号时保留目录并提示跳过。

```text
balanced-model
deep-model
deepseek-v4.1-flash
default-model
fast-model
gemini-3.5-flash
glm-5.2
glm-5.3
glm-5.3-flash
gpt-5.4
gpt-5.5
gpt-5.6-luna
gpt-5.6-sol
gpt-5.6-terra
gpt-6-astra
hy3
hy4-preview
kimi-k2.6
kimi-k2.8-preview
kimi-k3
primary-model
```

新增：`deepseek-v4.1-flash`、`glm-5.3-flash`、`gpt-6-astra`、`kimi-k2.8-preview`。

清理旧条目：`auto`、`glm-5.1`、`glm-5v-turbo`、`gpt-5.3-codex`、`hy4-preview-x`、`kimi-k2.5`、`minimax-m3`。

### 媒体目录的边界

当前配置另含国内 `hunyuan-image-alpha`、`hunyuan-image-alpha-edit`，国际 `gpt-image-2.5-sunburst`、`seedance-2.5`。本次只确认元数据存在，未接入这些独立图像/视频协议，未放进聊天目录，也未标记为已调用成功。历史已登记图像条目原样保留。

## 修复范围与验收

- `providers.py`：当前网关/profile、云配置覆盖规则、免费/付费线路；严格刷新失败直接报错，非严格兼容路径才尝试旧 `/v2/enterprises/personal/models`。
- `discovery.py`：逐账号结果、全成功替换/部分失败保留；不更改账号开关。
- `app.py`、模型页：只读刷新、错误及无账号状态真实展示；空目录启动同样仅读目录。
- 回归覆盖网关与聊天参数一致、排除旧目录复活、免费/付费线路、strict失败、部分失败保留、刷新与空目录启动不触发生成；真实浏览器以临时数据库验证失败提示与重试。
- 既有模型开关/优先级应逐值保持，账号#9保持关闭调用；签到和自动续期不在本次修改范围。

部署及正式接口验收结果见下方记录。

### 部署后正式验收（2026-10-08）

- Python及真实浏览器回归：**250 passed**；前端轮询单测：**7 passed**；TypeScript检查、前端构建、`git diff --check`通过。构建仅保留既有bundle大于500KB提示。
- 规范审查：以本轮修改前快照对比当前源码，无新增依赖/数据库迁移；令牌错误脱敏、失败保留旧数据；未更改账号开关/签到策略。测试与浏览器回归使用临时DB及可控上游。
- 需求审查：当前客户端目录与聊天网关/profile一致；云配置覆盖旧白名单、展开明确线路；目录刷新/空目录启动均无生成探针；媒体元数据未误作聊天；保留用户主动测活及旧API响应字段。
- 正式 `GET /api/models/discover`：国内6账号、Trae6账号、WorkBuddy国际1账号全部读取成功；CodeBuddy独立渠道无账号，明确返回 `ok: null` 和保留原目录的原因。
- 随后以国际#22既有凭证只读核验CodeBuddy配置，成功取得并入库21项；账号身份及渠道归属保持原样。
- 正式 `GET /api/models/discover/progress`：WorkBuddy **18聊天+2历史图像**，TraeWork **21聊天**，CodeBuddy国际 **21聊天**，WorkBuddy国际 **25聊天**。
- 使用已有启用API Key读取 `/v1/models`：**14项**；逐项等于模型注册表按现有渠道开关过滤的结果。管理页完整目录数不等于对外开启数量。
- 与部署前DB逐值比较：`model_platform_disabled`、`model_route_priority`完全一致；14个账号的ID/UID/渠道/启用状态一致，#9 `enabled=0`。
- 正式浏览器打开 `/models`：新文案及按钮可见，无页面JS错误；截图与页面文本在 `/tmp/buddy-catalog-acceptance-20261008/`。未点击测活或调用生成API。
- 本机 `http://127.0.0.1:8530/health` 与Tailscale地址 `http://100.126.43.55:8530/health` 均返回 `ok: true`；Docker状态healthy。
- 最终脱敏证据：`/data/buddy-deployed-refresh-20261008.json`、`/data/codebuddy-deployed-catalog-20261008.json`、`/data/buddy-final-acceptance-20261008.json`。
- 回滚镜像：`agent2api:before-buddy-catalog-20261008`；DB备份：`/data/before-buddy-catalog-20261008.db`（SQLite在线备份及integrity_check通过）。新镜像：`sha256:3cbfc414b3e1e652ebc0bf053fd8340f32a735c2caffd213d37c107a12ce5816`。
- 未执行Git提交或推送；已有工作区修改完整保留。
