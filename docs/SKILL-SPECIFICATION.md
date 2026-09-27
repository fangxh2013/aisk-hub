# aisk 领域技能短入口与 References 编写规范

本规范定义 `aisk-hub` 公共内核中 9 个 canonical 领域技能的写法。结构由 `./bin/aisk skill check`
（`agent-skills/engine/skill_router.py` 的 `SkillRouter.check`）机械校验，常驻体量由
[`spec/token-efficiency.yaml`](../spec/token-efficiency.yaml) 校验；本文与校验代码不一致时以代码为准，
并同步修正本文。

---

## 一、核心原则：短入口 + References 按需加载

> **严禁将 26 个旧技能的内容机械拼接成巨石 SKILL.md 文件。**

- **入口层（SKILL.md）**：只放职责、勿用与触发条件、工作方式要点和 references 索引。公共技能目前每个
  19 行，新增技能也应保持在几十行以内；
- **规程层（references/）**：按子能力拆分的 Markdown，只有任务真正需要时才由模型定向读取。

```text
agent-skills/skills/
└── <canonical-skill-name>/
    ├── SKILL.md                # 短入口
    └── references/             # 领域规程（按需加载）
        └── <workflow>.md
```

---

## 二、SKILL.md 结构（`aisk skill check` 校验）

```markdown
---
name: <canonical-skill-name>
description: <一句话职责>。适用于<场景>。勿用：<相邻场景>→<应改用的技能>。触发：<触发词，用顿号分隔>。
metadata:
  version: <语义化版本>
---

# <English Display Name>

## 工作方式

<两到三段：本领域的边界、默认步骤、验证要求与禁止的捷径。>

## 按需参考

- <什么时候读>见 [references/<workflow>.md](references/<workflow>.md)。
```

校验项：

1. 以 frontmatter 开头，`name` 与目录名一致；
2. `description` 写在一行，同时包含「勿用：」和「触发：」——宿主靠它在相邻技能之间做选择，
   缺了「勿用」就会和相邻技能抢触发；
3. 正文包含 `## 工作方式` 与 `## 按需参考` 两节；
4. `## 按需参考` 链接的每个 `references/*.md` 都存在，`references/` 下的每个 `.md` 都被链接，
   不留死链，也不留没人索引的孤儿文件；
5. SKILL.md 少于 500 行，技能目录与文件都不能是符号链接。

跨领域的规则各有归属：秘密不进日志与响应、高风险动作的确认与最小权限在 `security`；需求澄清、
最小可回滚修改与交付验证在 `engineering-discipline`；任务租约、保护分支与交付门禁在 `ai-worktree`。
领域技能的「工作方式」只写本领域特有的约束（例如 `ops-workbench` 的默认只读），不重复抄写这些规则；
常驻入口之间的重复还会被 `max_duplicate_line_ratio` 拦下。

---

## 三、9 个 canonical 技能与旧技能映射

别名以 [`spec/skill-migration-map.yaml`](../spec/skill-migration-map.yaml) 为准，内核 `SkillRouter`
据此把旧名称路由到 canonical 技能。

| 领域技能 | 显示名 | 纳管的旧技能与别名 | references |
| :--- | :--- | :--- | :--- |
| **`web-engineering`** | Web Engineering | `dev-web-code`, `vue3-crud-page`, `element-plus-patterns`, `state-management`, `frontend-review`, `frontend-testing` | `frontend-workflow.md` |
| **`backend-engineering`** | Backend Engineering | `dev-code`, `api-design`, `ruoyi-module-scaffold`, `backend-testing` | `service-workflow.md` |
| **`ops-workbench`** | Operations Workbench | `ops`, `k3s-deployment`, `docker-build`, `jenkins-ci` | `operations-workflow.md` |
| **`engineering-discipline`** | Engineering Discipline | `brainstorming`, `change-safety`, `systematic-debugging`, `pre-commit-review`, `dev-rules`, `git-flow` | `change-workflow.md` |
| **`db-workbench`** | Database Workbench | `database-design`, `db-design`, `db-schema-backup` | `database-workflow.md` |
| **`security`** | Security | `security-review`, `sec-review` | `security-review.md` |
| **`ai-worktree`** | AI Task Workspace | `ai-worktree` | `task-workflow.md` |
| **`dev-release`** | Development Release | `dev-release` | `release-checklist.md` |
| **`prod-log-analysis`** | Production Log Analysis | `prod-log-analysis` | `log-analysis.md` |

---

## 四、Token 预算与质量门禁

`spec/token-efficiency.yaml` 的 `public_instruction_files` 列出常驻加载的技能入口（当前是
`ai-worktree`、`engineering-discipline`、`security` 三个），`aisk contract tokens` 按「字符数 ÷ 4」
保守估算并校验：

1. **单个常驻入口上限**：每个文件不超过 `max_single_skill_tokens: 2200`（约 8800 个字符）；
2. **单任务上下文预算**：选中的入口加上 `fixed_envelope_tokens: 600` 不超过 `max_task_context_tokens: 4500`；
3. **重复行比例**：常驻入口之间 24 个字符以上的行，重复比例不超过 `max_duplicate_line_ratio: 0.20`；
4. **常驻文件数量**：不超过 `max_policy_files: 12`。

省 Token 不能删除权限、隐私、高危操作和验证门禁。

---

## 五、开源与隐私边界

1. 公共技能中**严禁包含任何特定企业的专有业务属性**（例如具体公司名、内部系统域名、真实 IP、Nacos DataId、
   专有微服务模块包名）；
2. 业务定制必须通过 `aisk-private` 的 **Slot Overlay v2** 注入，公共技能只保留通用占位符
   （如 `<由私有 Overlay 注入>`）。
