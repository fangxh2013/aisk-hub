# Claude 适配规范

工具名：`claude`。为 Claude Code CLI 及 Claude 宿主提供生命周期钩子（Hooks）、多 AI 协同记账、租约流转与 macOS 原生弹窗契约。

---

## 一、工具识别与会话模型

1. **进门判别**：
   在 `engine/worktree/actor.py` 中，`detect_tool()` 优先判断 Claude 特征环境变量：
   ```python
   if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
       return "claude"
   ```
2. **会话标识获取**：
   - 环境变量 `CLAUDE_CODE_SESSION_ID`（Claude Code 官方注入的会话唯一标识）
   - 环境变量 `CLAUDE_SESSION_ID`
   - CLI 参数 `--session`
   内核将其映射为 `owner_session`，用于区分同一机器上不同并发 Claude 终端。
3. **协同租约流转**：
   认领任务时分配单调递增的 `fencing_token`。任务执行中通过 Hook 事件或任务命令自动续订租约 TTL。

---

## 二、Claude Code 项目级钩子机制（Hooks）

Claude Code 拥有完整的原生生命周期钩子机制，由 `aisk task bind` 自动生成于项目根目录：
`<项目根>/.claude/settings.json`

### 1. 钩子事件映射与行为

| 事件 | 触发时机 | 内核动作 |
| :--- | :--- | :--- |
| **`SessionStart`** | 会话启动时 | 自动校验任务状态；可认领时自动绑定当前会话并注入项目工作区约束 |
| **`Stop`** | 工具执行或命令响应完毕 | 自动为本会话持有的任务续租心跳（更新 `lease_until`） |
| **`PreToolUse`** | 准备执行 Bash 工具前 | 守卫检查：拦截针对主干分支的直接合并/推送，强制要求弹窗确认 |
| **`SessionEnd`** | 会话正常关闭时 | 触发自动快照（WIP Snapshot）并释放活跃租约，方便其他 AI 认领接力 |

---

## 三、人工确认与系统原生弹窗规范（macOS AppleScript / Windows WinForms）

在 Claude 中，凡涉及**合并主干（dev/master/main）、推送远端、落地代码（land/promote）、执行 DDL/SQL 迁移或生产发布等关键/高危操作**，必须通过当前操作系统的原生弹窗向用户请求确认：macOS 使用 `/usr/bin/osascript`，Windows 使用当前交互桌面的 PowerShell WinForms；严禁静默执行或仅在聊天会话中假定已授权。

### 1. 标题协议与调用范式
- **标题标准**：`动作-claude｜<任务号>`（如 `git合并-claude｜T042`、`落地代码-claude｜T001`）
- **标准命令格式**：
  ```bash
  /usr/bin/osascript -e 'display dialog "<操作说明与影响范围>\n\n确认请点「<操作动作>」" with title "<操作动作>-claude｜<任务号>" buttons {"取消", "<操作动作>"} default button "<操作动作>" giving up after 600'
  ```
- **判定准则**：
  只有捕获到 `button returned:<操作动作>` 且 `gave up:false` 时才判定为用户真实授权并继续执行；超时或取消则立即终止保持原状。

---

## 四、技能发现与安装路径

Claude Code 会自动发现并加载以下目录中的技能：
- 项目级技能：`<项目根>/.claude/skills/`
- 全局用户级技能：`~/.claude/skills/`

执行 `./bin/aisk link --target claude` 会将内核规范技能以无符号链接方式建立适配索引，支持通过逻辑别名（`skill_router.py`）自动将旧命令重定向至 canonical 领域技能。

---

## 五、复现与探针命令

### 1. 识别与环境探针
验证当前进程环境变量是否被内核识别为 `claude`：

```bash
CLAUDECODE=1 CLAUDE_CODE_SESSION_ID="test-sess-claude" PYTHONPATH=agent-skills ~/.aisk/venv/bin/python -c "
from engine.worktree import actor
print('Detected tool:', actor.detect_tool())
assert actor.detect_tool() == 'claude'
print('✅ Claude tool detection verified')
"
```

### 2. 原生弹窗协议探针
验证弹窗标题是否严格包含 `-claude |`：

```bash
/usr/bin/osascript -e 'display dialog "这是一次 Claude 弹窗协议连通性测试。\n\n请点击「确认测试」完成验收。" with title "探针测试-claude | T999" buttons {"取消", "确认测试"} default button "确认测试" giving up after 10'
```
