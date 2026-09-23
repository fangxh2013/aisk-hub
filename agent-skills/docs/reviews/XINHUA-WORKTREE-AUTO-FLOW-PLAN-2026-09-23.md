# 新华前后端 worktree 自动落地、推送与回收方案

> 状态：设计提案，尚未实现。本文描述目标行为，不是当前运行规则。
> 日期：2026-09-23
> 目标：前后端任务完成后可靠地进入本地 fxh、自动发布到 fxh-dev 并回收任务 worktree；文档与 aisk 仓库不使用 worktree。

## 1. 决策摘要

本方案将“任务完成”定义为一条可恢复的交付流水线，而不是模型说出“完成”后立即删除目录。成功时不需要人工确认；失败时保留足够的提交、引用和任务记录供自动重试或人工处理。

### 1.1 仓库策略

| 仓库类别 | 隔离方式 | 完成后的自动行为 | 明确禁止或保留给用户的动作 |
|---|---|---|---|
| 新华后端 xinhua-platform | 每个后端任务使用自己的 worktree；不与前端共用目录或任务分支 | 任务分支提交 → 门禁 → 快进合入本地 fxh → 自动归档并删除任务 worktree → 静默推送 fxh 到 origin/fxh-dev | fxh 合入本地 dev 只能由用户明确下令，并经 aisk task 弹窗确认；推送 dev 也必须由用户明确下令并经弹窗确认 |
| 新华前端 xinhua-platform-web | 每个前端任务使用自己的 worktree；与后端独立 | 同后端，目标分支为本仓库自己的本地 fxh，推送目标为本仓库自己的 origin/fxh-dev | 同后端 |
| 新华文档 xinhua-platform-docs | 普通检出，不创建任务 worktree 或锚点 | 在本仓库本地 fxh 上提交文档改动 | 本方案不授权自动推送文档仓库 |
| aisk-hub、aisk-private | 普通检出，不创建任务 worktree 或锚点 | 只提交本次任务改动到各自本地 master，随后自动推送各自的 origin/master | 这是针对这两个仓库的明确例外；不能推广到其他仓库的 master |
| 所有仓库的 main | 不适用 | 无 | AI 在任何情况下不得合并、提交、改写、推送、删除或以其他方式修改 main；没有用户命令或弹窗可以解除此限制 |

fxh、fxh-dev、dev 和 master 都按**仓库独立配置**解释。禁止把一个仓库的分支名、提交或推送状态套用到另一个仓库。

### 1.2 对“100%提交至 fxh”的精确定义

网络故障、门禁失败或分支冲突时，系统无法保证一次操作必然成功。这里的“100%”必须定义成**不丢任务、不虚报成功**：

1. 只有目标任务提交的完整 SHA 已经是本仓库本地 fxh 的祖先，任务才能标记为“本地已落地”。
2. 如果提交、门禁、冲突检查或快进失败，任务不得标记完成，也不得删除唯一工作副本。
3. 如果 fxh-dev 推送失败，已落地到 fxh 的提交和任务归档仍保留；任务进入“个人分支待推送”，由后台或后续命令幂等重试。
4. “已发布”只在 origin/fxh-dev 确认包含目标 SHA 后成立。禁止用强推、重置或改写历史把失败伪装成成功。

因此，系统承诺的是**成功才完成、失败可恢复、重试不重复提交**，不是在网络和远端规则失效时仍声称 100% 成功。

## 2. 当前基线与差距

以下事实来自本机只读检查和当前 aisk-hub 源码；本提案没有改动这些运行设置。

