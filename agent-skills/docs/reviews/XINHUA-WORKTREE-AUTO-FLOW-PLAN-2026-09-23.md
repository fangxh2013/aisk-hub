# 新华前后端 worktree 自动落地、推送与回收方案

> 状态：核心实现、离线验收与 macOS 发布重试 LaunchAgent 安装已完成；本机运行证据见 §11。旧 direct 锚点只读盘点，含未提交改动的目录保持原样。
> 日期：2026-09-23
> 修订：v1.4；补充 active finish 的 scope 规范化、提交后恢复 checkpoint、持久升级通知重放及并发内核状态复核。
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
3. 如果 fxh-dev 推送失败，已落地到 fxh 的提交和任务归档仍保留；可恢复错误进入 push_pending 并幂等重试，确定性拒绝立即升级给用户，待处理超过时限也必须主动告警，不允许无限期静默挂起。
4. “已发布”只在 origin/fxh-dev 确认包含目标 SHA 后成立。禁止用强推、重置或改写历史把失败伪装成成功。

因此，系统承诺的是**成功才完成、失败可恢复、重试不重复提交**，不是在网络和远端规则失效时仍声称 100% 成功。

## 2. 实施前基线与差距（2026-09-23 快照）

以下是实现前从本机只读检查和当时的 aisk-hub 源码核实的基线。它们是差距分析的历史快照，不应被当作实现后的当前状态；实际行为以 §11 验收记录及当前代码为准。

1. 后端和前端目前各登记 3 个工作区：主检出、dev 锚点、门禁锚点，共 6 个固定工作区；这不是 6 个任务 worktree。
2. 新华档案当前以 fxh 为集成分支，后端和前端的个人推送分支均为 fxh-dev，promote 当前配置为 ff-trunk，confirm_land 当前为 true。
3. 当前 quota_active=8 检查的是进行中任务数，不是 Git worktree 数。create_task 会对任务选择的每个仓库分别创建一个 worktree；一个前后端多仓任务最多产生两个任务 worktree。
4. pause 会保存未提交改动快照并把任务置为暂停，但保留原工作树。因此暂停会释放当前活跃任务配额，却不会释放磁盘上的 worktree。
5. 现有 land 已有来源审计、候选提交门禁、并发锁、落地前再次核对分支头，并以 --ff-only 合入本地集成分支；当前确认策略仍会要求人工点击。
6. 实现前的 promote 对 ff-trunk 仓库不仅会推送 fxh-dev，还会继续处理本地 dev 与远端 dev。它不能直接作为“只静默推送 fxh-dev”的后台动作；必须先将个人分支发布与 dev 操作拆成互不调用的接口。
7. 当前 archive 会把任务分支保存到 refs/aisk/archive/...，复制任务记录并移除 worktree；它是现有安全回收基础。自动流程需要在落地提交可恢复之后调用专用归档路径，不能把通用 --force 当作删除安全证明。
8. 本次只读核验通过 `ls-remote` 检查实时远端：后端、前端 `origin/fxh-dev` 当前都可快进到本地 fxh（本地各领先 2 个提交）；前端 `origin/fxh` 与 `origin/fxh-dev` 仍相互独有 1272/1450 个提交。远端 `fxh` 不是发布目标；不得把它当作候选 upstream、自动删除或自动修复。每次启用/运行自动发布都必须重新校验本地 origin fetch/push URL 与档案 `expected_origin_url` 精确一致、本地 fxh upstream/refspec 只映射到 origin/fxh-dev，并对实时目标做祖先关系预检；任何错源、非快进或目标歧义都立即停止并升级。以上分支数量和关系是 2026-09-23 的观测快照，不能代替运行时检查。
9. 当前已存在按时间与磁盘占用运行的 retention/gc，不是待建功能：promoted_hours=72，gc 只把已上主干或已验收且超期的任务列为归档候选；archive_days=30 用于过期构建日志和归档引用，引用只有在提交已并入集成分支或主干时才会退休，未并入的引用保留；`doctor` 按 `disk_budget_gb` 检查并提醒。公共档案默认值为 10 GB，但本机新华档案显式设置为 5 GB；实现以实际档案值为准。`build_dirs` 配置构建产物目录，默认不清理进行中任务的构建目录。gc 默认预览，显式 `--apply` 才执行回收。

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
    Archived --> push_pending: 自动推送暂时失败
    Archived --> needs_attention: 非快进/权限/保护规则拒绝
    Archived --> Published: fxh-dev 确认包含目标 SHA
    push_pending --> Published: 幂等重试成功
    push_pending --> needs_attention: 满 15 分钟或失败 3 次
    needs_attention --> blocked_publish: 满 1 小时仍未解决
