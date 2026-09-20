---
name: backend-engineering
description: 设计、实现和验证后端 API 与服务逻辑，覆盖契约、业务层、持久化、事务和测试。适用于接口开发、服务重构和后端缺陷修复。勿用：前端页面与组件→web-engineering，数据库建模与结构对齐→db-workbench。触发：后端、接口、API、Controller、Service、Mapper、XML、Feign、统一响应、错误码、DTO、VO、分页查询、事务、幂等、鉴权接入、MyBatis、模块骨架、单元测试、定向编译。
metadata:
  version: 1.0.0
  agent_created: true
---

# Backend Engineering

## 工作方式

先确定 API 契约、输入输出、错误语义和数据一致性边界，再按项目已有分层实现。外部调用要明确超时、重试、幂等和降级策略；数据库访问要使用参数化查询并限制返回范围。

测试优先覆盖正常路径、边界输入、依赖失败、事务回滚和重复请求。变更交付前运行最小编译、定向单测与受影响模块回归。

## 按需参考

- 分层、契约和测试清单见 [references/service-workflow.md](references/service-workflow.md)。