1. 后端和前端目前各登记 3 个工作区：主检出、dev 锚点、门禁锚点，共 6 个固定工作区；这不是 6 个任务 worktree。
2. 新华档案当前以 fxh 为集成分支，后端和前端的个人推送分支均为 fxh-dev，promote 当前配置为 ff-trunk，confirm_land 当前为 true。
3. 当前 quota_active=8 检查的是进行中任务数，不是 Git worktree 数。create_task 会对任务选择的每个仓库分别创建一个 worktree；一个前后端多仓任务最多产生两个任务 worktree。
4. pause 会保存未提交改动快照并把任务置为暂停，但保留原工作树。因此暂停会释放当前活跃任务配额，却不会释放磁盘上的 worktree。
5. 现有 land 已有来源审计、候选提交门禁、并发锁、落地前再次核对分支头，并以 --ff-only 合入本地集成分支；当前确认策略仍会要求人工点击。
6. 当前 promote 对 ff-trunk 仓库不仅会推送 fxh-dev，还会继续处理本地 dev 与远端 dev。它不能直接作为“只静默推送 fxh-dev”的后台动作；必须先将个人分支发布与 dev 操作拆成互不调用的接口。
7. 当前 archive 会把任务分支保存到 refs/aisk/archive/...，复制任务记录并移除 worktree；它是现有安全回收基础。自动流程需要在落地提交可恢复之后调用专用归档路径，不能把通用 --force 当作删除安全证明。
8. 本机后端和前端的 fxh 当前工作区干净，各自领先 origin/fxh-dev 两个提交。这只说明当前基线适合本地试运行，不构成未来自动推送的绕过条件。

### 2.1 成熟工具可借鉴的做法

