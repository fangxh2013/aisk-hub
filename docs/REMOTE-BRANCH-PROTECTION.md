# 远端分支保护与本地确认的闭环

本项目的本地弹窗解决“AI 是否得到本次操作授权”，不能解决“一个全新的 clone 是否能绕过本地钩子”。
要防止临时 clone、脚本或人工误操作直接写入 `dev`，必须同时配置 GitHub/GitLab 等远端服务的分支保护。

## 推荐策略

以新华项目的分支命名为例：

| 分支 | 日常 commit/push | 合并或远端 push | 服务端要求 |
| --- | --- | --- | --- |
| `fxh` | 默认允许 | 默认允许 | 不设为 protected |
| `fxh-dev` | 默认允许 | 默认允许 | 不设为 protected，或按团队审查需要保护 |
| `dev` | 允许在本地形成候选 | 必须 `git合并-<工具>` / `git推送-<工具>` 确认 | 禁止直接 push，只允许 PR 或受控机器人 |
| `main` / `master` | 默认不作为 AI 集成面 | 必须人工确认 | 禁止直接 push、禁止 force push |

profile 中的 `repo.trunk` 等价于受保护主干；若项目采用其他个人分支名，只需把
`integration_branch` 和 `push_branch` 写入私有 profile，不要把企业名称、远端 URL 或凭据写入公共仓库。

## GitHub 设置清单

仓库管理员在 **Settings → Branches → Add branch ruleset**（或旧版 Branch protection rules）中为
`dev`、`main`、`master` 分别配置：

1. Require a pull request before merging。
2. Require approvals；至少 1 个团队成员或指定 CODEOWNERS。
3. Require status checks to pass before merging；选择构建、测试和隐私扫描工作流。
4. Require conversation resolution；禁止 force pushes；禁止 branch deletion。
5. Restrict who can push：不包含普通 AI 使用者；仅保留受控 CI/发布机器人（如确有需要）。
6. Do not allow bypassing the above settings；管理员也应遵守规则，除非有单独的紧急流程。

个人 `fxh` / `fxh-dev` 不要套用禁止直推规则，否则会重新把日常开发误变成“每次 push 都弹窗/走主干流程”。

## 可审计验收

至少用一个临时 clone 做两组验证，并把结果作为仓库管理员的变更记录：

```text
# 应成功：个人分支
git push origin HEAD:fxh-dev

# 应被远端拒绝：受保护分支（本地确认不能替代服务端拒绝）
git push origin HEAD:dev
git push --force origin HEAD:dev
```

在已安装 aisk 守卫的工作区，再验证：

```text
# 不弹窗：个人分支
git push origin HEAD:fxh-dev

# 弹窗标题必须含工具名；取消后命令不应执行
git push origin HEAD:dev
git merge dev
```

验证证据应包含：规则截图或导出、远端拒绝输出、`dialog_audit.jsonl` 的脱敏元数据、提交号和时间。
不得上传业务代码、服务器地址、访问令牌、完整远端 URL 或提示词全文。

## 失败模式与处理

- **本地弹窗出现但临时 clone 仍能 push `dev`**：远端保护没有生效，立即停止发布并修复服务端规则。
- **`fxh` 也弹窗**：检查旧 profile 是否把 `fxh` 错列在 `protected`；当前内核会优先按 integration/push_branch
  识别个人面，也可设置 `merge_policy.confirm_personal_push: true` 显式恢复确认。
- **Windows 没有弹窗**：确认当前进程位于交互桌面、PowerShell/WinForms 可用；无图形会话必须拒绝，不能退化成静默执行。
- **任务 worktree 直接 push**：这是任务隔离违规，应回到 `check → ready → land → promote`；个人分支放行只适用于任务外日常开发工作区。

本地守卫、任务状态机和远端 branch protection 缺一不可；只有三者同时通过，才可称为完整交付闭环。