~~~

流程要求：

1. 只有任务执行器明确发出“交付就绪”事件后才启动完成流水线。会话结束、模型暂停、进程退出或空闲超时都不等于任务完成。
2. 在任务 worktree 内检查基线差异、暂存范围、未跟踪文件和敏感路径；只提交本任务改动。禁止把主检出里已有的用户改动、其他任务文件或凭据带入提交。
3. 自动生成符合仓库规范的提交说明，在**任务分支**提交；不得直接在 worktree 中检出或改写 fxh。
4. 对这个确切提交运行仓库门禁，记录 ready_sha、基线 SHA、门禁版本和日志。任何门禁失败都留在任务分支，不得进入自动落地阶段。
5. 在本仓库集成锁下生成落地计划；校验任务来源、分支祖先、文件重叠、敏感路径、当前 fxh 头和工作区状态。对候选提交运行门禁。
6. 使用 compare-and-swap 语义保护 fxh：只有 fxh 仍处于计划记录的 head，且候选可快进时才落地；分支在检查期间前进就重新计划，不强行覆盖。
7. 合入后再次验证 fxh 包含 ready_sha，先写入可恢复的落地与归档记录，再移除任务 worktree 和任务分支。worktree 清理失败时保持 cleanup_pending 并重试，不回滚已正确落地的 fxh。
8. 先验证本地 fxh 的 upstream/refspec 精确指向本仓库 origin/fxh-dev，再将本地 fxh 普通非强制推送至该目标；不得对 origin/fxh 做写操作。收到非快进/目标歧义时立即停止并升级，网络暂时失败时记录 push_pending；禁止换 token、绕过保护或强推。
9. 用任务 ID、仓库键、ready_sha 和远端 SHA 更新登记簿。重复收到完成事件时必须识别已完成阶段，不得产生重复提交或把另一任务的提交登记在本任务名下。

静默表示**无确认弹窗**，不表示隐藏失败。成功时保留可查询的审计记录；失败时在看板和当前执行会话中明确显示阻塞阶段与安全恢复方式。

### 3.2 worktree 配额与回收策略（叠加现有 retention/gc）

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
- 看板展示活跃槽位、暂停占用、清理待重试、每仓磁盘占用和最近失败原因。达到磁盘预算时先拒绝新工作树；只通过完成流水线已授权的即时归档，或用户显式运行 gc --apply，清理符合安全条件的任务，不触碰活跃任务。

#### 与现有 retention/gc 的边界

新方案**叠加现有 retention/gc，不替代它，也不重置现有阈值**。两者分别解决“现在能否再物化一个任务工作树”和“哪些已完成资源到期后可回收”，配置、状态和看板指标必须分开：

| 机制 | 解决的问题 | 规则 |
|---|---|---|
| 活跃/物化槽位上限 | 新任务是否能立即创建 worktree | 同步准入控制。活跃、暂停、cleanup_pending 的真实目录都计入；排队任务不占物理槽位 |
| retention.promoted_hours（默认 72 小时） | 已完成任务何时成为通用 gc 归档候选 | 保留现行规则，只处理已上主干或已验收且超过保留期的任务；不能因槽位不足提前删除暂停、活跃或未验证任务 |
| retention.archive_days（默认 30 天） | 旧日志与已安全退休的归档引用何时到期 | 继续由现有 gc 处理；归档引用仅在提交已并入集成分支或主干后才可退休，未并入的引用无论多旧都保留。自动流水线任务还须确认 fxh-dev 已包含目标 SHA；push_pending 或 remote_sha 未确认时额外 pin 其归档引用 |
| retention.disk_budget_gb（默认 10 GB） | 总任务目录磁盘占用的提醒阈值 | 保留 doctor 告警，并单独展示实际用量；达到阈值可阻止新 worktree 物化，但不能自动清理活跃现场 |
| retention.build_dirs | 可清理的构建缓存/产物范围 | 沿用档案配置和现行 gc 安全规则；活动任务默认跳过，不能用共享可写构建目录换取槽位 |

