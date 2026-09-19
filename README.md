# aisk-hub

面向 Codex、Claude、Antigravity、WorkBuddy 的本地 AI 技能协同内核。

公共仓只提供通用内核、契约、适配器和脱敏测试。私有业务代码、真实拓扑与端口、配置正文、
凭据、会话内容、企业知识和任务在制品留在私有层；公共仓只保存字段形状、摘要和安全降级规则。

> 文档来源：Codex。本文是可公开复核的安装、协同与安全交付入口；真实客户端运行分仍须按验收口径单独联调。

## 技能迁移契约

当前契约是 9 个 canonical 技能、26 个旧技能 alias，另保留 `sec-review` 与 `db-design` 两个
历史兼容别名。alias 是逻辑路由，不是 symlink。删除 alias 只能在引用扫描为零、迁移验收通过、
回滚包已保存且经过一个发布回滚窗口后进行；默认不自动删除。

## Overlay v2

示例 manifest 可以使用 `<placeholder>`，但这只能通过离线 schema 检查，不能算真实私有 Overlay
完成。真实验收采用外置 lock 的单一路径：manifest 提供 `manifest_id` 和真实 sha256，外置
`tools/overlay.lock.json` 提供同一 `manifest_id`、manifest 摘要、40 位 kernel commit 和生成时间。

```text
PYTHONPATH=agent-skills python3 tools/verify_spec.py \
  --private-manifest "$AISKHUB_PRIVATE_ROOT/OVERLAY_MANIFEST.yaml" \
  --overlay-lock "$AISKHUB_PRIVATE_ROOT/tools/overlay.lock.json" \
  --report-dir /tmp/aisk-verify-reports
```

校验器只读私有文件，不复制、不改写；缺少真实文件、manifest_id 或 lock 时明确输出
`offline-only`，不把合成样例冒充完成。

## 验证口径

```text
PYTHONPATH=agent-skills python3 tools/verify_spec.py --no-report-files
PYTHONPATH=agent-skills python3 -m unittest discover -s agent-skills/tests -p 'test_*.py'
```

`offline_contract_score` 是可重复的静态契约分；`real_file_score` 只表示真实私有文件与外置 lock
是否匹配；`real_runtime_score` 只有真实客户端/外部系统联调后才有值。当前仓库不把离线分数写成
Codex、Claude、Antigravity 或 WorkBuddy 的实机通过，未联调时必须保持 `runtime_status: offline-only`。

## Token Runtime

Token Runtime 是上下文路由器和硬约束保护器，不是机械摘要器。它根据任务风险选择
`lite`、`standard`、`deep`、`emergency` 四档；数据库、权限、隐私、生产和 Git 高危动作会硬触发
`deep`，证据不确定、冲突、缺失或预算解析失败会回退到 `emergency`，安全、隐私、验证和授权门禁
不会因节省 Token 被裁掉。审计只记录模式、触发器、规则摘要和计数，不记录任务正文、凭据或环境值。

```text
./bin/aisk contract token-runtime --task "git push master" --complexity simple
./bin/aisk contract token-runtime --task "数据库迁移" --complexity complex
```

四个客户端的标题模板、预算和 dry-run 接入约束见
[`docs/TOKEN-RUNTIME-ADAPTERS.md`](docs/TOKEN-RUNTIME-ADAPTERS.md)；离线基准与真实运行分离见
[`docs/TOKEN-RUNTIME-BENCHMARK.md`](docs/TOKEN-RUNTIME-BENCHMARK.md)。

回滚依赖外置 lock、manifest 备份和任务证据包：先恢复上一份 manifest/lock，再重新运行离线校验，
确认摘要和工作树回到基线后才恢复客户端加载。不要通过删除历史 alias 或覆盖私有文件来回滚。

## 安装与首次使用

### A. 只安装公开通用内核（不需要私有仓权限）

公开仓可以单独使用；安装前先做 dry-run，确认目标端和将被回收的旧 aisk 副本：

```text
git clone https://github.com/fangxh2013/aisk-hub.git
cd aisk-hub

./bin/aisk link claude codex antigravity workbuddy workbuddy-ai --dry-run
./bin/aisk link claude codex antigravity workbuddy workbuddy-ai
./bin/aisk permit --dry-run
```

`link` 会向用户目录写入技能和适配器配置；`permit` 会合并用户级只读权限与 WorkBuddy 守卫。
确认 dry-run 输出后，才执行真正的权限安装：

```text
./bin/aisk permit
```

不需要业务事实时，以上步骤不要求 profile。`aisk link` 只分发 9 个 canonical 技能，不建立
symlink，也不覆盖端上未由 aisk 管理的同名第三方技能。WorkBuddy 桌面版和 WorkBuddy AI
分别写入各自技能目录，但共享用户级权限配置；适配器会区分 `workbuddy` 与 `workbuddy-ai`。

### B. 接入自己的项目 profile（需要查仓库/环境事实时）

profile 是本机配置，不提交到公开仓库；它只保存仓库路径、环境名称和脱敏的事实位置，不保存
密码、Token 或配置正文：

```text
export AISK_HOME="${AISK_HOME:-$HOME/.aisk}"
export AISKHUB_PROFILE_DIR="${AISKHUB_PROFILE_DIR:-$HOME/.aisk/profiles}"
mkdir -p "$AISKHUB_PROFILE_DIR"
cp agent-skills/templates/profile.example.yaml "$AISKHUB_PROFILE_DIR/my-project.yaml"
# 编辑 my-project.yaml：至少填写 project 和 repos；按需补充 envs

./bin/aisk profile
./bin/aisk doctor
```

