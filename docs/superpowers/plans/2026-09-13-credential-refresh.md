# 四渠道凭证保活实施计划

**目标：**修复已复现的三项续期缺陷，WorkBuddy 国际使用相同生命周期，不复制刷新代码。
**结构：**credentials.py 只负责时间/凭证元数据解析与脱敏；providers.py 负责上游交换；proxy.py 负责续期调度与错误语义；db.py 原子保存轮换结果；现有界面展示独立续期状态。
**技术：**Python / Flask / SQLite / pytest / React / TypeScript。

- [x] tests/test_credentials_refresh.py：用真实临时 DB 和模拟上游覆盖缺失 exp、旧数据补齐、force 失败、无 refresh、退避、轮换持久化、并发、相同国际会话共享、导入保留。
- [x] 先运行 `.venv-test/bin/python -m pytest tests/test_credentials_refresh.py -q`，确认业务断言失败，再实现 credentials.py、providers.py、proxy.py、db.py、app.py。
- [x] tests/test_workbuddy_intl.py：覆盖第四渠道登录/刷新头与响应、模型目录、四路优先级、开关和 Key、双协议、共享额度与旧数据兼容，再实现四渠道接入。
- [x] 前端共用渠道列表与卡片，补续期成功时间/独立失败原因；原先三位排序改为按渠道数量渲染。
- [x] 执行全量 pytest、TypeScript/build、git diff --check；容器构建包含新模块。
- [x] 备份 SQLite 后部署，仅重建本项目容器；真实续期通过正常落库路径验证，逐号结果脱敏。不把 mock 测试当作真实授权成功。
- [x] 更新 README 和维护说明，记录参考仓库 MIT 来源、真实验证与尚未验证项。用户未要求 Git 提交，本轮保留未提交修改。

验收记录见 ../../credential-refresh-2026-09-13.md。新账号网页登录完整授权留给实际新增账号时执行，已验证起始/等待端点与旧国际凭证真实续期；未声称完整新授权已通过。
