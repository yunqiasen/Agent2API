# TraeWork 新模型目录修复（2026-10-07）

## 根因及证据

上一轮重复读取 `function=solo_work_lite`，把“旧场景目录刷新成功”误当作“覆盖当前客户端全部模型”。不是用户看错，也不是简单版本号落后。

- 官网下载元数据：`https://api.trae.com.cn/icube/api/v1/native/version/trae/cn/latest`，`data.solo` 当前版本0.1.69，构建2.3.87413。
- 下载官网0.1.69安装包并读取客户端 `modules/ai-agent/libai_agent.dylib` 的协议字符串，确认存在 `solo_work_remote` 及 `get_detail_param`。
- 同一账号、同一凭证、同一目录端点，单独修改版本头0.1.43→0.1.69未改变结果。
- 改目录请求 `function` 为 `solo_work_remote` 后，上游直接返回 Step-5-Preview、GLM-5.3、DeepSeek-V4.1-Flash。
- 6个Trae账号均成功读取21个过滤后的模型；旧目录19个，本次新增3个，移除内部项 `computer_use_subagent`，其余18个保留。

## 修复

`trae.FUNCTION` 改为 `solo_work_remote`，让目录读取与聊天请求体的场景保持一致，不硬编码新模型白名单。保留既有账号凭证、API协议、签到、渠道开关和优先级逻辑。

新增 API 模型ID（以上游 config_name 为准）：

- `step-5-preview`
- `deepseek-v4.1-flash`（不是杜撰 `DeepSeek-V4.1` ID）
- `glm-5.3`

## 测试与边界

- 新增5项测试，修改前5失败，修改后5通过：目录自动接纳未来模型、4个新旧模型请求体均采用对应场景，原请求体不被修改。
- 全量Python及浏览器回归241项通过。
- 本轮规范审查：仅修改渠道场景常量及注释，无依赖或数据库结构变化。
- 本轮需求审查：目录和请求参数同步调整，不只添加展示名称；原有开关与优先级保留。
- 实际只请求模型目录，没有执行聊天/图像生成，因此不宣称已验证模型推理成功，也没有为了测试发起付费推理。
- 脱敏现场结果保存在数据卷 `/data/trae-remote-catalog-20261007.json`。

## 部署后验收

- 新镜像启动，健康检查正常。
- 正式管理接口 `/api/platforms/trae/models` 返回21项，包含3个新增ID。
- 使用已有有效API Key读取 `/v1/models`，3个新增ID均可见；当前这3个模型的路由候选均包含Trae。
- 模型开关和优先级与本轮变更前快照一致，#9仍关闭。
- 回滚镜像：`agent2api:before-remote-catalog-20261007`；数据库备份：`/data/before-remote-catalog-20261007.db`。
