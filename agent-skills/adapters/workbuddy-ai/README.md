# WorkBuddy AI 适配规范

工具名：`workbuddy-ai`。与桌面版 `workbuddy` 分开记账，避免两个入口共享任务租约和弹窗归属。
本文对齐 `../antigravity/README.md` 的写法：能力边界、契约、已知缺口、探针命令都写明。

---

## 一、工具识别与会话模型

`detect_tool()`（`engine/worktree/actor.py`）里，两个 WorkBuddy 入口都靠 `CODEBUDDY_*`
进门（门条件保持只看 `CODEBUDDY_*`，不动——`tests/protocol_worktree.sh` 的环境清理循环
只清 `CODEX_*` / `CODEBUDDY_*` 等前缀，扩门变量会让那道自检假红），进门后由
`_is_workbuddy_ai()` 判别是哪一个。

判别变量换过代，**只认旧变量会静默判错**：

| 壳 | 判别变量 | 桌面版取值 | AI 版取值 |
|---|---|---|---|
| 旧（已废弃） | `WORKBUDDY_DATA_FOLDER_NAME` | `~/.workbuddy` 的 basename | `.workbuddy-ai` |
| 旧（已废弃） | `CODEBUDDY_APP` | 非 `workbuddy-ai` | `workbuddy-ai` |
| **现（2026-09-19 实测）** | `WORKBUDDY_CONFIG_DIR` | `~/.workbuddy` | `~/.workbuddy-ai` |

两个旧变量在桌面版与 AI 版上**都已不再注入**（钩子进程 `env` 实测：两者都不存在），
所以只认旧变量会把 AI 端静默判成桌面版。判别逻辑取三者之或，兼容新旧壳。

判别是**单向安全**的：只有明确看到 `.workbuddy-ai` 才返回 AI 版，其余一律回落桌面版——
宿主再换变量名，最坏是两个入口合并记账，不会把桌面版误判成 AI 版。

会话号：`SESSION_ENV["workbuddy-ai"] = ("CODEBUDDY_SESSION_ID", "WORKBUDDY_AI_SESSION_ID")`。
实测 `CODEBUDDY_SESSION_ID` 与 `CLAUDE_SESSION_ID` **同值且都在**，会话级隔离可用。

`PROCESS_NAMES` 里两个入口的可执行文件名相同（都跑 codebuddy 引擎），**进程链分不开它们**；
分开记账完全依赖上面的环境变量。`session_ids()` 目前不消费 `PROCESS_NAMES`，
该表只是与 `detect_tool()` 的返回值域对齐。

---

## 二、钩子是能用的（推翻「零钩子」结论）

`docs/reviews/2026-09-15/WORKBUDDY-REQUESTS.md` 的 R3【P0】以「WorkBuddy 端零钩子」
为前提。**该前提不成立**：2026-09-19 隔离实测，项目级
`<项目根>/.codebuddy/settings.json` 里的钩子会被真实触发。

| 事件 | 实测 | `hooks.main()` 的动作 |
|---|---|---|
| `SessionStart` | ✅ 触发 | 能认领就为本会话认领并注入须知 |
| `Stop` | ✅ 触发 | 本会话持有的任务续心跳 |
| `SessionEnd` | ❌ 一次性会话（`-p`）未触发；交互式会话无法验（见下） | 自动交还 + 收割 WorkBuddy 记忆 |
| `PreToolUse` | ✅ 触发并可拦截（见第四节） | 守卫 |

所以 R3 要求的「SessionStart 自动认领 / 心跳随活动更新」**已经具备**，不需要另做退化路径；
R3 的 `cmd_open` 缺 WorkBuddy 专属动作清单一条已补（`tasks.py::cmd_open` 已分两端）。

**`SessionEnd` / `Stop` 复验（2026-09-19 探针，结论仍是「未证实」）**：用第八节第 1 条的探针
（临时 HOME + 临时 `WORKBUDDY_CONFIG_DIR`，钩子命令当探针，会话因 `Authentication required`
失败时钩子照样触发）把三个事件各跑一遍，一次性会话 `-p ok`：

| 事件 | 一次性会话（`-p ok`） |
|---|---|
| `SessionStart` | ✅ 触发 1 次 |
| `Stop` | ✅ 触发 1 次 |
| `SessionEnd` | ❌ 未触发 |

