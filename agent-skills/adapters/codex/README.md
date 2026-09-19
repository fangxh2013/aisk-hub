# Codex 适配规范

工具名：`codex`。为 OpenAI Codex CLI、IDE 扩展及自动化 Agent 提供多 AI 协同记账、会话隔离与安全弹窗协议。

---

## 一、工具识别与会话模型

1. **进门判别**：
   在 `engine/worktree/actor.py` 中，`detect_tool()` 检查环境变量：
   ```python
   if any(k.startswith("CODEX_") for k in env):
       return "codex"
   ```
   只要环境中注入了任何 `CODEX_*` 前缀变量，内核均识别为 Codex。
2. **会话标识**：
   会话标识获取优先级：
   - 环境变量 `CODEX_THREAD_ID`（Codex CLI 默认注入的线程/会话 ID）
   - 环境变量 `CODEX_SESSION_ID`
   - CLI 参数 `--session` 显式指定
   内核将其作为 `owner_session` 记入 SQLite 协同状态机，实现任务独占与心跳续租。
3. **租约与 Fencing Token 防御**：
   Codex 认领任务（`aisk task claim`）时，状态机为其签发严格自增的 `fencing_token`。如果会话因长耗时推理或休眠导致租约超时被其他 AI 接管，Codex 唤醒后的写入将被 `StaleFencingTokenException` 阻断。

---

## 二、人工确认与系统原生弹窗规范（macOS AppleScript）

在 Codex 中，凡涉及**合并主干（dev/master/main）、推送远端、落地代码（land/promote）、执行 DDL 迁移或生产发布等关键/高危操作**，必须通过系统原生弹窗（`/usr/bin/osascript`）向用户请求确认，严禁静默执行或假定已授权。

### 1. 标题协议与调用范式
- **标题标准**：`动作-codex｜<任务号>`（如 `git提交-codex｜T042`、`落地代码-codex｜T003`）
- **标准命令格式**：
  ```bash
  /usr/bin/osascript -e 'display dialog "<操作说明与影响范围>\n\n确认请点「<操作动作>」" with title "<操作动作>-codex｜<任务号>" buttons {"取消", "<操作动作>"} default button "<操作动作>" giving up after 600'
  ```
- **判定准则**：
  只有捕获到 `button returned:<操作动作>` 且 `gave up:false` 时才判定为用户授权继续执行；超时或取消则立即终止。

---

## 三、技能与指令接入方式

1. **项目级指令注入**：
   Codex 在打开工作区时读取项目根目录或 `.codex/instructions.md`。`aisk link` 会在工作区生成适配层索引，引导 Codex 遵循：
   - 先读取当前工作区 `AGENT-WORKTREE.md`；
   - 遵守 `AGENTS.md` 规范；
   - 动态路由领域技能（如调用 `dev-code` 自动映射至 `backend-engineering`）。
2. **MCP 工具接入**：
   Codex 支持通过 MCP 配置文件直连 `aisk-mcp-server`，直接调用 `aisk_task_inspect` 获悉当前协同状态。

---

## 四、能力边界与降级策略

| 能力项 | 支持状态 | 说明 |
| :--- | :---: | :--- |
| **会话自动续租** | ✅ 支持 | 会话活动或执行任务命令时自动更新心跳 |
| **工作区隔离** | ✅ 支持 | 基于 `git worktree` 运行于 `~/.aisk-runtime/hub/tasks/tasks/` |
| **原生弹窗授权** | ✅ 支持 | 经由 `/usr/bin/osascript` 系统调用弹出 |
| **PreToolUse 拦截** | ⚠️ 弱依赖 | 部分 CLI 壳缺乏严密钩子，主要由工作区文件钩子与内核校验守护 |
| **主干直接推送** | ❌ 严禁 | 必须走 `aisk task check -> ready -> land -> promote` |

---

## 五、复现与探针命令

### 1. 识别探针
验证当前环境是否被内核准确识别为 `codex`：

```bash
CODEX_SESSION_ID="test-sess-codex" PYTHONPATH=agent-skills ~/.aisk/venv/bin/python -c "
from engine.worktree import actor
print('Detected tool:', actor.detect_tool())
assert actor.detect_tool() == 'codex'
print('✅ Codex tool detection verified')
"
```

### 2. 原生弹窗标题协议探针
验证标题是否正确包含 `-codex |`：

```bash
/usr/bin/osascript -e 'display dialog "这是一次 Codex 弹窗协议连通性测试。\n\n请点击「确认测试」完成验收。" with title "探针测试-codex | T999" buttons {"取消", "确认测试"} default button "确认测试" giving up after 10'
```
