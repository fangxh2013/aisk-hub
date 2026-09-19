---
name: dev-release
description: 生成可审计的发布影响分析与准备清单，覆盖代码差异、数据库迁移、配置、构建和回滚。适用于发布前核对和变更交接。
metadata:
  version: 1.0.0
  agent_created: true
---

# Development Release

## 工作方式

从目标基线和当前变更出发，按仓库、服务、数据、配置、构建、验证和回滚分类。只报告能由版本、文件或命令证据支持的影响；无法确认的项目列为待人工核对。

发布清单应区分必做、条件触发和不受影响项，说明顺序、风险和验收信号。分析本身不执行发布、推送、数据库或运行环境写操作。

## 按需参考

- 发布影响清单见 [references/release-checklist.md](references/release-checklist.md)。
