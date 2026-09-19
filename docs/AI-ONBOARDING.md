# AI Onboarding Contract

本文是把 `aisk-hub` GitHub 地址交给 Codex、Claude、Antigravity、WorkBuddy 或 WorkBuddy AI
后，AI 应遵守的公共入口契约。它不包含任何企业路径、服务器地址、凭据或业务知识。

> 来源：Codex。本文只定义公开可复核的接入边界，不替代目标工具的原生安全策略。

## 1. 先确认边界

AI 必须先读取根目录 `README.md`、本文和相关工具适配说明，再判断用户要做的是：

1. 只安装公开通用技能；
2. 接入用户自己的 profile；
3. 接入已获授权的私有 Overlay；
4. 修改内核、开发功能或维护文档。

没有明确授权时，先只读盘点和 dry-run，不写用户配置、不提交、不推送、不访问数据库/Nacos/K8s/Jenkins，
也不猜测用户的项目路径和环境。

## 2. 推荐执行顺序

```text
./bin/aisk link claude codex antigravity workbuddy workbuddy-ai --dry-run
./bin/aisk skill list
./bin/aisk public verify
```

用户确认安装后：

```text
./bin/aisk link claude codex antigravity workbuddy workbuddy-ai
./bin/aisk permit --dry-run
./bin/aisk permit
```

`link` 和 `permit` 都可能写入用户目录，必须在回答中说明影响范围。若只需查看路由，使用：

```text
./bin/aisk skill resolve <旧技能名>
./bin/aisk skill inspect <旧技能名>
```

## 3. 私有 Overlay 规则

私有仓库只有在用户明确拥有访问权限时才可 clone 或读取。启动器使用：

```text
export AISKHUB_PRIVATE_ROOT=/absolute/path/to/aisk-private
```

`AISK_PRIVATE_ROOT` 是启动器导出的内部运行时变量，不应作为用户选择私有检出位置的入口。
Overlay 校验必须使用真实 manifest 和外置 lock：

```text
PYTHONPATH=agent-skills python3 tools/verify_spec.py \
  --private-manifest "$AISKHUB_PRIVATE_ROOT/OVERLAY_MANIFEST.yaml" \
  --overlay-lock "$AISKHUB_PRIVATE_ROOT/tools/overlay.lock.json" \
  --require-real --no-report-files
```

校验失败时停止分发，不能删除字段、改写 lock、联网拉取替代版本或把 placeholder 当成完成。

## 4. 工具归属与目标目录

| 工具 | 技能目标目录 | 会话/钩子注意事项 |
|---|---|---|
| Codex | `~/.agents/skills/` | 新会话读取最新技能；高危动作保留 Codex 归属 |
| Claude | `~/.claude/skills/` | 通过 Claude hooks/显式命令接入；缺少原生弹窗时走安全降级 |
| Antigravity | `~/.gemini/config/plugins/agent-skills/skills/` | 重启宿主后重新发现插件与 MCP 配置 |
| WorkBuddy | `~/.workbuddy/skills/` | 用户级 `.codebuddy/settings.json` 与任务钩子共同生效 |
| WorkBuddy AI | `~/.workbuddy-ai/skills/` | 与桌面版目录分离；运行期用配置目录区分归属 |

不要直接编辑这些目标目录中的受管技能；修改应回到本仓库后重新运行 `aisk link`。
端上未被 `.aisk-managed` 标记的第三方技能不应被回收。

## 5. 多 AI 协同与高危动作

协同任务必须先查重，再认领：

```text
./bin/aisk task find "关键词"
./bin/aisk task context T001
./bin/aisk task claim T001 --tool <codex|claude|antigravity|workbuddy|workbuddy-ai> --session <会话号>
```

任务目录内禁止 AI 直接 `git push`、`git fetch`、`git pull`、改写 Git 配置或直接操作主干。
交付使用 `check → ready → land → promote`。真正的合并/推送前必须由操作者确认。

确认标题必须包含动作、工具和任务号，例如：

```text
git提交-codex｜T001
git合并-claude｜T001
git推送-antigravity｜T001
git推送-workbuddy-ai｜T001
```

身份不明、会话号缺失、确认超时或守卫异常时 fail-closed。

## 6. 给 AI 的可复制启动提示

```text
请先读取该仓库的 README.md 和 docs/AI-ONBOARDING.md。
先执行 link --dry-run、skill list 和 public verify，只做只读检查。
不要猜测我的项目路径、私有仓库权限、服务器、凭据或环境。
任何写入用户配置、安装权限、提交、合并、推送或访问外部系统前，先说明影响并等待我确认。
```

## 7. 完成报告必须区分的结果

AI 交付时分别报告：

- `offline_contract_score`：公共契约和静态门禁；
- `real_file_score`：真实私有 manifest/lock；
- `real_runtime_score`：真实客户端会话、技能发现、钩子和弹窗联调。

本地 dry-run、临时目录 smoke test 或关键词路由回归不能冒充真实客户端通过。
