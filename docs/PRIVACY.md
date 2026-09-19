# 隐私与开源边界

公开包只包含通用内核、公开模板、适配接口和脱敏测试。以下内容留在本机私有层：业务代码、服务器/集群地址、配置正文、真实凭据、会话历史、企业知识和任务在制品。

公共边界不是“把值替换成 placeholder 就能发布”：公开文件不得携带真实私有路径、内网地址、
域名、凭据或业务标识；可以公开字段名称、枚举、相对路径形状、摘要和无业务含义的合成样例。
私有 Overlay 的 manifest 与 lock 只作为运行时输入，不能复制到公共仓或证据报告正文。

推荐格式：

- 非敏感结构：公开 YAML/JSON 模板，只保留字段形状和占位符；
- 机密值：SOPS + age 或操作系统密钥链，仓库只保存引用和 schema；
- 企业知识：本地 Markdown/SQLite 私有知识包，公开版只保留接口契约；
- 审计：JSONL 只记工具、动作、任务号、会话号、结果和时间，不写提示词、SQL、Token 或业务数据。

发布前运行：

```text
./bin/aisk public verify
./bin/aisk public export --dest /path/to/public-candidate
```

`public verify` 同时检查工作树和公开发布 ref（`master` 与显式公开 Tag）的全历史；本地 `aisk task` 运行分支不属于公开发布面。`public scan` 只做工作树快速检查。
公开候选使用显式 allowlist，只导出通用内核、适配接口、公开模板和最小回归。出现命中时必须先脱敏，不能用参数绕过门禁。

删除规则：旧 alias、私有 manifest 或 lock 的引用未清零前不删除；删除前要保留带摘要的备份、
完成离线和真实文件校验，并经过一个可回滚发布窗口。回滚时恢复上一份 manifest/lock 与 alias
映射，重新校验摘要和工作树；不通过强制覆盖、历史改写或静默降级来回滚。

验收分数分为三层：`offline_contract_score`（公共静态契约）、`real_file_score`（私有 manifest
与外置 lock）和 `real_runtime_score`（真实客户端/外部系统联调）。没有真实客户端证据时必须报告
`runtime_status: offline-only`；离线契约通过不等于四工具已加载，也不等于 DEV/PROD 可达。

公开 Git 历史中的旧私有路径命中仍是发布门禁问题；本任务只清理当前工作树，不改写历史、不
强制重置、不推送。发现该命中时验收必须保留失败证据，待单独批准的历史清理阶段处理。
