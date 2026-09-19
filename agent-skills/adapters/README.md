# 四工具适配层

`engine/` 是跨工具内核；本目录只描述宿主差异，不复制业务知识、私有路径、凭据或会话正文。
机器可读契约位于 `spec/adapter-capabilities.yaml`。

## 共同契约

- 每次动作都携带 `tool`、`task_id`、`session_id`；无法识别工具时拒绝高风险动作。
- 高风险确认标题统一为 `动作-工具｜任务号`，不把模型名作为必填字段。
- 首选安装目标是项目级钩子文件；宿主不支持某个入口时降级为显式命令和 TTY 确认。
- MCP、弹窗、钩子或会话注入缺失都 fail-closed：可以只读本地快照，不能静默放行合并、推送、DDL 或发布。
- `verification: offline_contract` 只代表字段和降级策略通过静态校验，不代表真实客户端已加载。

## 安装目标

| 工具 | 项目级安装目标 | 会话来源 | 适配重点 | 未安装时降级 |
|---|---|---|---|---|
| Codex | `.codex/hooks.json` | `CODEX_THREAD_ID` / `CODEX_SESSION_ID` | 任务钩子、工作区和面板 | 显式命令 + `--session` |
| Claude | `.claude/settings.json` | `CLAUDE_CODE_SESSION_ID` | hooks、续租和 CLI | 显式命令 + TTY |
| Antigravity | `.agents/hooks.json`；宿主 MCP 配置由宿主管理 | `conversationId` | hooks、MCP、原生弹窗 | 显式命令 + 本地快照 |
| WorkBuddy | `.codebuddy/settings.json` | `CODEBUDDY_SESSION_ID` / `WORKBUDDY_CONFIG_DIR` | shared hooks，运行期区分桌面版与 AI 版 | 显式命令 + TTY |

WorkBuddy AI 是 WorkBuddy 的运行目标，不使用第二份死配置；通过
`WORKBUDDY_CONFIG_DIR` 在共享 `.codebuddy/settings.json` 中运行期识别。真实宿主是否触发
`SessionEnd`、是否正确归属 AI 变体，仍需客户端实测。
1. 通过统一 `ActionContext` 传递 `tool`、`task_id`、`session_id`。
2. 所有确认弹窗标题包含工具名，格式为 `动作-工具｜任务号`。
3. 不依赖历史迁移目录、外部 Git 仓库或私有业务目录。
4. 适配缺失时内核仍可运行；适配特性只增不改核心安全门禁。

WorkBuddy 两端的权限落点是 **user 作用域** `~/.codebuddy/settings.json`
（`permissions.allow` / `permissions.deny`）：两入口同一份文件、写入幂等、只增不改；
不写 project 作用域（未被信任的项目目录其 allow 会降级成不可信层）。详见
`workbuddy-ai/README.md` 第四节「WorkBuddy 权限模型」。
