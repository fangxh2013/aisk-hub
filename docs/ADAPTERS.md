# 四工具协同适配矩阵

本文档定义 Codex、Claude、Antigravity、WorkBuddy（含 WorkBuddy AI）在 `aisk` 协同架构中的适配契约、会话来源、弹窗标准与能力矩阵。

---

## 一、客户端能力矩阵

| 客户端 | 内核标识 | 会话标识来源 | 钩子机制 (Hooks) | 原生弹窗支持 | 适配目录 |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **Codex** | `codex` | `CODEX_THREAD_ID` / `CODEX_SESSION_ID` | 弱依赖 (指令级) | ✅ macOS AppleScript / Windows WinForms | `adapters/codex/` |
| **Claude** | `claude` | `CLAUDE_CODE_SESSION_ID` | ✅ 原生 `settings.json` | ✅ macOS AppleScript / Windows WinForms | `adapters/claude/` |
| **Antigravity** | `antigravity` | `conversationId` | ✅ Stdio MCP 2.0 | ✅ macOS AppleScript / Windows WinForms | `adapters/antigravity/` |
| **WorkBuddy** | `workbuddy` | `CODEBUDDY_SESSION_ID` | ✅ `.codebuddy/settings.json` | ✅ macOS AppleScript / Windows WinForms | `adapters/workbuddy/` |
| **WorkBuddy AI** | `workbuddy-ai` | `CODEBUDDY_SESSION_ID` + `WORKBUDDY_CONFIG_DIR` | ✅ `.codebuddy/settings.json` | ✅ macOS AppleScript / Windows WinForms | `adapters/workbuddy-ai/` |

---

## 二、高危操作系统原生弹窗契约

凡涉及**合并主干（dev/master/main）、推送远端、落地代码（land/promote）、执行 DDL 迁移或生产发布等关键/高危操作**，必须通过当前操作系统的原生 UI 向用户请求确认，严禁静默执行。macOS 使用 `/usr/bin/osascript`；Windows 使用当前交互桌面的 PowerShell WinForms。无图形桌面或弹窗超时统一拒绝。

### 1. 标题标准协议
**标题必须直接可见对应工具名称**，格式严格为：
`动作-<工具名>｜<任务号>`

例如：
- `git提交-codex｜T042`
- `git合并-claude｜T042`
- `git推送-antigravity｜T042`
- `落地代码-workbuddy-ai｜T001`

### 2. 授权判定铁律
macOS 只有捕获到 `button returned:<操作动作>` 且 `gave up:false` 时才判定为用户真实授权；Windows 只有 WinForms 的确认按钮返回成功才授权。返回取消、超时或 UI 启动失败必须立即终止操作，严格保持原状。

---

## 三、各端深度适配规范文档索引

每个适配器的具体环境变量、会话隔离、MCP 配置、已知缺口与复现探针见各自的独立说明文档：
- [Codex 适配规范](../agent-skills/adapters/codex/README.md)
- [Claude 适配规范](../agent-skills/adapters/claude/README.md)
- [Antigravity 适配规范](../agent-skills/adapters/antigravity/README.md)
- [WorkBuddy 桌面版适配规范](../agent-skills/adapters/workbuddy/README.md)
- [WorkBuddy AI 助手版适配规范](../agent-skills/adapters/workbuddy-ai/README.md)
