# -*- coding: utf-8 -*-
"""Antigravity 原生 MCP Server (Model Context Protocol).

纯 Python 标准库实现，零第三方依赖。通过 stdio 承载 JSON-RPC 2.0 协议，
向 Antigravity 等 AI 客户端直接暴露结构化工具：
  - aisk_task_inspect: 只读查看多 AI 协同任务状态（SQLite WAL）、租约与事件
  - aisk_fact_service: 服务事实（镜像/CI Job/DataId/端口/命名空间，支持 brief 精简）
  - aisk_fact_entry: 服务入口与端口（向后兼容保留，底层对齐 fact_service）
  - aisk_env_summary: 环境拓扑与配置概览
  - aisk_db_query: 托管只读 SQL 查询（Fail-Fast 离线探针 + 只读校验 + 自动脱敏）
  - aisk_repo: 获取已注册仓库的绝对路径
"""

import json
import os
import socket
import sqlite3
import sys
from pathlib import Path

# 确保 engine 模块可导入
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine import dberrors, facts, mcp_contract, profile, schema, secrets  # noqa: E402
from engine.brokers import db as db_broker  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aisk-mcp-server"
SERVER_VERSION = "1.1.0"

TOOLS = [
    {
        "name": "aisk_task_inspect",
        "description": "只读查看多AI协同任务状态机、租约与挂起事件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "任务ID（如 T001），空则返回任务列表",
                },
                "limit": {
                    "type": "integer",
                    "description": "最大条数，默认 10",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "aisk_fact_service",
        "description": "查询微服务运维事实（镜像/CI Job/DataId/端口等）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "微服务名称",
                },
                "env": {
                    "type": "string",
                    "description": "环境名（dev/pre/prod），默认 dev",
                    "default": "dev",
                },
                "brief": {
                    "type": "boolean",
                    "description": "精简模式只返入口端口，默认 false",
                    "default": False,
                },
                "profile": {
                    "type": "string",
                    "description": "MCP 项目名",
                },
            },
            "required": ["service"],
        },
    },
    {
        "name": "aisk_fact_entry",
        "description": "查询微服务的对外入口类型与映射端口。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "微服务名称",
                },
                "env": {
                    "type": "string",
                    "description": "环境名，默认 dev",
                    "default": "dev",
                },
                "profile": {
                    "type": "string",
                    "description": "MCP 项目名",
                },
            },
            "required": ["service"],
        },
    },
    {
        "name": "aisk_env_summary",
        "description": "获取环境拓扑与配置（IP/命名空间/服务清单等）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "env": {
                    "type": "string",
                    "description": "环境名（dev/pre/prod），默认 dev",
                    "default": "dev",
                },
                "brief": {
                    "type": "boolean",
                    "description": "是否仅返回核心拓扑，默认 false",
                    "default": False,
                },
                "profile": {
                    "type": "string",
                    "description": "MCP 项目名",
                },
            },
        },
    },
    {
        "name": "aisk_repo",
        "description": "获取已注册代码仓或部署仓的本地绝对路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "仓库别名，为空返回全部映射",
                },
                "profile": {
                    "type": "string",
                    "description": "MCP 项目名",
                },
            },
        },
    },
    {
        "name": "aisk_db_query",
        "description": "安全执行只读 SQL 查询，自动拦截写操作并脱敏。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "只读 SQL 语句（SELECT/SHOW/DESC/EXPLAIN）",
                },
                "env": {
                    "type": "string",
                    "description": "环境（dev/pre/prod），默认 dev",
                    "default": "dev",
                },
                "database": {
                    "type": "string",
                    "description": "数据库名",
                },
                "limit": {
                    "type": "integer",
                    "description": "最大行数 1-500，默认 100",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "确认查询目标，生产必填 true",
                    "default": False,
                },
                "no_redact": {
                    "type": "boolean",
                    "description": "是否关闭脱敏，默认 false",
                    "default": False,
                },
                "profile": {
                    "type": "string",
                    "description": "MCP 项目名",
                },
            },
            "required": ["sql"],
        },
    },
]


class ToolError(Exception):
    """可向调用方报告的工具执行失败。"""


class OfflineDatabaseUnavailableError(ToolError):
    """网络探测失败或不可达，快速降级为 offline_database_unavailable。"""


