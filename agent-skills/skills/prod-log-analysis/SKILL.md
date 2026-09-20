---
name: prod-log-analysis
description: 在受控只读范围内聚类运行日志、定位错误根因并形成证据链。适用于线上异常、错误频次变化、数据错误和发布回归分析。勿用：环境与容器排障→ops-workbench，一般缺陷复现→engineering-discipline。触发：生产日志、线上报错、报错日志、ELK、ERROR、错误聚类、错误频次、时间窗、最近 24 小时、根因、证据链、该谁修、改动责任人、git blame、Unknown column、DDL 没执行、数据错误、发布回归。
metadata:
  version: 1.0.0
  agent_created: true
---

# Production Log Analysis

## 工作方式

先固定时间窗、服务、版本、请求标识和时区，再聚类错误类型、频次、首次出现时间和影响范围。将日志与发布、配置、依赖和指标对齐，避免仅凭一条异常猜测根因。

分析过程只读并遵守脱敏边界。结论按事实、推断、反证和下一步验证分层；输出中不复制令牌、个人数据、请求正文或内部秘密。

## 按需参考

- 错误聚类和归因步骤见 [references/log-analysis.md](references/log-analysis.md)。