对照组 `SessionStart` / `Stop` 都命中，说明探针本身有效，`SessionEnd` 未触发是**事件侧的差异**，
不是探针写错。**但这不等于「WorkBuddy 不支持 `SessionEnd`」**——`-p` 是一次性会话，
进程内起会话又结束，可能走的是另一条收尾路径。真实关闭会话（交互式 TUI 里 `/exit` 或 Ctrl-D）
是否触发**仍未验证**：本机未登录，交互式会话卡在认证确认框，连 `SessionStart` 都不触发
（`SessionStart` 在 `-p` 下会触发、在卡住的交互式会话里不触发），探针推不到那一步。

下一步怎么验：在一台**已登录**的机器上开真实 TTY 交互会话，先确认 `SessionStart` 命中，
再显式退出，然后看 `SessionEnd` 是否落盘。在那之前，R6（记忆收割）**不要**按现状结论动手。

---

## 三、MCP 工具面（与 Antigravity 同等）

AI 端可拿到内核的 6 个 MCP 工具（`engine/mcp_server.py`），与 Antigravity 完全一致：

| 工具名 | 功能 | 返回语义 |
|---|---|---|
| `aisk_task_inspect` | 只读查协同状态机（SQLite WAL）、租约、fencing_token、挂起事件 | `live` |
| `aisk_fact_service` | 微服务运维事实（镜像 / Deployment / Job / DataId / NodePort），支持 `brief` | `offline_snapshot` |
| `aisk_fact_entry` | 对外入口与映射端口 | `offline_snapshot` |
| `aisk_env_summary` | 环境拓扑、主机 IP、命名空间、服务全景 | `offline_snapshot` |
| `aisk_repo` | 反查各仓库在本机的绝对路径 | 本地路径字典 |
| `aisk_db_query` | 只读 SQL，1 秒 Fail-Fast 离线探针 + 敏感字段脱敏 | `live` / `offline_unavailable` |

### 怎么开（关键：作用域决定要不要审批）

引擎的 MCP 配置**只读**这几个路径（见宿主内置文档 `cli/dist/web-ui/docs/cn/cli/mcp.md`）：

- USER：`~/.codebuddy/.mcp.json` → `~/.codebuddy/mcp.json` → `~/.codebuddy.json`
- PROJECT：`<项目根>/.mcp.json` → `<项目根>/mcp.json`
- LOCAL：`~/.codebuddy.json#/projects/<workspace_path>`

**`~/.workbuddy-ai/mcp.json` 不在这份清单里**——它是桌面壳自己的注册表，引擎不读。
`~/.workbuddy-ai/mcp-approvals.json`（当前 `{}`）是壳的审批记录，与本节的引擎路径是两套。

**项目作用域首次连接需人工审批；user 作用域不需要。** 所以可脚本化的开启方式是：

```bash
codebuddy mcp add aisk --scope user \
  --env PYTHONPATH=$HOME/work/aisk-hub/agent-skills \
  --env AISK_HUB_ROOT=$HOME/work/aisk-hub \
  --env AISK_PRIVATE_ROOT=$HOME/work/aisk-private \
  -- ~/.aisk/venv/bin/python -m engine.mcp_server
```

2026-09-19 已按此开启并实测：`codebuddy mcp list` 显示 `aisk … ✓ Connected`；
一次性会话的 init 事件里 `mcp_servers: [{'name':'aisk','status':'connected'}]`，
工具列表含 6 个 `mcp__aisk__*`。**改动需新开会话才生效**（本会话不受影响）。

注意 user 作用域是**两个 WorkBuddy 入口共用**的（同一个引擎）——这一条会同时给桌面版
加上 aisk 工具。若只想给 AI 端，得走壳的注册表 + UI 信任，那条路不可脚本化。

---

## 四、弹窗与审批契约

高风险动作（推送 / 落地 / 合并主干 / DDL / 生产发布）必须经系统原生弹窗确认，
实现在 `engine/worktree/registry.py`（macOS 走 `/usr/bin/osascript`）。

- **标题格式（代码实测）**：`{动作}-{工具}｜{任务号}`，任务号为空时用「系统」。
  例：`推送-workbuddy-ai｜T001`。
- **别名归一**：`workbuddy_ai` / `workbuddyai` / `WORKBUDDY-AI` 都会归一成 `workbuddy-ai`。
- **fail-closed**：工具身份识别不出来时**不生成弹窗**，直接返回拒绝
  （`registry.py` 的 `except ActionContextError: return False`）。
