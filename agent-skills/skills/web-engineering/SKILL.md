---
name: web-engineering
description: 设计、实现和验证现代 Web 前端功能，覆盖页面、组件、状态、接口与测试。适用于前端页面改造、交互缺陷、组件复用和构建回归。
metadata:
  version: 1.0.0
  agent_created: true
---

# Web Engineering

## 工作方式

先确认用户流程、数据边界和验收条件，再定位页面、路由、状态和接口的真实调用链。优先复用现有组件与约定，避免在单页状态和全局状态之间重复存储同一事实。

实现后检查加载态、空态、失败态、权限边界、键盘可用性和窄屏布局。组件测试覆盖行为而非内部实现；构建或类型检查作为交付门槛。

## 按需参考

- 详细实施清单见 [references/frontend-workflow.md](references/frontend-workflow.md)。