任务成功落地后的即时自动回收是完成流水线的专用步骤：它在确认 ready_sha 已包含于本地 fxh、持久化归档引用和检查点之后调用安全归档原语，满足本方案已授权的静默回收要求；这不等于启动通用 gc，也不等待 72 小时。不得为让即时归档生效而粗放扩大 gc 候选状态。通用 gc 仍用于既有任务的超期清理、构建产物、日志和到期归档引用，默认预览且只有显式 apply 才执行。两条路径必须幂等，GC 不得重复归档或删除流水线刚写入且仍作为唯一恢复副本的引用；对自动流水线任务，fxh-dev 尚未确认包含目标 SHA 时，即使本地 fxh 已包含提交，也必须保留归档引用。

当槽位已满时，准入器只允许按完成流水线回收**刚刚安全落地且已验证**的任务；不得把 gc --apply 当作隐式腾位动作，也不得为配额驱逐暂停任务或跳过 72 小时保留期。没有安全可回收目录时，任务保持无 worktree 的 queued 状态（或在未实现排队前拒绝物化并报告占用），提示操作者查看 gc 预览。doctor/看板分别展示槽位占用、retention 到期候选、构建产物大小和磁盘预算告警，不能把“槽位满”与“磁盘超预算”合并成一个信号。

## 4. fxh、fxh-dev 与 dev 的权限边界

### 4.1 自动允许的动作：只作用于后端和前端

本仓库白名单中的完成流水线可以无弹窗地：

- 在任务分支提交本任务改动；
- 通过门禁后以快进方式更新本地 fxh；
- 将本地 fxh 普通推送到 origin/fxh-dev；
- 推送前验证本地 fxh 的 upstream/refspec 精确映射到 origin/fxh-dev；拒绝向 origin/fxh 写入；
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
- 档案授权的 `expected_origin_url`、实际 origin fetch/push URL 与显式 refspec，避免 origin 被误改后静默发布到另一仓库；
- 推送待处理的首次发生时间、尝试次数、下次重试时间、失败类别、升级时间与通知去重键；
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
| fxh-dev 推送遇到可恢复的网络/服务错误 | 本地集成/提交保留，远端状态不伪报成功 | 进入 push_pending；按有上限的退避计划普通 push 重试，并持久化首次待处理时间、尝试次数和下次重试时间 |
| fxh-dev 推送被非快进、权限或分支保护拒绝 | 本地集成/提交保留，不改写远端历史 | 立即转 needs_attention 并通知用户；禁止把确定性拒绝当成网络故障反复重试，禁止自动 merge/rebase/force |
| push_pending 持续未解决 | 不得显示已发布或静默丢在后台 | 默认首次待处理满 15 分钟或已失败 3 次（先到者为准）时主动升级到当前任务/桌面可见通知并标记 needs_attention；满 1 小时仍未解决时再发一次升级通知并标记 blocked_publish。可恢复的瞬时错误之后按最长 60 分钟间隔继续幂等重试，但不重复发送相同告警；确定性错误暂停自动重试，等待用户处理。阈值可按档案调整，状态变化或解决后关闭告警 |
| 用户取消 dev 弹窗 | 本地/远端 dev 均不变 | 结束本次显式操作，不后台重试写操作 |
| 自动提交期间发现其他写者 | 不混合两份改动 | 释放或过期租约后重新检查，不猜文件归属 |