- **Windows 无原生弹窗**：`IS_WIN` 时跳过 osascript，落到 tty 判断；非 tty 记为
  `denied / no-interactive-tty`。即 Windows 上高风险动作默认被拒，这是**刻意的失败关闭**。
- **`aisk permit` 覆盖两端并真实写入（2026-09-19 重写，此前是 `unsupported`）**：
  `engine/permissions.py` 的 `HANDLERS` 含 `workbuddy` 与 `workbuddy-ai`，两端都落到
  `apply_workbuddy()`，写入 **user 作用域** `~/.codebuddy/settings.json` 的
  `permissions.allow` / `permissions.deny`（规则形如 `Bash(aisk db query *)`）。
  **两端写的是同一份文件**（同一个 codebuddy 引擎、同一个 user 作用域），所以天然幂等；
  `tool` 参数只用于让结果行区分展示，不选落盘路径。本文档上一版写的「两者都返回
  `unsupported`」**已经过时**——权限模型现已一手核实，落点与理由见下。

### WorkBuddy 权限模型（2026-09-19 一手核实）

宿主把官方文档随应用发布，本机原文路径：
`/Applications/WorkBuddy AI.app/Contents/Resources/app.asar.unpacked/cli/dist/web-ui/docs/cn/cli/`
下的 `permissions.md` / `settings.md`（同目录还有 `mcp.md` / `permission-modes.md` /
`hooks-guide.md` / `bash-sandboxing.md` / `cli-reference.md`）。以下逐条读原文（+ 读
`cli/dist/codebuddy.js` 与临时 HOME 实测）核实。

**落盘作用域三层**：

| 作用域 | 路径 | 可信度 |
|---|---|---|
| user | `~/.codebuddy/settings.json` | **永远可信**（不受目录信任影响） |
| project | `<repo>/.codebuddy/settings.json`（进 git） | 目录被显式信任前属**不可信层** |
| project-local | `<repo>/.codebuddy/settings.local.json`（不进 git） | 同上 |

另有进程态层（`--allowedTools` / `--disallowedTools` / session / policy），不落盘。

**权限对象**：`{"permissions": {"allow": [...], "ask": [...], "deny": [...]}}`。

**规则语法**：`Tool` 或 `Tool(specifier)`。Bash 三态——精确 `Bash(npm run build)`、
前缀 `Bash(git:*)`、glob `Bash(npm run *)`（glob 的 `*` **可跨 `/`**）。另有文件类
`Read/Edit/Write(glob)`、`WebFetch(domain:...)`、MCP `mcp__<server>__<tool>`、
`Agent(Explore)`、`Skill(<name>)`（Skill 必须精确匹配、不支持通配）。

**求值链是有序的**（deny 最高优先级）：

```
Hooks/PreToolUse → deny → 可信 allow → 交互态危险命令检查 → ask
→ bypass → 不可信 allow → 模式基线 → 非交互兜底 → dontAsk/auto 收口
```

两条容易踩的语义：复合命令（`&&` `||` `;` `|`）下 allow 要求**所有子命令都命中**才放行；
含重定向（`>` `<` `>>` `<<` `&>`）的命令在 allow 下要求**精确匹配**，通配符不生效。

**受保护路径**（任意模式都额外保护）：`.git`/`.gitconfig`/`.gitmodules`、shell 配置、
包管理器配置、`.vscode`/`.idea`/`.husky`/`.devcontainer`、`.codebuddy`
（除 `.codebuddy/worktrees`）、`.mcp.json`/`.codebuddy.json`。

**为什么适配层写 user 作用域（关键依据）**：项目目录被显式信任前，`<repo>/.codebuddy/settings.json`
的 allow 属**不可信层**，**不能越过危险命令检查**；user 作用域永远在**可信层**，且可越过
交互态危险命令检查。所以 `aisk permit` 必须写 user 作用域——写 project 作用域会让规则降级成
不可信 allow，达不到「只读动词免确认」的目的，还会把内核规则塞进用户的仓库。

**写入与合并语义（临时 HOME 实测，2026-09-19）**：`apply_workbuddy()` 把 `READONLY_VERBS`
包成 `Bash(...)` 并入 `permissions.allow`、`DENY_VERBS` 并入 `permissions.deny`；
**只增不改**——已存在的不重复加，用户自己的条目与无关键（如 `model`）一字不动，
落盘前备份 `settings.json.aisk-bak`。实测：首次新增 17 条（9 allow + 8 deny），
第二次跑返回「白名单已是最新」；`dry_run=True` 只报数不落盘（先 dry 后 real 仍报 17 条，
即 dry 确实没写）。端到端 `aisk permit`（临时 HOME）印证「两端同一份文件」：

