# Token Runtime 适配契约

本文档定义四个宿主适配层如何消费公共 Token Runtime 契约。适配层只声明发现路径、会话来源、宿主能力和默认预算；预算计算、裁剪顺序、去重和安全门禁仍由公共运行时负责。适配层不得复制核心策略或把宿主的 tokenizer 估算伪装成已验证事实。

## 共同契约

每个 `agent-skills/adapters/*/token-policy.yaml` 都必须声明：

- `contract_source: public-token-runtime` 和 `do_not_duplicate_core_policy: true`；
- 发现路径、会话能力与会话来源；
- 支持的 `lite`、`standard`、`deep`、`emergency` mode 及默认预算；
- `fail_to_full` 行为：预算解析或裁剪失败时回退 `emergency` 完整允许上下文，但仍保留安全、隐私、验证和授权门禁；
- `verification.status: offline_contract`、`runtime_status: dry-run`、`real_client_validation: not_integrated`，除非有真实宿主联调证据。

`default_tokens: 4500` 与公共离线门禁的 `max_task_context_tokens: 4500` 对齐；`emergency` 是公共运行时定义的完整允许上下文，不是无限制读取，也不等于跳过门禁。`fail_to_full` 只处理上下文预算失败，身份缺失或高危授权失败必须 `fail_closed`。

## 四工具配置示例

以下示例与各适配器策略文件保持一致：

```yaml
# Codex: agent-skills/adapters/codex/token-policy.yaml
tool: codex
discovery:
  project_path: .codex/hooks.json
session:
  capability: structured
  sources: [CODEX_THREAD_ID, CODEX_SESSION_ID, explicit_session_argument]
modes: {supported: [lite, standard, deep, emergency], default: standard}
budget: {default_tokens: 4500, max_tokens: 4500}
fail_to_full: {enabled: true, on_budget_error: emergency}
```

```yaml
# Claude: agent-skills/adapters/claude/token-policy.yaml
tool: claude
discovery:
  project_path: .claude/settings.json
session:
  capability: structured
  sources: [CLAUDE_CODE_SESSION_ID, CLAUDE_SESSION_ID, explicit_session_argument]
modes: {supported: [lite, standard, deep, emergency], default: standard}
budget: {default_tokens: 4500, max_tokens: 4500}
fail_to_full: {enabled: true, on_budget_error: emergency}
```

```yaml
# Antigravity: agent-skills/adapters/antigravity/token-policy.yaml
tool: antigravity
discovery:
  project_path: .agents/hooks.json
  mcp_config: host_managed
session:
  capability: structured
  sources: [conversationId, explicit_session_argument]
modes: {supported: [lite, standard, deep, emergency], default: standard}
budget: {default_tokens: 4500, max_tokens: 4500}
fail_to_full: {enabled: true, on_budget_error: emergency}
```

```yaml
# WorkBuddy: agent-skills/adapters/workbuddy/token-policy.yaml
tool: workbuddy
discovery:
  project_path: .codebuddy/settings.json
  mcp_config: host_managed
  variant_resolution: runtime_from_WORKBUDDY_CONFIG_DIR
session:
  capability: structured
  sources: [CODEBUDDY_SESSION_ID, WORKBUDDY_CONFIG_DIR, explicit_session_argument]
modes: {supported: [lite, standard, deep, emergency], default: standard}
budget: {default_tokens: 4500, max_tokens: 4500}
fail_to_full: {enabled: true, on_budget_error: emergency}
```

这些示例只展示适配层输入，不重新实现公共运行时的预算算法。四工具的真实客户端发现、真实 tokenizer、上下文注入和弹窗链路均尚未联调；当前仅可报告离线契约和 dry-run 结果。

## 弹窗标题归属

所有高危 Git 操作（尤其推送、合并主干、落地和发布）使用公共模板：

```text
{action}-{tool}｜{task_id}
```

T006 的推送标题必须分别呈现为：`git推送-codex｜T006`、`git推送-claude｜T006`、`git推送-antigravity｜T006`、`git推送-workbuddy｜T006`。工具身份、任务号或用户确认缺失时，不得生成看似已归属的标题，也不得静默放行高危动作。

## 安全边界

- Token 预算不是权限；缩短上下文不能删除身份、隐私、高危操作或验证要求。
- `fail_to_full` 只允许公共运行时回退到 `emergency` 完整允许上下文；不能绕过 `fail_closed`、任务租约、弹窗确认或任务交付门禁。
- 适配层只读取已声明的项目/用户发现路径和会话标识，不写入提示词、业务数据、凭据或真实 Token。
- 宿主能力未联调时只能使用 `dry-run`、本地快照或显式命令，并必须保持未验证标记；离线契约分不得表述为真实运行分。
- `emergency` 仍受公共契约、隐私扫描、任务范围和宿主权限约束；适配器不能自行扩大范围。