桌面通知有独立的持久待发队列：升级事件先写入审计记录；`osascript` 临时失败时保留事件，后续 scheduler 轮询重放；同一事件通过持久 event ID 去重。LaunchAgent 只在 macOS 安装，卸载只在 `launchctl` 明确确认服务不存在或成功 bootout 后删除 plist；状态查询错误时保留配置并报错。安装设有 `RunAtLoad`，会立即执行一轮 due 扫描，因此安装前必须确认没有未审查的待发任务。

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
      expected_origin_url: "仓库受控配置中的单个精确 URL，不得含凭据"
      publish_pending_policy:
        retry_delays_minutes: [1, 5, 15]
        needs_attention_after_minutes: 15
        needs_attention_after_attempts: 3
        blocked_after_minutes: 60
        retry_only_transient_failures: true
        notify_deduplication: task_repo_remote_stage
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
      expected_origin_url: "仓库受控配置中的单个精确 URL，不得含凭据"
      publish_pending_policy:
        retry_delays_minutes: [1, 5, 15]
        needs_attention_after_minutes: 15
        needs_attention_after_attempts: 3
        blocked_after_minutes: 60
        retry_only_transient_failures: true
        notify_deduplication: task_repo_remote_stage
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

配置校验必须拒绝：重复或含糊的目标分支、把 main 写入自动目标、未声明仓库使用自动推送、缺少/格式错误/与本地不匹配的 expected_origin_url、将 task-worktree 配额应用到 direct 仓库、把个人分支推送策略意外扩展到 trunk。

### 7.2 实现顺序

1. 保留现有通用默认值：非新华项目仍要求 land 确认；不得先全局改成静默。
2. 引入完成事件、任务单仓约束、每仓活跃/物化 worktree 配额和可重启的阶段记录。
3. 把个人分支 push 从 promote: ff-trunk 拆出来，确保个人自动流程只有 fxh → fxh-dev，代码路径不具备写 dev 的能力。
4. 增加精确仓库白名单的自动 land 与 post-land archive；在实际删除目录前验证归档引用和 fxh 祖先关系。
5. 在新槽位准入器旁接入现有 retention/gc：复用当前阈值和安全候选逻辑，单独呈现槽位与磁盘/年龄指标；不把 gc --apply 暗中变成腾位动作；验证暂停任务、未合并归档引用和唯一恢复副本的保护条件。
6. 为 push_pending 增加持久化重试时间、失败分类、15 分钟/3 次首次升级、60 分钟再次升级及通知去重；非快进、权限和保护规则拒绝立即升级，不自动改写远端历史。
7. 为 direct 仓库增加单写者租约、任务基线差异选择和 branch allowlist；单独验证 aisk 的 master 例外。
8. 在模拟仓库完成全部故障注入和状态恢复验收后，才逐仓启用新华后端/前端档案。
9. 盘点并安全退役 docs/aisk 遗留锚点：逐项确认进程、脏改动、锁、分支和恢复点；不批量 prune，不使用强制删除处理未知目录。
10. 小流量试运行并观察 worktree 数、磁盘、门禁失败、推送待处理时长/升级告警和误纳入文件；通过后再固定策略。

## 8. 验收门槛

### 8.1 P0，不通过即不得启用自动写入

