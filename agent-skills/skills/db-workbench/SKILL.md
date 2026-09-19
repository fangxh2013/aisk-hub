---
name: db-workbench
description: 设计、检查和安全演进关系型数据库结构，覆盖字段、索引、迁移、快照和跨环境差异。适用于建模、DDL 评审和结构对齐。
metadata:
  version: 1.0.0
  agent_created: true
---

# Database Workbench

## 工作方式

先明确数据生命周期、读写模式、容量、并发和兼容窗口，再设计字段、约束和索引。迁移要可重复、可审计、可回滚或有明确补偿方案；生产结构检查默认只读。

评审查询计划、索引选择、锁与超时、分页边界和敏感数据暴露。跨环境对齐先生成差异，再由授权人员确认执行，不把生成 DDL 当作已执行。

## 按需参考

- 建模与迁移清单见 [references/database-workflow.md](references/database-workflow.md)。
