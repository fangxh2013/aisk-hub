---
name: security
description: 审查认证授权、输入输出、注入、文件处理、敏感信息和高风险操作。适用于安全评审、接口改动、上传下载和凭据处理。
metadata:
  version: 1.0.0
  agent_created: true
---

# Security

## 工作方式

先定义资产、信任边界、攻击者能力和失败影响，再检查身份认证、对象级授权、输入校验、输出编码、资源限制和审计记录。默认拒绝未授权访问，默认不把秘密放进日志、提示或响应。

对文件、URL、查询、模板和命令执行分别检查路径穿越、注入、SSRF、资源耗尽和越权。高风险动作必须有明确确认、最小权限和可追溯记录。

## 按需参考

- 安全评审清单见 [references/security-review.md](references/security-review.md)。