def _probe_socket(host, port=3306, timeout=1.0):
    """快速探测主机端口连通性（默认 1 秒超时），fail-fast 避免长时阻塞。"""
    if not host or str(host).strip() in {"localhost", "127.0.0.1"}:
        return True
    try:
        with socket.create_connection((str(host), int(port)), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def _resolve_prof(explicit=None, db_hint=None):
    """MCP 下的 profile 解析。

    **cwd 在这里是死信号**：AI 工具拉起 MCP 服务端时用的是它自己的目录，
    从来不在用户仓库内，所以 profile.resolve 的 git 反查必然落空。
    db_hint（调用方给的库名）往往是唯一可用的线索——见 profile.match_by_database。

    智能回退策略：
    1. 优先采用 explicit 显式指定，或 AISK_PROFILE 环境变量；
    2. 若有 db_hint，通过库名前缀反查对应 profile；
    3. 检查系统单 profile 场景，若全局仅有 1 个 profile，直接使用；
    4. 检查 ~/.aisk-runtime/hub/tasks/anchors 中是否有活跃项目的锚点目录；
    5. 若均无法解析，返回清晰的候选列表供调用方明确选择，杜绝静默乱连。
    """
    if not explicit:
        explicit = os.environ.get("AISK_PROFILE")
    try:
        prof, _ = profile.resolve(explicit=explicit, db_hint=db_hint)
        return prof
    except profile.ProfileError:
        if explicit or db_hint:
            raise
        profiles = profile.list_profiles()
        if len(profiles) == 1:
            return profile.load(profiles[0])
        # 尝试检查活跃工作区锚点
        runtime_anchor = Path.home() / ".aisk-runtime" / "hub" / "tasks" / "anchors"
        if runtime_anchor.is_dir():
            for p in profiles:
                if (runtime_anchor / p.stem).exists():
                    return profile.load(p)
        names = [p.stem for p in profiles]
        cand_str = f"（现有候选: {', '.join(names)}）" if names else ""
        raise ToolError(f"未指定 profile 且无法自动推导项目{cand_str}，请在参数中指定 profile。")


def handle_task_inspect(args):
    """只读查询当前任务状态机（SQLite WAL）、租约与 pending 协同事件。"""
    task_id = args.get("task_id")
    limit = int(args.get("limit", 10))

    state_db_path = os.environ.get("AISK_STATE_DB")
    if not state_db_path:
        candidates = [
            Path.home() / ".aisk-runtime" / "hub" / "tasks" / "state.db",
            Path.home() / ".aisk" / "state.db",
        ]
        for cand in candidates:
            if cand.is_file():
                state_db_path = cand
                break
    if not state_db_path or not Path(state_db_path).is_file():
        raise ToolError("未找到本地多 AI 任务状态机数据库 (state.db)。当前无活跃协作工作区。")

    db = sqlite3.connect(str(state_db_path), timeout=3)
    db.row_factory = sqlite3.Row
    try:
        if task_id:
            row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise ToolError(f"未找到任务: {task_id}")
            pending = db.execute(
                "SELECT COUNT(*) FROM outbox_events WHERE task_id = ? AND exported_at IS NULL",
                (task_id,),
            ).fetchone()[0]
            lines = [
                f"task_id: {row['task_id']}",
                f"status: {row['status']}",
                f"version: {row['version']}",
                f"fencing_token: {row['fencing_token']}",
                f"owner_tool: {row['owner_tool'] or '(none)'}",
                f"owner_session: {row['owner_session'] or '(none)'}",
                f"lease_until: {row['lease_until'] or '(none)'}",
                f"updated_at: {row['updated_at']}",
                f"pending_outbox_events: {pending}",
            ]
            return "\n".join(lines)
        else:
            rows = db.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            if not rows:
                return "当前协同状态机中暂无任务。"
            res = []
            for r in rows:
                owner = f"{r['owner_tool']}:{r['owner_session'][:8]}..." if r['owner_tool'] and r['owner_session'] else "(unassigned)"
                lease = f"lease_until={r['lease_until']}" if r['lease_until'] else "no-lease"
                res.append(f"[{r['task_id']}] {r['status']} | token={r['fencing_token']} | owner={owner} | {lease} | updated={r['updated_at']}")
            return "\n".join(res)
    finally:
        db.close()


def handle_fact_service(args):
    service = args.get("service")
    env_name = args.get("env") or "dev"
    brief = bool(args.get("brief", False))
    prof = _resolve_prof(args.get("profile"))
    name, _env = profile.get_env(prof, env_name)
    mdir = profile.manifest_dir(prof, name)
    if not mdir:
        raise ToolError(f"env={name} 未声明 manifests 目录或目录不存在")
    f = facts.service_facts(
        mdir,
        service,
        jdir=profile.env_path(prof, name, "jenkins_jobs"),
        cdir=profile.env_path(prof, name, "nacos_configs"),
    )
    if not f:
        svcs = facts.list_services(mdir)
        avail = f"（可用服务: {', '.join(svcs)}）" if svcs else ""
        raise ToolError(f"env={name} 未找到服务 '{service}' {avail}")
    if brief:
        return f"{f['service']} type={f['service_type']} nodePort={f['node_port']} ports={','.join(f['ports']) or '-'}"
    lines = [
        f"service: {f['service']}",
        f"namespace: {f['namespace']}",
        f"deployment: {f['deployment']}",
        f"image: {f['image']}",
    ]
    if f.get("gray_version"):
        lines.append(f"gray_version: {f['gray_version']}")
    lines.append(f"entry: type={f['service_type']} nodePort={f['node_port']} ports={','.join(f['ports']) or '-'}")
    if f.get("jenkins_job"):
        lines.append(f"jenkins_job: {f['jenkins_job']}")
    if f.get("nacos_dataid"):
        lines.append(f"nacos_dataid: {f['nacos_dataid']}")
    lines.append(f"manifest: {f['manifest']}")
    return "\n".join(lines)


def handle_fact_entry(args):
    """向后兼容保留；底层对齐 handle_fact_service(brief=True)。"""
    return handle_fact_service({**args, "brief": True})


def handle_env_summary(args):
    env_name = args.get("env") or "dev"
    brief = args.get("brief", False)
    prof = _resolve_prof(args.get("profile"))
    name, env = profile.get_env(prof, env_name)
    lines = facts.env_summary(prof, name, env)
    if not brief:
        mdir = profile.manifest_dir(prof, name)
        if mdir:
            svcs = facts.list_services(mdir)
            lines.append(f"services({len(svcs)}): {' '.join(svcs)}" if svcs else "services: (空)")
    return "\n".join(lines)


def handle_repo(args):
    prof = _resolve_prof(args.get("profile"))
    repos = prof.get("repos") or {}
    name = args.get("name")
    if not name:
        res = {k: str(profile._expand(v)) for k, v in repos.items()}
        return json.dumps(res, ensure_ascii=False, indent=2)
    if name not in repos:
        raise ToolError(f"profile {prof['project']} 没有仓库 '{name}'。现有: {', '.join(repos)}")
    return str(profile._expand(repos[name]))


def _readonly_query_fn(dbcfg, pwd):
    """给 schema 模块注入的只读查询函数。口令留在闭包里，不进 schema 模块。"""
    def run(sql):
        return db_broker.query(
            sql,
            host=dbcfg.get("host") or dbcfg.get("vip"),
            port=dbcfg.get("port", 3306),
            user=dbcfg.get("user"),
            password=pwd,
            mysql_bin=dbcfg.get("mysql_bin"),
            max_rows=500,
            redact=False,
        )
    return run


def handle_db_query(args):
    sql = args.get("sql", "").strip()
    env_name = args.get("env") or "dev"
    database = args.get("database")
    limit = int(args.get("limit", 100))
    # MCP 是模型进程的非交互通道，禁止通过参数关闭脱敏；只读结果必须保持安全。
    no_redact = False

    try:
        db_broker.assert_readonly(sql)
    except db_broker.BrokerError as e:
        raise ToolError(f"只读安全策略拒绝: {e}") from e

    try:
        prof = _resolve_prof(args.get("profile"), db_hint=database)
        name, env = profile.get_env(prof, env_name)
    except Exception as e:
        raise ToolError(f"获取环境配置失败: {e}") from e

    if env.get("require_confirm") and args.get("confirmed") is not True:
        raise ToolError(f"{name} 环境要求用户确认本次目标后再以 confirmed=true 调用")

    dbcfg = env.get("db") or {}
    if not dbcfg:
        raise ToolError(f"{name} 环境未配置数据库连接信息")

    # env 和库名前缀对不上就拦住。这不是洁癖：连的是 env 指定的服务器，用的却是
    # 另一个环境的库名，轻则报库不存在，重则「以为在查 dev、其实连着 prod」。
    prefix = dbcfg.get("prefix") or ""
    if database and prefix and not database.startswith(prefix):
        elsewhere = [e for e, pre in profile.db_prefixes(prof) if database.startswith(pre)]
        if elsewhere:
            raise ToolError(f"环境和库名对不上：env={name} 的库前缀是 {prefix}，"
                    f"但库 {database} 属于 {elsewhere[0]} 环境。\n"
                    f"   要查 {database} 就用 env={elsewhere[0]}；"
                    f"要查 {name} 就换成 {prefix}* 的库。**这类错配拒绝自动纠正。**")

    key = dbcfg.get("secret")
    if not key:
        raise ToolError(f"{name}.db 未声明 secret 凭据键名")

    pwd = secrets.get(key, privileged=False)
    if not pwd:
        raise ToolError(f"未能读取只读口令 (凭据键: {key})")

    # Fail-fast 轻量网络探测（1 秒超时）：避免断网或非内网时长时间挂起阻塞 AI 交互
    host = dbcfg.get("host") or dbcfg.get("vip")
    port = int(dbcfg.get("port", 3306))
    if host and not _probe_socket(host, port, timeout=1.0):
        raise OfflineDatabaseUnavailableError(f"数据库主机 {host}:{port} TCP 连接探测超时(1s)，判定处于离线/断网状态")

    # 执行前先看表名。命中就直接给答案，省掉一个失败往返；
    # 拿不准一律放行（fail-open），误拦一条能跑的查询比放过一条会报错的糟得多。
    pre = None
    try:
        pre = dberrors.precheck_tables(sql, prof, name, database)
    except Exception:                                   # noqa: BLE001
        pre = None
    if pre:
        raise ToolError(pre)

    try:
        cols, rows, trunc = db_broker.query(
            sql,
            host=host,
            port=port,
            user=dbcfg.get("user"),
            password=pwd,
            mysql_bin=dbcfg.get("mysql_bin"),
            database=database or dbcfg.get("database"),
            max_rows=limit,
            redact=not no_redact,
        )
        return db_broker.format_table(cols, rows, trunc, limit)
    except db_broker.BrokerError as e:
        # **必须和 CLI 走同一套自纠错**。这里曾经直接透传裸报错，
        # 结果走 MCP 的会话反复撞「表不存在」只能猜表名，而走 CLI 的会话有护栏。
        hint = None
        try:
            run = _readonly_query_fn(dbcfg, pwd)
            hint = dberrors.explain(e, sql, prof, name,
                                    lambda sc, tb: schema.fetch_columns(run, sc, tb),
                                    run=run, prefix=dbcfg.get("prefix"))
        except Exception:                               # noqa: BLE001
            hint = None
        raise ToolError(f"查询失败: {e}" + (f"\n\n{hint}" if hint else "")) from e


TOOL_HANDLERS = {
    "aisk_task_inspect": handle_task_inspect,
    "aisk_fact_service": handle_fact_service,
    "aisk_fact_entry": handle_fact_entry,
    "aisk_env_summary": handle_env_summary,
    "aisk_repo": handle_repo,
    "aisk_db_query": handle_db_query,
}


def _rpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _validate_arguments(tool_name, args):
    """校验当前工具使用的扁平 schema；拒绝类型强转和未知参数。"""
    if not isinstance(args, dict):
        raise ValueError("arguments 必须是对象")
    spec = next(t["inputSchema"] for t in TOOLS if t["name"] == tool_name)
    props = spec["properties"]
    unknown = set(args) - set(props)
    if unknown:
        raise ValueError("未知参数: " + ", ".join(sorted(unknown)))
    for key in spec.get("required", []):
        if key not in args:
            raise ValueError(f"缺少参数: {key}")
    types = {"string": str, "boolean": bool, "integer": int}
    for key, value in args.items():
        prop = props[key]
        if type(value) is not types[prop["type"]]:
            raise ValueError(f"{key} 必须是 {prop['type']}")
        if isinstance(value, str) and not value.strip():
            raise ValueError(f"{key} 不能为空")
        if "minimum" in prop and value < prop["minimum"]:
            raise ValueError(f"{key} 不得小于 {prop['minimum']}")
        if "maximum" in prop and value > prop["maximum"]:
            raise ValueError(f"{key} 不得大于 {prop['maximum']}")


def process_request(req):
    if (not isinstance(req, dict) or req.get("jsonrpc") != "2.0"
            or not isinstance(req.get("method"), str)):
        return _rpc_error(None, -32600, "Invalid Request")
    msg_id = req.get("id")
    if "id" in req and (isinstance(msg_id, bool)
                       or not isinstance(msg_id, (str, int, type(None)))):
        return _rpc_error(None, -32600, "Invalid request id")
    # 通知不响应，也不能借 tools/call 通知触发查询。
    if "id" not in req:
        return None
    method = req["method"]
    params = req.get("params", {})
    if not isinstance(params, dict):
        return _rpc_error(msg_id, -32602, "params 必须是对象")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or tool_name not in TOOL_HANDLERS:
            return _rpc_error(msg_id, -32601, "Tool not found")
        args = params.get("arguments", {})
        try:
            _validate_arguments(tool_name, args)
        except ValueError as e:
            return _rpc_error(msg_id, -32602, str(e))
        try:
            raw_text = TOOL_HANDLERS[tool_name](args)
            if tool_name == "aisk_db_query":
                result_text = mcp_contract.text_response(
                    ok=True, source="database-broker", data=raw_text,
                    availability="live", redacted=True,
                    profile=args.get("profile"), environment=args.get("env") or "dev")
            elif tool_name == "aisk_task_inspect":
                result_text = mcp_contract.text_response(
                    ok=True, source="sqlite-coordination-store", data=raw_text,
                    availability="live", redacted=False,
                    profile=args.get("profile"), environment=args.get("env") or "dev")
            else:
                result_text = mcp_contract.text_response(
                    ok=True, source="local-manifest", data=raw_text,
                    availability="offline_snapshot", redacted=False,
                    profile=args.get("profile"), environment=args.get("env") or "dev")
            is_error = False
        except (ToolError, profile.ProfileError, db_broker.BrokerError) as e:
            if isinstance(e, OfflineDatabaseUnavailableError) or (tool_name == "aisk_db_query" and any(word in str(e).lower() for word in ("找不到 mysql", "连接", "超时", "口令", "数据库连接"))):
                result_text = mcp_contract.offline_database_unavailable(
                    profile=args.get("profile"), environment=args.get("env") or "dev")
            else:
                result_text = mcp_contract.text_response(
                    ok=False, source="aisk-mcp-server", data=None,
                    availability="live" if tool_name == "aisk_db_query" else "offline_snapshot",
                    redacted=True, profile=args.get("profile"), environment=args.get("env") or "dev",
                    error_code="TOOL_REJECTED", error_message=str(e))
            is_error = True
        except Exception as e:
            # 未知异常可能含凭据或连接串，不向客户端透传异常原文。
            result_text = mcp_contract.text_response(
                ok=False, source="aisk-mcp-server", data=None,
                availability="offline_unavailable" if tool_name == "aisk_db_query" else "offline_snapshot",
                redacted=True, profile=args.get("profile"), environment=args.get("env") or "dev",
                error_code="INTERNAL_ERROR", error_message=f"工具执行失败（{type(e).__name__}），请检查本地配置")
            is_error = True
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": str(result_text)}],
            "isError": is_error,
        }}
    return _rpc_error(msg_id, -32601, f"Method '{method}' not supported")


def dump_schemas(target_dir):
    """导出工具 Schema 到指定目录（如 ~/.gemini/antigravity/mcp/aisk/）。"""
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    generated = []
    for tool in TOOLS:
        schema_file = target / f"{tool['name']}.json"
        # 兼容当前扁平格式（包含 name, description, parameters/inputSchema）
        schema_dict = {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["inputSchema"],
        }
        schema_file.write_text(json.dumps(schema_dict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        generated.append(str(schema_file))
    return generated


def run_stdio_server():
    """换行分隔的 JSON-RPC；损坏消息不终止后续请求。"""
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = _rpc_error(None, -32700, "Parse error")
        else:
            resp = process_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--dump-schemas":
        paths = dump_schemas(sys.argv[2])
        print(f"已导出 {len(paths)} 个 MCP Schema 文件到 {sys.argv[2]}")
        sys.exit(0)
    run_stdio_server()