```
✅ workbuddy: 只读动词已并入（新增 17 条） → ~/.codebuddy/settings.json
✅ workbuddy-ai: 白名单已是最新 → ~/.codebuddy/settings.json
```

**`trustedDirectories` 的层级（复核的一处文档出入）**：`permissions.md` 的「信任目录」表把
`permissions.trustedDirectories` 列为持久化方式之一，但同篇下一行的公式写的是
`settings.trustedDirectories`，且本机 `~/.codebuddy/settings.json` 里也是**顶层**
`trustedDirectories`。读宿主内置 bundle（`cli/dist/codebuddy.js`）确认：引擎只读顶层
`settingsManager.get("trustedDirectories")`（`saveTrustedDirectories()` 也写顶层），
整个 bundle **没有任何 `permissions.trustedDirectories` 的读取**；
`permissions.additionalDirectories` 倒是真的（`el.permissions?.additionalDirectories`）。
结论：**顶层才是真字段，`permissions.trustedDirectories` 是 `permissions.md` 表格里的笔误**
——不是版本差异，也不是两种写法都支持。适配层不要写 `permissions.trustedDirectories`。

**一处 spec 与代码不一致（已于 2026-09-19 完成对齐并闭环）**：

- **文件**：`spec/adapter-capabilities.yaml`（在**仓库根** `spec/` 下，不在 `agent-skills/`）。
- **字段**：`dialog_protocol.title_format` 与 `dialog_protocol.fallback_no_model`。
- **现值**：均已对齐为 `'{action}-{tool}｜{task_id}'`，与 `agent-skills/engine/action_context.py::ActionContext.title` 保持 100% 吻合（连字符分隔、无 model 字段），漂移已消除。

**为什么当初没被门禁发现**：`tools/verify_spec.py::adapter_checks` 只断言
`actor_count == 7`（外加 spec 与 `actor.py` / `action_context.py` / `permissions.py` 的端集合对齐），
`engine/contracts.py::validate_adapter` 对标题只做「含 `{tool}` 与 `{task_id}`」的形状检查
（第 206 行），**不做语义一致性校验**——它看不出 `｜` 与 `-` 的区别。这类语义漂移
「门禁看不见」，只能靠一手核对；本次即是靠一手比对发现并闭环的。

**`hooks_intercept: true` 已由当前 WorkBuddy AI 构建实测确认（2026-09-19）**：

当前安装的 `/Applications/WorkBuddy AI.app`（`codebuddy.js` 2026-09-05 构建）会执行
`PreToolUse`，并认 Claude 兼容的结构化返回；`permissionDecision: deny` 会在工具执行前阻止调用，
`allow + updatedInput` 会改写工具参数，命令退出码非 0 也会把错误回给 Agent。

| 拦截机制 | 钩子是否执行 | 工具是否被拦住/改写 |
|---|---|---|
| `hookSpecificOutput.permissionDecision=deny` | ✅ | ✅ 工具不执行，返回拒绝理由 |
| `hookSpecificOutput.permissionDecision=allow + updatedInput` | ✅ | ✅ 工具按改写后的参数执行 |
| 命令 `exit 2` + stderr | ✅ | ✅ 工具不执行，返回 hook 错误 |

因此 spec 的 `workbuddy` / `workbuddy-ai` `hooks_intercept: true` 与当前客户端一致。
这是**版本相关能力**：如果升级后 `deny` 又变成只显示消息，先跑第八节的探针，再按实测结果更新
适配文档，不要凭旧版本结论把守卫降级成软约束。

**C 方案的实际边界**：项目级钩子只在任务目录存在；为了覆盖任意目录的原始 `git push`，
`aisk permit workbuddy` / `aisk permit workbuddy-ai` 还会把同一条守卫安装到 user 级
`~/.codebuddy/settings.json`。守卫在任务外只对公开远端或档案 `forbidden_commands` 命中的动作调用
`registry.confirm_human()`；点「推送/确认」才放行，取消、超时、无 GUI 或守卫异常均拒绝。
任务目录内的 `git push` 仍无条件拒绝，不会因点击弹窗而绕过任务交付门禁。

