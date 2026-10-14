## Parent

#4

Label: ready-for-agent（仅文字记录）

## What to build

两个国际客户端保留独立登录/导入/卡片，兼容参考凭证明文字段并补齐自动维护与错误展示。

## Acceptance criteria

- [x] T01–T09、T43–T44：嵌套/扁平/批量字段正确，重复导入保持归属、ID及调用开关。
- [x] 自动续期、并发轮换、同会话同步/不同会话隔离正确，额度查询不掩盖刷新错误。
- [x] HTTP与真实浏览器验收，临时数据库测试及审查通过，无正式账号操作。

## Blocked by

None (can start immediately).

## 授权

用户对完整规格实现卡回复“批准下一步”。允许TDD、全回归及代码审查；不部署生产，不提交推送。
