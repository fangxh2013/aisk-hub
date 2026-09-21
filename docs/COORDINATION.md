# 多 AI 协同开发

协同的最小协议不是让工具互相调用，而是让它们共享可验证的任务状态：

1. `find`：按关键词查找已有任务，避免重复派活。
2. `claim`：以工具名和会话号原子认领。
3. 隔离工作区：一个任务、一个分支、一个变更责任人。
4. `note`：记录完成项、下一步和阻塞原因。
5. `check → ready → land → promote`：每个仓库独立门禁，合并和推送由带工具归属的弹窗确认。
6. `events.jsonl` 和 `dialog_audit.jsonl` 只记录元数据，不记录业务内容和凭据。

### fxh 脏工作区与任务 worktree 的边界

任务分支是独立的 Git worktree。`fxh` 主工作区存在未提交改动时，不应阻断任务分支的本地
提交、构建检查、`ready` 或 Windows → hub 的中央交换发布。AI 在任务目录内使用：

```text
aisk task commit Txxx --repos be --path src/... -m "feat: ..."
aisk task commit Txxx --all -m "fix: ..."
aisk task check Txxx
aisk task ready Txxx
```

上述提交命令只作用于任务 worktree，不检查、暂存、提交或清理 `fxh`，也不推送公网。任务
分支的远端发布由中央交换流程完成，并且必须弹出标题包含 `git推送-<工具>` 的确认框。

只有真正要把提交写入 `fxh` 文件树的 `land` 才要求对应 `fxh` 工作区干净；已经 land 后的
`promote` 只读取 `fxh` 提交引用、使用独立门禁/主干锚点，因此允许 `fxh` 保留用户未提交
改动，但绝不替用户覆盖这些改动。不同仓库使用 `--repos` 独立交付，互不连坐。

### Windows 中央交换

Windows 端不能直接 `git push`，也不能依赖无 TTY 的终端输入。`aisk task ready` 会对
配置的本地/UNC `hub` 做预检，弹出 WinForms 原生确认框，标题格式为
`git推送-<工具> | <任务号> | <仓库>`；确认后只做非强制推送，并用 `ls-remote` 回读提交号。
拒绝、超时、远端分叉或回读不一致都 fail-closed，且写入 `.partial.json` 供重试，不会
覆盖同名分支。

如需接入外部编排器，可在 adapter 中实现 MCP、文件队列或 CI 事件桥；桥接层只能提交任务事件，不能绕过内核状态机。

## 本地状态存储边界

同一台机器上的并发任务状态由 SQLite `state.db` 作为唯一事实源：状态变更、CAS 版本和
`outbox_events` 在同一事务内提交；JSONL 只作为可重试的审计导出。每次接管递增
`fencing_token`，旧会话即使从休眠中恢复也不能继续写入。

Mac 与 Windows 不共享同一个 SQLite 文件。跨机器只交换带摘要和签名的 handoff 事件包，
导入端重新执行状态机校验；网络盘挂载 SQLite 被明确禁止。

契约回归可离线执行；报告必须区分三种证据：

```text
PYTHONPATH=agent-skills python3 tools/verify_spec.py --no-report-files
./bin/aisk contract verify
```

报告中的 `offline_contract_score` 与 `is_99_plus_certified` 只表示公共离线契约通过；
`real_file_score` 需要私有 manifest 与外置 `tools/overlay.lock.json`；`real_runtime_score` 只有
真实客户端或外部系统联调后才有值。未联调必须是 `runtime_status: offline-only`，不能用样例、
关键词路由或 mock broker 冒充真实运行。

旧技能迁移固定为 9 个 canonical、26 个 legacy alias，另保留 2 个历史兼容别名。删除 alias 前
必须确认引用为零、保存回滚证据并经过回滚窗口；回滚先恢复 manifest/lock 和 alias 映射，再重新
执行契约校验。
