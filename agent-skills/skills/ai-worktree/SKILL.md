---
name: ai-worktree
description: 管理多工具协作的隔离任务、租约、进度、验证和交付。适用于并行代理、任务接力、冲突预防和工作区恢复。勿用：具体业务代码→backend-engineering、web-engineering，环境排障→ops-workbench。触发：并行、多个 AI、多工具、协作、互不干扰、任务、认领、接力、接手、交还、租约、工作区、隔离工作区、worktree、进度、下一步、交接单、冲突预防、恢复、抢救。
metadata:
  version: 4.0.0
  agent_created: true
---

# AI Worktree

## 工作方式

一件事对应一个任务和一个隔离工作区。开工前查找并认领已有任务，修改只落在任务声明的仓库和范围内；过程中记录完成项、下一步和阻塞原因。

交付前运行任务门禁并填写验证证据。暂停时交还租约，接力时先读任务须知、目标、进度和交接单。不要用强制覆盖、批量吞冲突或绕过保护分支来缩短流程。

## 按需参考

- 状态流转和接力清单见 [references/task-workflow.md](references/task-workflow.md)。