WorkBuddy 桌面版与 AI 版共用该 user 级配置，`--tool auto` 再按
`WORKBUDDY_CONFIG_DIR` 运行期解析；Codex、Antigravity、Claude、Cursor 不进入这条任务外分支。

---

## 五、技能分发

`engine/link.py`：`WORKBUDDY_TOOLS = {"workbuddy", "workbuddy-ai"}` 共用一套端上改写；
`TARGETS["workbuddy-ai"]["root"] = "~/.workbuddy-ai/skills"`，且 `DEFAULT_TOOLS` 含两端。
2026-09-19 实测：内核 `skills/` 26 个 → `~/.workbuddy-ai/skills` 26 个（完整，无缺失）。

---

## 六、项目级钩子的工具归属：已修（`auto` 哨兵 + 运行期解析）

**前置事实（2026-09-19 实测，未变）**：桌面版与 AI 版的引擎**都只读**
`<项目根>/.codebuddy/settings.json`，**都不读** `<项目根>/.workbuddy-ai/settings.json`
（两端分别跑同一组探针，都只命中 `CODEBUDDY-HIT`）。所以 `bind.py` 生成的
`.workbuddy-ai/settings.json` 是**死文件**——它仍会生成（保留兼容），
但不构成「AI 端已有钩子」的证据。

**曾经的缺口**：项目级钩子只能写一个工具名。改造前 `bind.py::codebuddy_settings()` 写死
`workbuddy`，于是 AI 端会话的 `SessionStart` / `Stop` / 守卫事件会**以桌面版名义记账**
（弹窗标题、登记簿的 `owner.tool`）。任务租约本身不会串——`same_actor()` 同时比会话号，
两端会话号不同——但归属标识是错的。

**修法（已实现，2026-09-19）**：不写死工具名，改成**运行期**解析。

1. `engine/worktree/actor.py` 新增哨兵常量与解析函数：

   ```python
   AUTO_TOOL = "auto"

   def resolve_tool(name, fallback="workbuddy", env=None):
       if name != AUTO_TOOL:
           return name                  # 其余取值原样透传
       return detect_tool(env) or fallback
   ```

2. `bind.py` 的两个 WorkBuddy 设置生成函数（`codebuddy_settings()` /
   `workbuddy_ai_settings()`）改传 `actor.AUTO_TOOL`，生成物里是 `--tool auto`。
3. `guards.py::main()` 与 `hooks.py::main()` 首行加 `tool = actor.resolve_tool(tool)`，
   用当前钩子进程的 `env` 解析出真实端。钩子进程带着 `WORKBUDDY_CONFIG_DIR`，
   所以 `detect_tool()` 能正确返回 `workbuddy` 或 `workbuddy-ai`。
4. 解析不出端时回落 `workbuddy`（桌面版）——即**最坏情况等同于改造前的写死行为**，不回退。

**边界（刻意如此）**：`resolve_tool()` 对非 `auto` 取值**原样返回**，所以
`codex` / `antigravity` / `claude` / `cursor` 的生成物与运行期行为**完全不变**。
验证：生成物中 codex 仍是 `--tool codex`、antigravity 无工具令牌；协议验收 93 项通过。

**不受影响**：命令行路径本来就是对的。会话里直接跑 `aisk task …` 时 `detect_tool()`
正确返回 `workbuddy-ai`（`tasks.py` 的
`tool = getattr(args, "tool", None) or actor.detect_tool()`）。

**守护测试**：`tests/test_worktree.py::GuardsAndHooks::test_workbuddy_hook_tool_is_resolved_at_runtime`
同时钉住三条——两端各自解析正确、解析不出时回落桌面版、其余端原样透传。

---

## 七、关于 `AISK_TOOL`（R1.2 的前提不成立）

R1.2 建议「`write_env` 写入 `AISK_TOOL`」。实测**不需要**：宿主不注入任何 `AISK_*`，
但 `detect_tool()` 靠 `WORKBUDDY_CONFIG_DIR` 就能认出端，`session_ids()` 靠
`CODEBUDDY_SESSION_ID` 就能拿到会话号（两者在钩子进程与 Bash 子进程里都在）。

而且把工具名烘进任务级 env 文件是**错的**——同一个任务目录在 mac 上可能被 claude 打开，
写死的 `AISK_TOOL=workbuddy` 会让归属错乱。要写也只能按端分文件，属共享生成器的改动。

---

## 八、探针与复验命令

