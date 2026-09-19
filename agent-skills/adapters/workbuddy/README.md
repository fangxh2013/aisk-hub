# WorkBuddy 适配

工具名：`workbuddy`（桌面版）。兼容 `CODEBUDDY_*` 环境变量；适配层不得把 WorkBuddy 私有记忆写入公开源代码。

两个入口（桌面版 `workbuddy` / AI 版 `workbuddy-ai`）的完整适配规范见
`../workbuddy-ai/README.md`。与本端直接相关的四条：

1. **判别变量**：现壳只注入 `WORKBUDDY_CONFIG_DIR`（本端为 `~/.workbuddy`）；
   旧壳的 `WORKBUDDY_DATA_FOLDER_NAME` / `CODEBUDDY_APP` 已废弃。
2. **项目级钩子两入口共用** `<项目根>/.codebuddy/settings.json`，一份文件只能写一个工具名，
   所以生成的是哨兵 `auto`，由 `actor.resolve_tool()` 在**运行期**解析成真正在跑的那一端
   （本端 → `workbuddy`）。写死任一个都会让另一端的会话事件与守卫以错误名义记账。
3. **MCP 是 user 作用域、两入口共用的**：`codebuddy mcp add … --scope user`
   配在 `~/.codebuddy/mcp.json`，桌面版与 AI 版同时生效。
4. **权限也落在 user 作用域**：`aisk permit` 写 `~/.codebuddy/settings.json` 的
   `permissions.allow` / `permissions.deny`，两入口**同一份文件**（同一个 codebuddy 引擎），
   写入幂等、只增不改。**不写 project 作用域**的原因（未被信任的项目目录其 allow 会降级成
   不可信层、不能越过危险命令检查）、规则语法与求值链，见
   `../workbuddy-ai/README.md` 第四节「WorkBuddy 权限模型」。
