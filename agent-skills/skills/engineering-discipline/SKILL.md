---
name: engineering-discipline
description: 规范需求澄清、变更安全、系统排障、跨层审查和交付验证。适用于功能开发、重构、缺陷修复和多人协作。
metadata:
  version: 1.0.0
  agent_created: true
---

# Engineering Discipline

## 工作方式

先写清目标、边界、约束和验收，再列受影响的调用方与验证方式。排障先复现并分层定位根因；修改保持最小且可回滚，不用宽泛异常处理掩盖问题。

交付前核对代码、测试、配置、权限、数据和发布影响。变更记录应说明做了什么、验证了什么、还缺什么，不把静态检查结果描述成真实环境验证。

## 按需参考

- 详细检查表见 [references/change-workflow.md](references/change-workflow.md)。
