# agent-skills

项目无关的 AI 技能内核。技能正文里不出现任何路径、IP、口令；事实由 `aisk` 现场取。

## 三层结构

    Layer 1  内核    这个仓库          与项目无关，所有项目共用
    Layer 2  档案    $AISK_HOME/profiles  一个项目一份，纯数据，永不入库
    Layer 3  绑定    各 AI 工具端      由 aisk link 生成（P1 阶段）

箭头只能从上往下：内核不知道任何具体项目，档案不知道任何具体工具。

## 快速开始

    cp templates/profile.example.yaml "$AISK_HOME/profiles/<项目名>.yaml"
    # 编辑它，填仓库路径和环境
    ./bin/aisk doctor

## 命令

    aisk profile                    当前解析到哪个 profile、为什么
    aisk env [--env X] [--brief]    环境事实（--brief 供技能正文注入）
    aisk fact services --env X      服务清单
    aisk fact service <名> --env X  命名空间/镜像/CI Job/DataId/端口
    aisk fact entry <名> --env X    入口类型与端口
    aisk doctor                     配置与路径体检
    aisk audit codex                检查 Codex 重复/历史影子技能
    aisk contract verify            验证迁移、适配器与协同契约
    aisk contract alias <旧名称>    解析旧技能逻辑别名
    aisk contract tokens             验证静态 Token 预算与重复上下文门禁
    aisk contract token-runtime --task "任务" [--complexity X]
                                    按任务选择 lite/standard/deep/emergency 上下文档位

## 设计约束

- **零依赖**：只用 Python 标准库。PyYAML 装了就用，没装退回内置的 YAML 子集解析器。
- **不猜**：profile 按当前仓库自动匹配；匹配不到、匹配到多个、多环境未指定，一律报错要求显式指定，绝不静默猜测。
- **不抄**：服务事实从部署仓 manifest 现场解析，不在 profile 里维护第二份清单。
- **跨平台**：纯 Python、不调 shell、路径走 pathlib、IO 显式 utf-8、终端强制 utf-8。

## 运行环境

内核零依赖，任何 python3 都能跑。技能脚本要用的第三方库装在
`$AISK_HOME/venv`；运行态和私有覆盖层均不进入公开仓库。

## 决策记录

公开架构和隐私边界见根目录 `docs/ARCHITECTURE.md`、`docs/PRIVACY.md`。

## 测试

    PYTHONPATH=agent-skills python3 -m unittest discover -s agent-skills/tests -p 'test_*.py'
    ./bin/aisk public verify

## 能力如何组合

Skill 保存领域流程，CLI/broker 执行确定性操作，MCP 复用同一底层能力。
当前 MCP 暴露 5 个事实/查询工具；它不执行整套 Skill，也不替代宿主的任务编排。
Codex 默认通过已安装 CLI 获取这些能力，已有原生工具继续处理文件、终端等工作。
只有实际调用路径能带来收益时才新增 MCP 接入，不能从「统一接口」推导出全面替换。

优化以可验证结果为准：错误语义、参数边界、正确的项目选择、分发资源完整性、
变更前备份、内容漂移检测。关键词路由通过率不等于真实模型完成任务的准确率。
当前实现与本次调整的依据见 [DECISIONS.md](DECISIONS.md)。

## 更新当前 Codex 技能

    ./bin/aisk link codex --dry-run
    ./bin/aisk link codex
    ./bin/aisk audit codex

分发前备份将被覆盖或退役的受管目录；同名非受管技能会报冲突，不覆盖。
`references/scripts/assets/agents` 随技能分发；相同内容不重复写。
当前会话可能仍显示启动时的技能目录快照，重新打开任务后核对新目录。

## 多工具任务工作区

统一入口 `aisk task`：先 `find <关键词>`，已有任务用 `context <任务>` 和 `claim` 接力；概览用 `status --brief`。
各工具（Claude Code、Codex、Antigravity、WorkBuddy、WorkBuddy AI、Cursor）共用任务流程，
能接钩子的端各自接入，Mac 与 Win11 使用各自本地工作区。新项目模板见 [示例档案](templates/worktree-profile.example.yaml)，
设计、兼容迁移和验证范围见根目录 `docs/COORDINATION.md`。

任务目录内的标准提交入口是 `aisk task commit <任务> --path <相对路径> -m "type: 说明"`
或 `aisk task commit <任务> --all -m "type: 说明"`。它只提交当前任务分支，不读取或修改
`fxh` 主工作区，也不执行公网 push；因此 `fxh` 有未提交改动时，其他任务仍可独立提交、
check 和 ready。Windows ready 通过本地/UNC hub 中央交换，确认框标题包含
`git推送-<工具>`，确认后非强制推送并回读校验；Mac 再执行 land/promote。只有实际修改
`fxh` 文件树的 land 才要求该主工作区干净。