当前目录同时匹配多个 profile 时，必须显式指定名称，例如：

```text
./bin/aisk --profile my-project profile
./bin/aisk --profile my-project doctor
```

### C. 授权用户再接入私有 Overlay

`aisk-private` 是可选的、需要单独授权的业务覆盖层；没有权限的同事只使用 A 流程，不要把私有
仓库内容复制到公开仓。为了让 `bin/aisk` 找到不在公开仓旁边的私有检出，使用
`AISKHUB_PRIVATE_ROOT`（不是 `AISK_PRIVATE_ROOT`，后者是启动器导出的运行时变量）：

```text
git clone https://github.com/fangxh2013/aisk-private.git /absolute/path/to/aisk-private
export AISKHUB_PRIVATE_ROOT=/absolute/path/to/aisk-private

./bin/aisk link claude codex antigravity workbuddy workbuddy-ai --dry-run
PYTHONPATH=agent-skills python3 tools/verify_spec.py \
  --private-manifest "$AISKHUB_PRIVATE_ROOT/OVERLAY_MANIFEST.yaml" \
  --overlay-lock "$AISKHUB_PRIVATE_ROOT/tools/overlay.lock.json" \
  --require-real --no-report-files
./bin/aisk link claude codex antigravity workbuddy workbuddy-ai
```

私有仓库的 `OVERLAY_MANIFEST.yaml` 必须与 `tools/overlay.lock.json` 的 `manifest_id`、公开内核
commit、内容摘要和 Overlay 摘要一致。锁文件失配时应停止分发并联系维护者，不能删除校验字段、
改用 placeholder 或手工把锁指向未经审核的公开 commit。

### D. 让 AI 按项目规则工作

把本仓库地址交给 AI 后，明确要求它先读取根 `README.md` 和
[`docs/AI-ONBOARDING.md`](docs/AI-ONBOARDING.md)，先执行 dry-run，再根据用户确认执行写入。
不同宿主不会因为收到一个 GitHub URL 就自动安装或自动读取所有适配器文档；该入口文件提供了
Codex、Claude、Antigravity、WorkBuddy 和 WorkBuddy AI 的共同操作契约。

授权用户的私有层从零安装、升级和回滚见
[`docs/PRIVATE-OVERLAY-USER-GUIDE.md`](docs/PRIVATE-OVERLAY-USER-GUIDE.md)；没有私有仓权限时，
只执行 A 流程，不能通过公开仓“推导”或绕过权限获得业务技能。

## 多 AI 协同与安全交付

先查任务再认领，不能为同一工作另开任务：

```text
./bin/aisk task find "关键词"
./bin/aisk task context T001
./bin/aisk task claim T001 --tool codex --session <会话号>
./bin/aisk task check T001
./bin/aisk task ready T001
./bin/aisk task land T001 --dry-run
./bin/aisk task land T001
./bin/aisk task promote T001 --repos main
```

任务目录内只允许在任务分支提交；任务内禁止 AI 直接 push、fetch、pull 或改写 Git 配置。
`land` 与 `promote` 根据 profile 的 `merge_policy.confirm_land` 决定是否弹出人工确认；建议
团队默认保持 `true`。所有确认标题都使用 `动作-工具｜任务号`，例如
`git推送-codex｜T001`、`git合并-claude｜T001`、`git推送-antigravity｜T001`。

WorkBuddy 在任务目录外执行面向公开远端的原始 `git push` 时，用户级 PreToolUse 守卫也会
拦截并请求确认；拒绝、身份不明或守卫异常均 fail-closed。该用户级钩子只追加 aisk 自己的
精确命令，不覆盖已有配置；可重复执行 `aisk permit workbuddy workbuddy-ai`。

## 隐私与发布检查

公共仓库只保存通用规则、适配器、schema、脱敏测试和合成样例。业务代码、服务器/集群信息、
Nacos/数据库配置、真实账号、密钥、会话内容和企业知识必须留在私有 Overlay；敏感值建议使用
操作系统密钥链或 SOPS + age，仓库只保存引用和 schema。

推送前至少执行：

```text
./bin/aisk public verify
PYTHONPATH=agent-skills python3 tools/verify_spec.py \
  --private-manifest /absolute/path/to/aisk-private/OVERLAY_MANIFEST.yaml \
  --overlay-lock /absolute/path/to/aisk-private/tools/overlay.lock.json \
  --require-real --no-report-files
git diff --check
```

`public verify` 会检查当前工作树和可达公开历史；命中真实路径、内网地址、凭据字面量或私有
仓库标识时，必须先脱敏再提交。发布报告中的 `real_runtime_score` 只有真实客户端会话与
真实弹窗/钩子联调后才能填写；本地模拟分发、静态检查和临时目录 smoke test 不得冒充实机通过。

更完整的隐私边界、适配矩阵、迁移步骤和回滚流程见 `docs/PRIVACY.md`、`docs/ADAPTERS.md`、
`docs/AI-ONBOARDING.md`、`docs/PRIVATE-OVERLAY-USER-GUIDE.md`、`MIGRATION_PLAN_CODEX.md` 与
`docs/COORDINATION.md`。
