# Antigravity 适配规范

工具名：`antigravity`。为 Antigravity 宿主提供任务协同记账、macOS 原生弹窗契约、MCP 运行时及环境事实服务。

---

## 一、工具识别与会话模型

1. **进程识别**：
   在 `engine/worktree/actor.py` 中，`PROCESS_NAMES["antigravity"] = ("language_server",)`。内核向上遍历调用栈识别 `language_server` 进程特征，判定当前调用端为 Antigravity。
2. **会话 ID 注入**：
   宿主通过上下文注入会话标识 `conversationId`（如 `aa5ceee7-7a1d-4721-be38-c0419f3e6145`），内核将其作为 `owner_session` 记入协同状态机，实现会话级任务隔离与续租。
3. **协同状态与 Fencing Token**：
   Antigravity 认领任务（`claim`）时，SQLite 状态机分配严格单调递增的 `fencing_token`。所有后续心跳与状态迁移必须携带并校验该 Token，阻断网络分区或长时挂起恢复后的僵尸写覆盖。

---

## 二、人工确认与系统原生弹窗规范（macOS AppleScript）

在 Antigravity 中，凡涉及**合并主干（dev/master/main）、推送远端、落地代码（land/promote）、执行 DDL/SQL 迁移或生产发布等关键/高危操作**，必须通过系统原生弹窗（`/usr/bin/osascript`）向用户请求确认，严禁静默执行或仅在聊天会话中假定已授权。

### 1. 标题协议与调用范式
- **标题标准**：`动作-antigravity｜<任务号>`（如 `落地代码-antigravity｜T001`）
- **标准命令格式**：
  ```bash
  /usr/bin/osascript -e 'display dialog "<操作说明与影响范围>\n\n确认请点「<操作动作>」" with title "<操作动作>-antigravity｜<任务号>" buttons {"取消", "<操作动作>"} default button "<操作动作>" giving up after 600'
  ```
- **判定准则**：
  只有捕获到 `button returned:<操作动作>` 且 `gave up:false` 时才判定为用户真实授权并继续执行；若返回取消或超时则立即终止，保持原状。

---

## 三、Antigravity 原生 MCP Server 架构

Antigravity 通过 Stdio 换行分隔的 JSON-RPC 2.0 协议直连内核提供的 `aisk-mcp-server`（[`engine/mcp_server.py`](../../engine/mcp_server.py)）。

### 1. 暴露的 6 大结构化工具

| 工具名 | 类型 | 功能说明 | 典型返回语义 |
| :--- | :---: | :--- | :--- |
| **`aisk_task_inspect`** | 协同感知 | 只读查询 SQLite WAL 任务状态、租约到期时间、Fencing Token、持有者及挂起事件 | `live` / 结构化状态 |
| **`aisk_fact_service`** | 运维事实 | 查询微服务运维事实（镜像、Deployment、Job、DataId、NodePort），支持 `brief: true` 精简模式 | `offline_snapshot` |
| **`aisk_fact_entry`** | 运维事实 | 查询对外入口与映射端口（向后兼容保留，底层对齐 `fact_service(brief=True)`） | `offline_snapshot` |
| **`aisk_env_summary`** | 拓扑事实 | 查询环境基础拓扑、主机 IP、K8s 命名空间与服务全景 | `offline_snapshot` |
| **`aisk_repo`** | 路径反查 | 反查项目各代码仓库在当前物理机上的绝对路径 | 本地路径字典 |
| **`aisk_db_query`** | 托管查询 | 安全执行只读 SQL 查询，内置 1 秒 Socket Fail-Fast 离线探针与自动敏感字段脱敏 | `live` / `offline_unavailable` |

### 2. 数据可用性四态契约
MCP 返回严格遵循 `mcp-response.schema.json` 契约：
- `live`：实时真实网络/数据库连接（仅当数据库物理连接成功或直连 SQLite 状态库时返回）；
- `offline_cached`：经校验的本地缓存事实；
- `offline_snapshot`：静态部署清单（Manifest）事实快照；
- `offline_unavailable`：离线/断网不可达。**离线状态下绝不伪造任何虚假 SQL 查询结果**。

---

## 四、Profile 解析与 CWD 盲区应对

### 1. 痛点：宿主 CWD 失效
Antigravity 拉起 MCP 服务端进程时，工作目录为其自身应用目录（如 `~/.gemini/antigravity`），不在用户工作区内，导致传统的 `git rev-parse` 自动反查失效。

### 2. 智能回退解决机制
内核 `_resolve_prof()` 实现了多级智能回退链路：
1. **显式指定**：优先读取调用参数中的 `profile`，或环境变量 `AISK_PROFILE`；
2. **库名反查**：若传入 `database` 参数，利用库名前缀模式匹配所属 profile；
3. **单项目直选**：若本地仅配置了 1 个 profile，直接选定；
4. **运行时锚点感知**：自动检查 `~/.aisk-runtime/hub/tasks/anchors` 下是否有当前活跃项目的锚点目录；
5. **候选提示防护**：若仍存在歧义，绝不随意竞猜，主动返回可用 profile 候选列表供调用方明确选择。

---

## 五、Fail-Fast 数据库离线防护机制

为防止 Antigravity 在非内网或离线环境中调用 `aisk_db_query` 发生长达数十秒的 TCP 握手卡死，MCP 服务端引入了轻量级预检：
1. 在调用底层驱动前，先执行 `_probe_socket(host, port, timeout=1.0)`；
2. 若 1 秒内握手失败，立刻抛出 `OfflineDatabaseUnavailableError`；
3. 捕获后瞬间返回标准的 `offline_database_unavailable` JSON 契约，避免会话挂起转圈。

---

## 六、Schema 自动化同步

Antigravity 依赖静态 Schema 文件实现工具懒加载（Lazy Tool Loading）。

- **宿主 Schema 存放路径**：由宿主提供的 `$AISK_ANTIGRAVITY_MCP_DIR/*.json`（路径不得写入公共仓）
- **一键同步命令**：
  ```bash
  PYTHONPATH=agent-skills python3 agent-skills/engine/mcp_server.py --dump-schemas "$AISK_ANTIGRAVITY_MCP_DIR"
  ```
  该命令会自动将 `mcp_server.py` 的最新 `TOOLS` 定义同步生成为扁平 Schema，保持定义与代码 100% 同步。

---

## 七、调试与探针命令（复现验证）

### 1. MCP Stdio 通信探针
模拟 Antigravity 宿主，直接测试 MCP 服务端的初始化与任务感知工具：

```bash
# 1. 测试工具清单
printf '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n' | \
  PYTHONPATH=agent-skills ~/.aisk/venv/bin/python agent-skills/engine/mcp_server.py | jq .result.tools[].name

# 2. 测试任务状态查询 (aisk_task_inspect)
printf '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"aisk_task_inspect","arguments":{}}}\n' | \
  PYTHONPATH=agent-skills ~/.aisk/venv/bin/python agent-skills/engine/mcp_server.py | jq .
```

### 2. 原生弹窗探针
验证当前机器的 macOS AppleScript 弹窗链路：

```bash
/usr/bin/osascript -e 'display dialog "这是一次 Antigravity 弹窗链路连通性测试。\n\n请点击「确认测试」完成验收。" with title "探针测试-antigravity | TEST" buttons {"取消", "确认测试"} default button "确认测试" giving up after 10'
```
