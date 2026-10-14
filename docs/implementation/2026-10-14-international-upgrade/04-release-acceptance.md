# 发布前验收记录

- 用户授权：验收 → 提交 → 推送。
- 分支：`main`；审查基点：`6a69b9249f0c70a3e38d564df2199a89e8c527e8`。
- 范围：国际凭证维护、国际每日奖励、四渠道限流换号及对应管理界面；不部署、不操作正式账号。

## 验收发现与红绿证据

两份 Dockerfile 的运行时COPY清单都遗漏新模块。新增 `tests/test_container_packaging.py`，把各清单声明的源码复制到临时目录，通过 `python -I` 启动应用并访问 `/health`，防止仓库路径掩盖缺依赖。

1. 修改清单前：两个用例均失败，异常为 `ModuleNotFoundError: No module named 'daily_rewards'`。
2. 在两份清单加入 `daily_rewards.py` 与 `rate_limits.py`。
3. 同样两个用例均通过（0.78秒）；无需修改应用逻辑。

## 最终检查

| 检查 | 结果 |
|---|---|
| `AGENT2API_UI_ARTIFACTS=/tmp/agent2api-intl-upgrade-ui .venv-test/bin/python -m pytest -q` | 543 passed，96.31秒，无失败/跳过 |
| `cd frontend && npx tsc -b --pretty false` | 退出0 |
| `cd frontend && npm test` | 7 passed，无失败/跳过 |
| `cd frontend && npm run build` | 退出0，JS/CSS产物与容器内构建一致 |
| `git diff --check` | 退出0 |
| `docker build --pull=false -f Dockerfile -t agent2api:acceptance-intl-20261014 .` | 成功 |
| `docker build --pull=false -f Dockerfile.fullbuild -t agent2api:acceptance-intl-20261014-full .` | 成功 |

既有构建提示：前端单包超过500kB；不影响本轮构建结果。

## 两种实际镜像的HTTP冒烟

分别以非root用户启动真实入口 `python app.py`，不映射对外端口，使用 `--network=none` 和 `/data` 临时内存文件系统，显式关闭验收容器的自动更新。

两种镜像均验证：

- Docker HEALTHCHECK达到healthy，`GET /health`返回200与正确服务名称。
- `GET /`与页面引用的JS、CSS资源均返回200且非空。
- 使用独立测试管理员完成 `POST /api/login`；`GET /api/accounts`为空。
- `GET /api/platforms`提供四个原渠道，两个国际渠道的维护种类为 `international_activity`。
- `GET /api/settings`包含默认60秒冷却；测试API Key读取 `GET /v1/models`正常。
- 运行结束删除验收容器及临时数据。正式容器ID、镜像、启动时间在验收前后完全一致，未覆盖正式镜像标签。

本机证据（不入Git）：

- `/tmp/agent2api-intl-acceptance-containers.json`：两份镜像HTTP检查结果与生产未变化断言。
- `/tmp/agent2api-intl-acceptance-smoke.py`：本次隔离容器检查脚本。
- `/tmp/agent2api-intl-upgrade-ui/`：真实浏览器截图；已查看国际奖励摘要、号池与冷却设置布局。

## 审查与发布边界

已复用开发阶段已完成的审查，仅补审打包修复与回归用例。Standards无遗留问题；Spec新增1项P1已完成红绿及实际镜像验证。

本记录是提交前验收结果，随本次升级提交；实际提交和推送由Git历史提供凭据。此次验收使用隔离上游与空库，没有实号奖励到账结论，也没有生产部署。
