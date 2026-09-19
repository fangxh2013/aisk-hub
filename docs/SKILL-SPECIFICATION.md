# aisk 领域技能短入口与 References 编写规范

本规范是 `aisk-hub` 公共内核中 9 大领域规范技能的唯一定义标准。所有技能开发、重构与迁移必须严格服从本规范。

---

## 一、核心原则：短入口 + References 按需加载

> **严禁将 26 个旧技能的内容机械拼接成巨石 SKILL.md 文件。**

面向 2026 年最新大模型（GPT-6/5.6、Claude 3.5/Opus 5、Gemini 3.8 Flash/Pro、DeepSeek V4.1）：
- **常驻/索引层（SKILL.md）**：保持极小体量（< 100 行），负责场景路由、前置规则绑定、安全门禁声明与 references 索引；
- **规程/深水层（references/）**：按功能子域拆分为独立的 Markdown 参考文件，只有在任务实际需要该子能力时，才由模型定向读取。

```text
skills/
└── <canonical-skill-name>/
    ├── SKILL.md                # 短入口（严格遵循 6 要素）
    └── references/             # 领域规程（按需加载）
        ├── submodule-a.md
        ├── submodule-b.md
        └── best-practices.md
```

---

## 二、SKILL.md 短入口标准结构（必须具备的 6 大要素）

每一个规范技能的 `SKILL.md` 必须严格包含以下 6 个小节，缺一不可：

```markdown
# <技能显示名称> (<canonical-skill-name>)

<一句话核心职责定义>

## 1. 适用场景与触发词 (Triggers)
- 触发场景：明确指出用户在执行什么任务时激活本技能；
- 兼容别名：明确列出支持的旧技能名称（由内核 SkillRouter 自动映射）。

## 2. 前置项目规则 (Prerequisites)
- 必须声明优先读取当前工作区的 AGENT-WORKTREE.md 与 AGENTS.md；
- 声明业务专有参数由私有 Overlay 插槽（Slot Overlay）注入，内核不硬编码业务实体。

## 3. 按需参考指引 (References)
- [子领域 A 规范](references/sub-a.md)：处理 A 类场景时读取；
- [子领域 B 规范](references/sub-b.md)：处理 B 类场景时读取。

## 4. 安全与合规门禁 (Safety Gates)
- 声明只读铁律（禁止未经授权的线上/DB 写操作）；
- 声明敏感数据脱敏要求；
- 声明关键高危操作必须唤起原生系统弹窗确认。

## 5. 最小验证命令 (Verification Commands)
- 声明离开本技能前必须执行的最小验证门禁（如编译、单测或静态检查命令）。

## 6. 禁止事项清单 (Zero-Tolerance Rules)
- 明确列出本领域内的绝对红线（如禁止物理外键、禁止盲改代码、禁止绕过门禁）。
```

---

## 三、9 大领域规范技能划分与 26 旧技能映射表

| 领域规范技能 | Display Name | 纳管的旧技能映射源 (Aliases) | 核心 References 拆分建议 |
| :--- | :--- | :--- | :--- |
| **`web-engineering`** | 前端工程闭环 | `dev-web-code`, `element-plus-patterns`, `vue3-crud-page`, `state-management`, `frontend-review`, `frontend-testing` | `element-plus.md`, `vue3-crud.md`, `pinia-state.md`, `vitest.md` |
| **`backend-engineering`** | 后端微服务工程 | `dev-code`, `api-design`, `ruoyi-module-scaffold`, `backend-testing` | `ruoyi-cloud.md`, `api-design.md`, `mybatis-plus.md`, `junit5-mockito.md` |
| **`ops-workbench`** | 环境运维工作台 | `ops`, `k3s-deployment`, `docker-build`, `jenkins-ci` | `k8s-pod-debug.md`, `jenkins-pipeline.md`, `dockerfile.md`, `nacos-config.md` |
| **`engineering-discipline`** | 研发纪律方法论 | `brainstorming`, `change-safety`, `systematic-debugging`, `pre-commit-review`, `dev-rules`, `git-flow` | `clarify-stage.md`, `safety-stage.md`, `debugging-stage.md`, `pre-commit-review.md` |
| **`db-workbench`** | 数据库建模对齐 | `database-design`, `db-schema-backup` | `mysql-standards.md`, `ddl-export-align.md` |
| **`security`** | 安全代码防御门禁 | `security-review` (独立门禁) | `idor-defense.md`, `sql-injection.md`, `data-masking.md` |
| **`ai-worktree`** | 多AI并行协同调度 | `ai-worktree` (独立调度) | `state-machine.md`, `lease-fencing.md`, `cross-machine-handoff.md` |
| **`dev-release`** | 发版受影响分析 | `dev-release` (独立门禁) | `diff-analysis.md`, `ddl-aggregation.md` |
| **`prod-log-analysis`** | 生产日志归因探针 | `prod-log-analysis` (独立分析) | `elk-clustering.md`, `git-blame-attribution.md` |

---

## 四、Token 预算与质量门禁

所有技能文件必须满足 [`spec/token-efficiency.yaml`](../spec/token-efficiency.yaml) 的自动化门禁测试：

1. **单技能入口上限**：`max_single_skill_tokens: 2200`（字符数 < 8800）；
2. **单任务上下文预算**：`max_task_context_tokens: 4500`（含常驻规则、入口技能与 1 个 reference）；
3. **文本重复行比例**：`max_duplicate_line_ratio: 0.20`（严禁在多个技能间大量复制粘贴同义车轱辘话）；
4. **常驻技能数量上限**：`max_policy_files: 12`。

---

## 五、开源与隐私边界

1. 公共技能中**严禁包含任何特定企业的专有业务属性**（例如具体公司名、内部系统域名、真实 IP、Nacos DataId、专有微服务模块包名）；
2. 业务定制必须通过 `aisk-private` 的 **Slot Overlay v2** 进行注入，公共技能仅保留通用占位符（如 `<由私有 Overlay 注入>`）。