1. **Codex 按数量回收托管 worktree**：默认保留最近 15 个托管 worktree；归档聊天或达到数量上限时可清理较旧工作树，但会保护进行中、固定和永久工作树。删除前先保存快照，之后可以恢复。适合借鉴的是“硬上限 + 保护条件 + 可恢复清理”组合，不是照抄 15 这个数。[Codex Worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)
2. **GitHub Copilot 按任务提供单仓库临时环境**：一个云端 agent 任务只改任务指定的仓库和分支，并运行在该任务自己的临时开发环境中；会话可以停止和归档。它与前后端拆成独立仓库任务的边界相似，说明隔离单位应是仓库内的独立交付任务，而不是把一个多仓任务自动扩展成成倍的 worktree。[Copilot cloud agent](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent) [管理 agent 会话](https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/manage-and-track-agents)
3. **Git worktree 本身不管理配额**：链接工作树共享仓库对象库，但各自有独立检出状态和工作文件；工作树移除与陈旧登记清理是两个操作。数量上限、重要任务保护和快照恢复必须由上层任务管理器实现。[Git worktree](https://git-scm.com/docs/git-worktree)

因此，本方案采用本机可承受的 4 个并发任务 worktree、8 个物理任务 worktree 上限，并额外保证暂停任务不会因为“释放活跃配额”而逃出物理计数。

关键实现位置：

- agent-skills/engine/worktree/tasks.py：check_quota、create_task、cmd_pause、cmd_archive
- agent-skills/engine/worktree/integrate.py：cmd_land、cmd_promote、个人分支确认判定
- agent-skills/WORKTREE.md、agent-skills/DECISIONS.md、README.md：当前通用工作流与确认规则
- ~/.aisk/profiles/xinhua.yaml：本机新华仓库与分支档案；凭据和本机路径不应复制到方案或公共模板

本提案需要把当前全局确认设置改为**精确到仓库和动作的策略**。不能简单地关闭全局 confirm_land，否则会误放行文档仓库或其他项目的落地操作。

## 3. 任务完成流水线

### 3.1 前后端代码任务

每个任务只绑定一个代码仓库：后端任务只绑定 be，前端任务只绑定 web。跨前后端需求拆成两个关联任务；关联 ID 或需求单号相同，但各自拥有独立分支、worktree、门禁和落地状态。

~~~mermaid
stateDiagram-v2
    [*] --> ActiveWorktree
    ActiveWorktree --> TaskCommit: 完成信号 + 明确改动清单
    TaskCommit --> Gate: 提交任务分支
    Gate --> Ready: 检查通过并记录 ready_sha
    Gate --> Blocked: 检查失败
    Ready --> Landing: 计划校验 + 精确提交门禁
    Landing --> LandedFxh: fxh 快进且包含 ready_sha
    Landing --> Blocked: 冲突/基线变化/门禁失败
    LandedFxh --> Archived: 保存归档引用后删除 worktree
    Archived --> PublishPending: 自动推送被拒绝或暂时失败
    Archived --> Published: fxh-dev 确认包含目标 SHA
    PublishPending --> Published: 幂等重试成功
~~~

流程要求：

1. 只有任务执行器明确发出“交付就绪”事件后才启动完成流水线。会话结束、模型暂停、进程退出或空闲超时都不等于任务完成。
2. 在任务 worktree 内检查基线差异、暂存范围、未跟踪文件和敏感路径；只提交本任务改动。禁止把主检出里已有的用户改动、其他任务文件或凭据带入提交。
3. 自动生成符合仓库规范的提交说明，在**任务分支**提交；不得直接在 worktree 中检出或改写 fxh。
4. 对这个确切提交运行仓库门禁，记录 ready_sha、基线 SHA、门禁版本和日志。任何门禁失败都留在任务分支，不得进入自动落地阶段。
5. 在本仓库集成锁下生成落地计划；校验任务来源、分支祖先、文件重叠、敏感路径、当前 fxh 头和工作区状态。对候选提交运行门禁。
6. 使用 compare-and-swap 语义保护 fxh：只有 fxh 仍处于计划记录的 head，且候选可快进时才落地；分支在检查期间前进就重新计划，不强行覆盖。
7. 合入后再次验证 fxh 包含 ready_sha，先写入可恢复的落地与归档记录，再移除任务 worktree 和任务分支。worktree 清理失败时保持 cleanup_pending 并重试，不回滚已正确落地的 fxh。
8. 仅将本地 fxh 推至本仓库的 origin/fxh-dev，使用普通非强制推送。收到远端拒绝、网络超时或权限错误时记录 push_pending；禁止换 token、绕过保护或强推。
9. 用任务 ID、仓库键、ready_sha 和远端 SHA 更新登记簿。重复收到完成事件时必须识别已完成阶段，不得产生重复提交或把另一任务的提交登记在本任务名下。

静默表示**无确认弹窗**，不表示隐藏失败。成功时保留可查询的审计记录；失败时在看板和当前执行会话中明确显示阻塞阶段与安全恢复方式。

### 3.2 worktree 配额与回收策略

建议作为首轮配置的上限：

| 指标 | 初始上限 | 计数方式 |
|---|---:|---|
| 同时活跃的任务 worktree | 4 个总计，后端最多 2 个、前端最多 2 个 | 正在执行、检查或落地的任务 |
| 实际存在的任务 worktree | 8 个总计 | 活跃、暂停、清理待重试等所有已有目录都计入 |
| 并发构建 | 保持当前 2 个 | 由构建信号量单独限制 |

按目前后端和前端各 3 个固定检出计算，8 个任务 worktree 的硬上限对应最多约 14 个相关 Git 工作区。这个数字是本机首轮控制值，不是行业标准；上线后按每仓的工作树、依赖和构建产物实际占用调整。

需要补齐以下机制：

- 配额按**仓库工作树槽位**计算，不能继续只数任务记录；暂停任务和 cleanup_pending 也必须占物理槽位。
- 当活跃额度已满时，任务可作为不含 worktree 的排队记录存在；只在有槽位且认领时物化。若实现排队过于复杂，就拒绝创建并给出当前占用，而不是先创建再超限。
- 暂停任务默认保留现场并继续计入物理上限。可增加安全的压缩暂停：先验证提交和 WIP 引用完整、保存需要的忽略文件清单，再移除 worktree；不支持无损快照的目录只能保留并占用槽位。
- 任务归档前验证 fxh 或任务归档引用可达；已完成、失败、暂停三类任务使用不同策略，不按年龄无差别删除。
- 共享 Maven/npm 下载缓存可以减少重复下载；不要让多个任务共享可写的 node_modules、target 或应用运行目录来代替隔离。
- 看板展示活跃槽位、暂停占用、清理待重试、每仓磁盘占用和最近失败原因。达到磁盘预算时先拒绝新工作树或清理已确认可归档任务，不触碰活跃任务。

## 4. fxh、fxh-dev 与 dev 的权限边界

### 4.1 自动允许的动作：只作用于后端和前端

本仓库白名单中的完成流水线可以无弹窗地：

- 在任务分支提交本任务改动；
- 通过门禁后以快进方式更新本地 fxh；
- 将本地 fxh 普通推送到 origin/fxh-dev；
- 在提交已于本地 fxh 验证可达且归档引用已写入后回收任务 worktree。

不得把无确认权扩展为所有 git push、所有 land、所有仓库或所有分支。每项授权必须同时匹配仓库、动作和目标分支。

### 4.2 只由用户命令触发的 dev 操作

任务自动完成流水线不调用任何 dev 合并或 dev 推送代码。只有用户明确下令后，才允许按两步处理：

1. 用户明确要求把指定仓库的本地 fxh 合入本地 dev。命令显示两侧 SHA、提交列表、文件范围和门禁结果，再通过 aisk task 弹窗确认。默认只接受 --ff-only；不能快进或存在冲突时停止，不自动 rebase、不选 ours/theirs。
2. 只有在用户再次明确要求推送指定仓库 dev 后，显示远端目标与待推送提交，再通过 aisk task 弹窗确认。确认失败、超时或远端拒绝都不写远端。

可以设计单独的用户入口，例如“合入本地 dev”和“推送 dev”；不能复用当前会同时推送 fxh-dev、推进本地 dev 并尝试推送远端 dev 的复合 promote 流程。

### 4.3 main 永久硬阻断

main 在所有仓库中都是 AI 永久不可写目标。守卫必须阻断直接 Git 命令、封装命令、脚本、别名、钩子、任务引擎和自动恢复流程对 main 的修改，包括 merge、commit、cherry-pick、rebase、reset、update-ref、branch 强制移动、删除和 push。

- 对 main 的只读检查可以继续。
- 不提供 --force、环境变量、项目配置、人工弹窗或用户命令来解除 AI 写入封锁。
- 远端分支保护是第二道防线；不能用凭据、强推或绕过服务端规则突破它。

### 4.4 aisk 的 master 精确例外

用户另行明确授权 aisk-hub/master 和 aisk-private/master 在无 worktree 模式下自动提交并推送。实现时将其建模为**精确仓库 allowlist**，不能把这项权限泛化为所有仓库的 master，也不能允许这些仓库的 main。

如果远端 master 有保护规则并拒绝此次普通推送，系统保留本地提交并报告待处理；不修改保护规则、不强推、不改写远端历史。

## 5. 不使用 worktree 的仓库

### 5.1 新华文档仓库

- 始终在既有普通检出上工作，完成后只把本任务明确修改的文档提交到本地 fxh。
- 本方案不自动 push 文档仓库；如将来需要推送，必须另行明确授权并定义目标。
- 文档主检出不干净、处于 detached HEAD、目标不是 fxh 或出现其他写入者时，自动提交停止，不自动切分支、不覆盖现有改动。

### 5.2 aisk-hub 与 aisk-private

- 不创建任务 worktree，也不为它们保留开发锚点；任务状态可以存在，但工作目录使用仓库普通检出。
- 完成后只提交任务开始基线之后、属于该任务声明范围的文件到本仓库 master，随后普通推送 origin/master。
- 由于普通检出没有文件级隔离，写任务必须取得每仓库单写者租约；同仓库其他 AI 在租约期间只能只读。不同仓库仍可并行。
- 开始时记录 Git 状态基线及文件摘要；自动暂存显式路径，不得使用无范围的全仓库暂存来吞并其他人的既有改动。
- 若基线时已有未提交文件、任务中出现范围外改动、租约过期但执行进程仍在运行，或远端分支与本地不再快进，停止自动提交/推送并保留原状。不得覆盖或夹带既有工作。

## 6. 完成触发、幂等与异常恢复

### 6.1 触发条件

不要把模型输出“完成”、会话退出、桌面端 Stop 或 SessionEnd 直接当作 Git 写入授权。自动化必须等待任务执行器发出带有任务 ID、仓库键、预期文件范围和验收结果的明确完成事件。这个事件可以由 agent 自动发出，无需人工确认，但必须可审计、可重放。

### 6.2 持久化检查点

每个仓库、每个任务至少记录：

- 当前阶段与尝试编号；
- 任务分支、基线 SHA、候选/ready SHA、落地前后的 fxh SHA；
- 门禁命令版本、退出码与日志定位；
- archive ref、worktree 路径和清理结果；
- origin/fxh-dev（或 aisk 的 origin/master）推送前后 SHA 与失败分类；
- 最后一次成功阶段、下一次安全重试动作。

任务状态先落盘再做慢操作；启动恢复时按 Git 的真实祖先关系和现存 worktree 登记对账，不只相信状态文本。

### 6.3 故障处理矩阵

| 故障点 | 必须保持的事实 | 自动动作 |
|---|---|---|
| 提交失败、路径含凭据或范围不明 | 未有成功提交，不标记完成，不删除 worktree | 进入 blocked_commit，保留现场并说明具体文件/错误 |
| 门禁失败 | fxh 不变，worktree 和任务分支保留 | 进入 blocked_gate，允许修复后重新检查 |
| 冲突、来源审计失败或 fxh 已前进 | 不重置、不覆盖 fxh，不选 ours/theirs | 进入 blocked_landing，重新计算候选或等待处理 |
| 快进成功但进程在登记状态前崩溃 | 已有提交仍留在 fxh | 重启时根据 ready_sha 是否为 fxh 祖先恢复为 landed，不重复合并 |
| worktree 删除失败 | 已落地提交和 archive ref 保持不变 | 进入 cleanup_pending，安全重试；不调用通用 prune |
| 推送 fxh-dev 网络失败或被拒绝 | 本地集成/提交保留，远端状态不伪报成功 | 进入 push_pending，普通 push 幂等重试 |
| 用户取消 dev 弹窗 | 本地/远端 dev 均不变 | 结束本次显式操作，不后台重试写操作 |
| 自动提交期间发现其他写者 | 不混合两份改动 | 释放或过期租约后重新检查，不猜文件归属 |

自动清理的顺序必须是：验证提交 → 写入可恢复引用和检查点 → 确认 fxh 包含提交 → 删除任务工作树 → 验证 Git worktree 登记已消失。任一前置步骤失败都不能跳过。

## 7. 配置与实现边界

### 7.1 配置按仓库、按动作授权

现有全局 merge_policy.confirm_land 不足以表达本方案。建议增加明确的仓库级策略，而不是把全局确认关掉。以下是字段语义示意，字段名需按档案解析器能力再定：

~~~yaml
repos:
  be:
    workspace_mode: task-worktree
    integration_branch: fxh
    automatic:
      commit_task_branch: true
      land_to_local: fxh
      push_only: fxh-dev
      archive_after_verified_land: true
    limits:
      active_worktrees: 2
      materialized_worktrees: 4
    user_commands:
      local_dev_merge_requires_command_and_confirmation: true
      remote_dev_push_requires_command_and_confirmation: true
    forbidden_targets:
      - main
  web:
    workspace_mode: task-worktree
    integration_branch: fxh
    automatic:
      commit_task_branch: true
      land_to_local: fxh
      push_only: fxh-dev
      archive_after_verified_land: true
  docs:
    workspace_mode: direct
    automatic_commit_branch: fxh
    automatic_push: false
  aisk-hub:
    workspace_mode: direct
    automatic_commit_branch: master
    automatic_push_branch: master
  aisk-private:
    workspace_mode: direct
    automatic_commit_branch: master
    automatic_push_branch: master
~~~

配置校验必须拒绝：重复或含糊的目标分支、把 main 写入自动目标、未声明仓库使用自动推送、将 task-worktree 配额应用到 direct 仓库、把个人分支推送策略意外扩展到 trunk。

### 7.2 实现顺序

1. 保留现有通用默认值：非新华项目仍要求 land 确认；不得先全局改成静默。
2. 引入完成事件、任务单仓约束、每仓活跃/物化 worktree 配额和可重启的阶段记录。
3. 把个人分支 push 从 promote: ff-trunk 拆出来，确保个人自动流程只有 fxh → fxh-dev，代码路径不具备写 dev 的能力。
4. 增加精确仓库白名单的自动 land 与 post-land archive；在实际删除目录前验证归档引用和 fxh 祖先关系。
5. 为 direct 仓库增加单写者租约、任务基线差异选择和 branch allowlist；单独验证 aisk 的 master 例外。
6. 在模拟仓库完成全部故障注入和状态恢复验收后，才逐仓启用新华后端/前端档案。
7. 盘点并安全退役 docs/aisk 遗留锚点：逐项确认进程、脏改动、锁、分支和恢复点；不批量 prune，不使用强制删除处理未知目录。
8. 小流量试运行并观察 worktree 数、磁盘、门禁失败、推送待处理和误纳入文件；通过后再固定策略。

## 8. 验收门槛

### 8.1 P0，不通过即不得启用自动写入

- 自动落地仅允许精确的新华后端/前端仓库和本地 fxh。
- 自动个人推送仅允许 origin/fxh-dev；自动代码路径不能执行 dev merge 或 dev push。
- main 的所有写入路径 fail-closed，不能通过配置、环境变量或命令行绕过。
- 完成事件、门禁、落地、推送、归档任一步失败都不能伪报成功或丢失唯一工作副本。
- 重放/重启不会重复提交、重复合并、把别的任务提交认领成当前任务。
- docs/aisk direct 模式只提交任务文件集，不夹带任务开始前或并发写者的改动。
- aisk 的 master 自动写入只对 aisk-hub、aisk-private 精确生效；main 仍永久禁止。

### 8.2 自动化测试与运行验收

实现 AI 应至少覆盖以下用例；本提案写作过程中未运行测试：

1. 同时创建多任务时验证全局与每仓配额；暂停任务仍占物理槽位；排队任务没有 worktree。
2. 单后端、单前端任务分别提交、过门禁、快进本仓 fxh、推本仓 fxh-dev、归档并移除各自 worktree。
3. 跨仓需求被拆成两个独立任务；一个仓库被拒绝时另一仓库状态独立。
4. fxh 在门禁中途前进、重叠脏改动、非快进、冲突、敏感路径和来源审计异常都阻止落地与回收。
5. 注入进程崩溃点：任务提交后、fxh 快进后、archive ref 写入后、删除 worktree 后、远端 push 请求后；重启恢复仍能对账。
6. 网络断开、权限拒绝和远端非快进时不强推、不重置，进入可重试状态。
7. fxh-dev 自动推送路径无法触及 dev；dev merge 需显式用户命令和弹窗；dev push 需另一明确命令和弹窗。
8. 用不同 Git 命令包装、脚本、别名、钩子尝试写入 main 均被拦截；对 main 不存在“AI 例外”配置。
9. direct 仓库中预置其他文件改动和并发写者，确认自动提交不夹带、不覆盖；未取得单写者租约时拒绝写。
10. 审计日志包含任务号、仓库、SHA、阶段、确认结果和清理结果，不包含凭据、秘密文件内容或会话敏感信息。

真实分数应拆成“离线契约分”和“本机运行分”。方案设计可按下列 100 分量表评审；任何 P0 失败，综合分不得超过 89。没有真实仓库端到端证据时，不得宣称实现达到 99.9+。

| 维度 | 分值 |
|---|---:|
| 仓库、分支和自动/人工权限边界精确 | 20 |
| 提交—门禁—落地—推送状态机正确、幂等 | 20 |
| worktree 数量、暂停占用、回收和恢复完整 | 15 |
| main fail-closed 与 dev 双重授权 | 15 |
| direct 仓库的文件范围、单写者和脏基线保护 | 10 |
| 故障注入、重启恢复和远端失败验收 | 15 |
| 审计、隐私和运维可观测性 | 5 |
| **合计** | **100** |

## 9. 对实现 AI 的执行指示

- 先读本文件、agent-skills/WORKTREE.md、agent-skills/DECISIONS.md、README.md、agent-skills/engine/worktree/tasks.py、integrate.py 及当前目标仓 AGENTS.md。
- 本方案与旧的通用规则冲突之处必须通过**精确的仓库策略**解决；不能全局关闭确认或 main/master 护栏。
- 不在 docs、aisk 仓库创建 worktree。前后端才使用独立任务 worktree。
- 不要把“自动推 fxh-dev”复用成“自动合入或推送 dev”。这三种动作必须是不同代码路径和不同状态。
- 任何 main 写操作始终禁止。对 aisk master 的授权只针对两个指定仓库。
- 不要在自动化失败后删除 worktree、分支或唯一快照；不要强推、reset --hard、ours/theirs 或批量 prune 来让流程变绿。
- 先完成实现、负路径测试和恢复测试，再单独报告“离线契约分 / 本机运行分 / 尚未验证的客户端”。本方案的目标分不代表代码已经实现。

## 10. 参考

- 当前通用设计：[agent-skills/WORKTREE.md](../../WORKTREE.md)
- 当前决策记录：[agent-skills/DECISIONS.md](../../DECISIONS.md)
- Git worktree 官方文档：[git-worktree](https://git-scm.com/docs/git-worktree)
- Codex 托管 worktree 数量与快照回收：[Codex Worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)
- GitHub Copilot 单仓库临时任务环境：[About Copilot cloud agent](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent)
