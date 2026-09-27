# 多 AI 协同开发

协同的最小协议不是让工具互相调用，而是让它们共享可验证的任务状态：

1. `find`：按关键词查找已有任务，避免重复派活。
2. `claim`：以工具名和会话号原子认领。
3. 隔离工作区：一个任务、一个分支、一个变更责任人。
4. `note`：记录完成项、下一步和阻塞原因。
5. `check → ready → land → promote`：每个仓库独立门禁；个人 `fxh/fxh-dev` 推送默认放行，
   合入或推送 `dev/main/master` 由带工具归属的弹窗确认。
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
个人集成分支的远端发布可由日常开发直接完成；受保护分支的远端发布必须弹出标题包含
`git推送-<工具>` 的确认框。`git merge dev` 必须弹出 `git合并-<工具>` 确认框。

只有真正要把提交写入 `fxh` 文件树的 `land` 才要求对应 `fxh` 工作区干净；已经 land 后的
`promote` 只读取 `fxh` 提交引用、使用独立门禁/主干锚点，因此允许 `fxh` 保留用户未提交
改动，但绝不替用户覆盖这些改动。不同仓库使用 `--repos` 独立交付，互不连坐。

### Windows 中央交换

Windows 任务目录内不能直接 `git push`，也不能依赖无 TTY 的终端输入。`aisk task ready` 会对
配置的本地/UNC `hub` 做预检，弹出 Windows 原生 TaskDialog 确认框，标题格式为
`git推送-<工具> | <任务号> | <仓库>`；确认后只做非强制推送，并用 `ls-remote` 回读提交号。
拒绝、超时、远端分叉或回读不一致都 fail-closed，且写入 `.partial.json` 供重试，不会
覆盖同名分支。

如需接入外部编排器，可在 adapter 中实现 MCP、文件队列或 CI 事件桥；桥接层只能提交任务事件，不能绕过内核状态机。

## 本地状态存储边界

`aisk task` 的事实源是每个档案数据根下的登记簿 `<data_root>/state/tasks/<任务号>.json`：
每次写入都是「同目录临时文件 + fsync + 原子替换」，需要互斥的操作用跨进程文件锁串行
（登记簿锁、单任务锁、每个仓库的落地锁）；`state/events.jsonl` 只追加事件元数据，供审计。

认领租约记在任务记录的 `owner` 上：工具名、会话号与心跳时间。工具和会话号都一致才算同一
执行者；持有中的任务拒绝其他会话写入，空闲（默认 30 分钟无活动）后需 `--takeover --reason`
接手，超过可接手时限（默认 120 分钟）才能直接认领。活动时间取心跳、任务分支新提交、未提交
文件修改时间与 `PROGRESS.md` 修改时间中的最大值。这里没有单调递增的 fencing token：被接手的
旧会话醒来后，它的写命令和钩子守卫会因执行者不匹配被拒绝，但已经在进行中的写入不会被追溯拦截。

Mac 与 Windows 各用本机登记簿，不共享同一份文件。跨机器交接走中央交换 hub：Windows 的
`ready` 把任务分支非强制推到 hub 裸仓并用 `ls-remote` 回读核对，同时把任务记录写到
`hub/state/win/`；Mac 集成端读取这些记录、从 hub 取分支后落地。档案开启 `share_activity`
时，两端还能看到对方进行中任务的只读摘要。数据根不要放在网络盘上（`aisk task doctor` 会告警）。

`spec/state-machine.yaml` 描述的 SQLite 状态机（CAS 版本、fencing token、事务 outbox）是协同
契约的参考实现 `agent-skills/engine/coordination_store.py`，由 `tools/verify_spec.py` 离线验证，
尚未接入 `aisk task` 运行时。MCP 工具 `aisk_task_inspect` 读取的是上面的登记簿；只有显式设置
`AISK_STATE_DB` 时才读取参考实现的 SQLite 文件。

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