- 自动落地仅允许精确的新华后端/前端仓库和本地 fxh。
- 自动个人推送仅允许 origin/fxh-dev；自动代码路径不能执行 dev merge 或 dev push。
- 推送目标必须由仓库配置和 fxh upstream 双重校验为 origin/fxh-dev；origin/fxh 一律不是写目标，目标分叉/非快进必须立即阻断并告警。
- main 的所有写入路径 fail-closed，不能通过配置、环境变量或命令行绕过。
- 完成事件、门禁、落地、推送、归档任一步失败都不能伪报成功或丢失唯一工作副本。
- 重放/重启不会重复提交、重复合并、把别的任务提交认领成当前任务。
- docs/aisk direct 模式只提交任务文件集，不夹带任务开始前或并发写者的改动。
- aisk 的 master 自动写入只对 aisk-hub、aisk-private 精确生效；main 仍永久禁止。
- 槽位准入与 retention/gc 分离：不覆盖现有阈值；gc 默认仍为预览；槽位满不触发未经授权的 gc --apply；活跃/暂停现场和未并入集成分支的归档引用不可被配额或过期策略删除。
- 即时 post-land archive 与通用 gc 幂等且可区分；GC 不重复归档，也不退休仍是唯一可恢复副本的引用。
- push_pending 的可恢复错误具备有限退避与持久化状态；到 15 分钟或 3 次失败必有主动可见升级，60 分钟时再次升级；确定性拒绝立即告警；成功后关闭告警且重复轮询不重复通知。
- 新流水线的 archive ref 在 fxh-dev 已验证包含目标 SHA 之前不可由 archive_days 退休；publish 状态、远端 SHA 和引用保留/释放决定必须跨重启一致。
- 自动升级通知必须使用已配置且通过验收的任务/桌面通知入口；若运行环境没有可用通知入口，不得启用“静默自动推送”，只能持久化 needs_attention 并将启用判定置为未通过。

### 8.2 自动化测试与运行验收

实现 AI 应至少覆盖以下用例；本提案写作过程中未运行测试：

1. 同时创建多任务时验证全局与每仓配额；暂停任务仍占物理槽位；排队任务没有 worktree。
2. 单后端、单前端任务分别提交、过门禁、快进本仓 fxh、校验 upstream 后推本仓 fxh-dev、归档并移除各自 worktree；试图将 origin/fxh 当目标或遇到非快进时立即阻断并告警。
3. 跨仓需求被拆成两个独立任务；一个仓库被拒绝时另一仓库状态独立。
4. fxh 在门禁中途前进、重叠脏改动、非快进、冲突、敏感路径和来源审计异常都阻止落地与回收。
5. 注入进程崩溃点：任务提交后、fxh 快进后、archive ref 写入后、删除 worktree 后、远端 push 请求后；重启恢复仍能对账。
6. 网络暂断进入 push_pending 并按退避重试；持续待处理在 15 分钟或 3 次失败时仅升级一次、满 60 分钟再次升级；之后瞬时错误降到每 60 分钟重试且不重复通知；成功关闭告警且重复事件不重复通知。权限拒绝和远端非快进立即升级，不强推、不重置、不反复无效重试；无可用通知入口时自动推送启用门禁失败。
7. fxh-dev 自动推送路径无法触及 dev；dev merge 需显式用户命令和弹窗；dev push 需另一明确命令和弹窗。
8. 用不同 Git 命令包装、脚本、别名、钩子尝试写入 main 均被拦截；对 main 不存在“AI 例外”配置。
9. direct 仓库中预置其他文件改动和并发写者，确认自动提交不夹带、不覆盖；未取得单写者租约时拒绝写。
10. 审计日志包含任务号、仓库、SHA、阶段、确认结果和清理结果，不包含凭据、秘密文件内容或会话敏感信息。
11. gc 预览与 apply 复用现有候选规则：promoted_hours 默认 72 小时、archive_days 默认 30 天、disk_budget_gb 公共默认 10 GB（本机新华档案当前 5 GB）、build_dirs 按档案生效；预览不改动，apply 只处理符合现行安全条件的任务/产物；磁盘占用达到档案预算时，准入器拒绝新物化而不暗中 gc。
12. 活跃或暂停任务不成为超期归档候选；活动任务构建目录默认不清；未并入集成分支/主干的 archive ref 即使超过 archive_days 也保留；任务历史记录保留。
13. 自动落地后的即时归档不等待 promoted_hours，但必须先验证 fxh 祖先关系并写 archive ref；push_pending 或 remote_sha 未验证的自动流水线任务，即使已超过 archive_days 也不得退休该引用；推送完成后才可从归档时间起按现行期限退休。
14. 即时归档与 gc 重复触发、进程在 ref 写入或目录删除前后崩溃时均能幂等恢复；不会丢失工作树或唯一引用。
15. origin 被改指向其他 URL、设置额外 pushurl、目标 refspec 含糊或运行平台不是 macOS 时，自动发布/scheduler 必须在任何远端写入前 fail-closed；通知发送失败后待发事件必须跨轮询重放且不重复发送已成功事件。