### 1. 钩子能力 + 钩子进程环境（不需要能登录）

会话因 `Authentication required` 失败时钩子**照样触发**。把钩子命令当探针用：

```bash
APP="/Applications/WorkBuddy AI.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"
D=/tmp/wbprobe; rm -rf $D && mkdir -p $D/.codebuddy $D/.workbuddy-ai
printf '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"env > %s/env.txt"}]}]}}' $D > $D/.codebuddy/settings.json
printf '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo AI-ONLY >> %s/env.txt"}]}]}}' $D > $D/.workbuddy-ai/settings.json
rm -rf $D-home $D-cfg && mkdir -p $D-home $D-cfg
(cd $D && HOME=$D-home WORKBUDDY_CONFIG_DIR=$D-cfg "$APP" -p ok >/dev/null 2>&1)
grep -E '^(WORKBUDDY|CODEBUDDY_APP|CODEBUDDY_SESSION_ID|CLAUDE_SESSION_ID|AISK_)' $D/env.txt
```

判读：`env.txt` 里有 `WORKBUDDY_CONFIG_DIR`（且无 `AI-ONLY`）→ 读的是
`.codebuddy/settings.json`。把 `SessionStart` 换成 `SessionEnd` / `Stop` 可逐个事件验。
桌面版把 `APP` 换成 `/Applications/WorkBuddy.app/.../cli/bin/codebuddy`。

逐个事件批量验（第二节 `SessionEnd` 复验用的就是这段）：

```bash
APP="/Applications/WorkBuddy AI.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"
for EV in SessionStart Stop SessionEnd; do
  D=/tmp/wbprobe-$EV; rm -rf $D $D-home $D-cfg; mkdir -p $D/.codebuddy $D-home $D-cfg
  printf '{"hooks":{"%s":[{"hooks":[{"type":"command","command":"echo %s-HIT >> %s/hits.txt"}]}]}}' \
    "$EV" "$EV" "$D" > $D/.codebuddy/settings.json
  (cd $D && HOME=$D-home WORKBUDDY_CONFIG_DIR=$D-cfg "$APP" -p ok >/dev/null 2>&1)
  [ -f $D/hits.txt ] && echo "$EV: 触发" || echo "$EV: 未触发"
done
```

### 2. PreToolUse 拦截探针（当前构建）

在一个不含项目级 `.codebuddy/` 的临时目录运行，先把用户级 `PreToolUse` 临时写成返回
`permissionDecision: deny` 的命令，再调用一次性会话；预期输出中出现 `Error: PROBE-DENY`
且没有工具的实际 stdout。完成后务必删除探针条目，避免把用户配置留在测试状态：

```bash
D=/tmp/wb-intercept-probe; mkdir -p "$D"
# hook 命令必须输出 JSON；这里的拒绝理由只用于判定，不是生产配置。
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"PROBE-DENY"}}' > "$D/deny.json"
# 用 jq/python 把 deny.json 的内容挂到 ~/.codebuddy/settings.json 的 hooks.PreToolUse，
# 然后执行：
(cd "$D" && "$APP" -p 'Run exactly: echo SHOULD-NOT-RUN' --model fast-model --output-format json)
# 预期：工具不执行；结束后恢复 ~/.codebuddy/settings.json 备份。
```

**不同版本判读**：若仍出现 `SHOULD-NOT-RUN`，当前客户端没有可用的硬拦截，必须停止
发布 C 方案并先更新适配层；不能把「hook 触发」误报为「动作已阻止」。

### 3. MCP 工具面

```bash
# 服务端本身（不经宿主）
cd "$AISK_HUB_ROOT/agent-skills"
printf '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n' | PYTHONPATH=. ~/.aisk/venv/bin/python -m engine.mcp_server | jq -r '.result.tools[].name'

# 引擎视角（含健康检查）
codebuddy mcp list
codebuddy mcp get aisk

# 会话视角：确认工具真的进了工具表
codebuddy -p ok --output-format stream-json 2>/dev/null | head -1 | jq '{mcp_servers, mcp_tools: [.tools[] | select(startswith("mcp__"))]}'
```

### 4. 弹窗标题契约（不弹 UI，只算字符串）

```bash
cd "$AISK_HUB_ROOT/agent-skills" && python3 -S -c "
import sys; sys.path.insert(0,'.')
from engine.action_context import ActionContext
print(ActionContext(tool='workbuddy-ai', action='推送', task_id='T001').title)"
```