真实分数应拆成“离线契约分”和“本机运行分”。方案设计可按下列 100 分量表评审；任何 P0 失败，综合分不得超过 89。没有真实仓库端到端证据时，不得宣称实现达到 99.9+。

| 维度 | 分值 |
|---|---:|
| 仓库、分支和自动/人工权限边界精确 | 20 |
| 提交—门禁—落地—推送状态机正确、幂等 | 20 |
| worktree 数量、暂停占用，以及与 retention/gc 的协同回收和恢复完整 | 15 |
| main fail-closed 与 dev 双重授权 | 15 |
| direct 仓库的文件范围、单写者和脏基线保护 | 10 |
| 故障注入、重启恢复、推送超时升级和远端失败验收 | 15 |
| 审计、隐私、告警去重和运维可观测性 | 5 |
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
- 当前 retention/gc 规则与实现：[WORKTREE.md §5.1、配置 §5.5](../../WORKTREE.md)，[tasks.py 的 gc_candidates/cmd_gc](../../engine/worktree/tasks.py)
- 当前决策记录：[agent-skills/DECISIONS.md](../../DECISIONS.md)
- Git worktree 官方文档：[git-worktree](https://git-scm.com/docs/git-worktree)
- Codex 托管 worktree 数量与快照回收：[Codex Worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)
- GitHub Copilot 单仓库临时任务环境：[About Copilot cloud agent](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent)

## 11. 实施与验收记录（2026-09-23）

本节记录本方案对应实现的本机证据，避免把设计目标和已验证行为混为一谈。

### 11.1 已完成实现

- 后端、前端按仓库分别使用任务 worktree；活跃与物化配额、direct 仓库单写者租约、精确文件范围提交、崩溃恢复意图、CAS 落地和 post-land 归档已实现。
- `finish` 先解析 `--path` 的真实仓库内路径，再检查任务 scope；拒绝规范化成仓库根目录的路径及解析到 scope 外的符号链接。提交 checkpoint 与清除 commit intent 在同次登记簿保存中完成，check/ready 失败或进程中断后可验证 HEAD 并续跑，不重复创建空提交。
- 自动个人推送限制为目标仓库的 `origin/fxh-dev`，推送前会重新核对 origin 身份；自动发布路径不能触及本地或远端 `dev`。发布失败写入持久化 `push_pending`，有限退避重试，超过阈值升级并对通知失败做持久化重放。
- `finish` 产生的发布升级会先写入 macOS 通知 JSON outbox 再调用通知入口；scheduler 也会从持久 `escalations_emitted` 状态重建通知事件，覆盖“发布状态已保存、进程在入通知日志前退出”的崩溃窗口，重复轮询由通知事件键去重。
- main 写入护栏覆盖常见 push refspec 与 `update-ref --stdin` 绕过路径；禁止 `--all`/`--mirror`。dev 合并与 dev 推送保留独立命令和人工确认。
- direct 模式用于新华 docs 与 Aisk 仓库；docs 只提交到本地 `fxh`、不自动推送；`aisk-hub` 和 `aisk-private` 仅按精确仓库 allowlist 自动提交并普通推送 `master`。
- worktree 槽位配额与已有 retention/gc 并存：槽位满时拒绝新建，不隐式执行 `gc --apply`；GC 安全候选、年龄阈值和磁盘预算仍由现有档案策略管理。

### 11.2 自动化验收证据

- Aisk-hub 官方 full runner：549 项单元测试通过，1 项跳过、2 项预期失败；进程协议验收 93/93；内核卫生检查与文档漂移检查通过。
- 使用真实仓库 Git hooks 的协议脚本：94/94 通过。另有定向验收：direct task 20 项、publish driver 18 项、分离发布流程 17 项、配额 13 项、autoflow 恢复 4 项、task hardening 48 项均通过。
- 本轮新增 `test_autoflow.py`：15 项编排/恢复/安全用例通过；publish worker 6 项与既有 autoflow recovery 4 项通过。覆盖路径穿越及 symlink scope 绕过、门禁失败续跑、通知 outbox 崩溃恢复和幂等送达。
- 文档路由评估：54 个案例，first-hit 53/54（98.1%），top-three 54/54（100%）。配置解析及五个目标仓的 effective origin 身份检查通过；`git diff --check` 与 Python 编译检查通过。
- 测试过程中的系统确认弹窗已改为测试内 mock 并断言确认边界；最终 full runner 未弹出真实确认框，也未触发真实仓库推送。

### 11.3 本机运行证据与限制

- 本机 `aisk task doctor` 通过，报告 8 个警告：后端/前端本地 `dev` 与 `main` 仅比远端快进；docs 仍登记两个迁移遗留 Aisk worktree；机器级 Git 配置存在漂移。检查没有写入或修复这些状态。
- Xinhua 发布重试 LaunchAgent 已安装并由 `launchctl` 确认运行，每 60 秒检查一次；安装后的首次运行日志为“没有到期的 fxh-dev 发布任务”，stderr 为空。当前没有 archived BE/WEB 任务带发布操作键、没有 `publish-pending` 状态文件，也没有待送达通知记录。
- docs 当前 checkout 已处于档案声明的 `fxh` 分支且干净。两个旧 docs 锚点保留；其中 `master` 锚点含已有的用户文档改动，不能作为自动清理对象。direct 模式的任务流程不会使用或删除它们。
- 当前验收没有合成或执行新华业务仓库的真实 `fxh-dev` 推送，也没有触碰任何 `main`。因此，真实远端保护规则、网络环境和通知中心的端到端送达仍由首次真实任务验证；无待发布任务时不制造一笔业务推送来伪造端到端证据。
- 方案设计完整性按 §8.1 量表评为 **100/100**；这是设计与离线契约验收分。真实远端运行分不宣称 100 分，须在首次真实后端/前端任务及通知升级中采集证据后另评。

### 11.4 本轮对并发分析的只读核对

- 本轮审查开始时 Aisk-hub HEAD 与 `origin/master` 同为 `e7f411f`，工作区干净；通知、scheduler、direct 与自动流实现已在前序提交中。`7160728` 的 Git 树只有方案文档改动，不含 `autoflow`、`publish_notify`、`publish_scheduler` 或其测试；把它描述成“66→88 项测试后的完整安全版本”不符合当前仓库历史。
- 审查过程中共享 `master` 新出现本地提交 `1d30d0f`；逐行审阅后确认它仅将已跟踪的 worktree 新测试加入 privacy 发布/扫描白名单。本轮新增的 `test_autoflow.py` 也已精确加入同一白名单并通过 privacy、内核卫生验收；没有丢弃或覆盖这笔并发提交。
- `~/.aisk/kernel-root` 当前内容为 `/Users/felix/work/aisk-hub/agent-skills`；运行时 xinhua profile 与版本化 profile 的自动策略一致，使用 `expected_origin_url`。本机检查时没有活跃 be/web 任务。
- 发布 worker 的 stderr 留有一条 12:51:53 的历史 SyntaxError，堆栈显示该次启动时 `config.py` 第 56 行存在 Git 冲突标记；本轮复核时文件可编译、工作区干净、LaunchAgent 仍运行，stdout 持续报告没有到期任务，且任务登记簿无活跃任务。该记录证明共享可变内核会受到并发编辑窗口影响；若以后要部署不可变内核，应基于当前已验收版本，并同时明确隔离/绑定 profile 与 runtime，不能只克隆旧的 `7160728`。
- 将内核固定到 `7160728` 不能实现完整隔离：`bin/aisk` 会从相邻 aisk-private 仓库解析 profile，并复用 profile 指定的仓库目录与 `~/.aisk-runtime` 任务状态；旧内核也不包含 direct/finish/scheduler 行为。不要把该历史提交作为当前策略的稳定副本。
