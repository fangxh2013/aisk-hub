# -*- coding: utf-8 -*-
"""数据库只读 broker。

**为什么是 broker 而不是「把口令给模型」**（设计文档 §7 方案 F）：
模型拿到口令 → 口令进上下文 → 流向各家模型厂商 → 写进会话留痕。
2026-08-24 实测过一次真实后果：两个 DEV 口令扇出到 4 个端、约 8,700 个文件。
broker 让模型只能调用「能力」，口令由 CLI 代持，从头到尾不进上下文。

**三层防护，缺一不可**：
  1. 只读白名单：SQL 必须以只读语句开头，且剥掉字符串字面量后不含任何写关键字。
     这是「默认拒绝」而非「黑名单枚举」——后者永远补不齐
     （TRUNCATE / REPLACE / SET GLOBAL / SELECT INTO OUTFILE 都能毁数据）。
  2. 结果脱敏：字段名像密钥/隐私的，值一律替换成 [REDACTED]。
     这条防的是「查询本身合法、但结果里带着密钥或个人信息」——
     ChatGPT 评审指出的缺口，光靠「不实现写函数」防不住。
  3. 行数上限：防止一条 SELECT * 把整张用户表倒进上下文。

生产环境额外要求 `--env prod` 显式指定，profile 里 require_confirm 的还要再确认一次。
"""

import os
import re
import shutil
import subprocess

# mysql 客户端常见落点。homebrew 的 mysql-client 是 keg-only，不在标准 bin 里，
# 所以不能只靠写死的 PATH——2026-08-24 实测踩过这个坑。
MYSQL_CANDIDATES = [
    "/opt/homebrew/opt/mysql-client/bin/mysql",
    "/usr/local/opt/mysql-client/bin/mysql",
    "/opt/homebrew/bin/mysql",
    "/usr/local/bin/mysql",
    "/usr/bin/mysql",
]


def resolve_mysql(explicit=None):
    """找 mysql 客户端。优先显式指定 → PATH → 常见落点。"""
    if explicit and (os.path.isabs(explicit) or shutil.which(explicit)):
        return explicit if os.path.isabs(explicit) else shutil.which(explicit)
    found = shutil.which("mysql")
    if found:
        return found
    for c in MYSQL_CANDIDATES:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None

# 只读语句白名单。必须是整条 SQL 的开头。
READONLY_START = re.compile(
    r"^\s*(SELECT|SHOW|DESC|DESCRIBE|EXPLAIN|WITH)\b", re.I)

# 剥掉字符串字面量后，出现任何一个就拒绝
WRITE_TOKENS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE|RENAME|GRANT|"
    r"REVOKE|SET|LOCK|UNLOCK|CALL|LOAD|OUTFILE|DUMPFILE|INTO\s+OUTFILE|"
    r"HANDLER|FLUSH|KILL|SHUTDOWN|RESET|PURGE|START|COMMIT|ROLLBACK)\b", re.I)

# 多语句：一条查询里塞第二条是经典绕过手法
MULTI_STMT = re.compile(r";\s*\S")

# 元数据列名，绝不脱敏——它们是 schema 信息不是隐私。
# 2026-08-24 踩过：裸 `name` 匹配把 information_schema 的 TABLE_NAME 打码了，
# 查表分布直接看不到结果。过度脱敏会让工具不可用，和漏脱敏一样是 bug。
METADATA_COL = re.compile(
    r"^(table|column|schema|database|db|index|constraint|trigger|routine|"
    r"field|file|event|partition|engine|collation|charset|tag|type|status|"
    r"job|task|topic|queue|service|module|method|class|package)_?name$", re.I)

# 字段名命中这些的，值一律脱敏。
# `name` 只在明确是人/联系人语境时才算敏感，不能裸匹配。
SENSITIVE_COL = re.compile(
    r"(pass|pwd|secret|token|credential|salt|"
    r"\bhash\b|api_?key|secret_?key|private_?key|access_?key|"
    r"phone|mobile|\btel\b|email|id_?card|idcard|identity|bank|card_?no|"
    r"\baddr(ess)?\b|"
    r"(user|real|nick|full|contact|cust(omer)?|owner|holder|recipient|"
    r"receiver|consignee|linkman|person)_?name|"
    r"account)", re.I)

MAX_ROWS_DEFAULT = 200
REDACTED = "[REDACTED]"


class BrokerError(Exception):
    pass


def _strip_literals(sql):
    """剥掉字符串字面量和注释，避免 'DELETE' 这种出现在数据里的词被误判。"""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"#[^\n]*", " ", sql)
    sql = re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)
    sql = re.sub(r'"(?:[^"\\]|\\.)*"', '""', sql)
    return sql


def assert_readonly(sql):
    """默认拒绝。任何无法静态确认为只读的，一律拒绝。"""
    if not sql or not sql.strip():
        raise BrokerError("SQL 为空")
    stripped = _strip_literals(sql)
    if MULTI_STMT.search(stripped.rstrip().rstrip(";")):
        raise BrokerError("拒绝：一次只允许一条语句（检测到多语句）")
    if not READONLY_START.match(stripped):
        head = stripped.strip().split()[:1]
        raise BrokerError(
            f"拒绝：只允许 SELECT/SHOW/DESC/EXPLAIN/WITH 开头的只读查询"
            f"（实际以 {head[0] if head else '?'} 开头）")
    hit = WRITE_TOKENS.search(stripped)
    if hit:
        raise BrokerError(f"拒绝：查询里出现写操作关键字 {hit.group(1).upper()}")
    return True


def redact_row(cols, row):
    """按列名脱敏。列名可疑就把值换掉，不管值长什么样。"""
    out = []
    for c, v in zip(cols, row):
        sensitive = (v not in ("", "NULL", None)
                     and not METADATA_COL.match(c)
                     and SENSITIVE_COL.search(c))
        out.append(REDACTED if sensitive else v)
    return out


def query(sql, *, host, port, user, password, database=None,
          mysql_bin=None, max_rows=MAX_ROWS_DEFAULT, timeout=30, redact=True):
    """跑一条只读查询，返回 (列名, 行列表, 是否被截断)。

    口令通过环境变量 MYSQL_PWD 传给 mysql 客户端，**不进命令行**——
    命令行参数任何人 ps 一下就能看到。
    """
    assert_readonly(sql)

    exe = resolve_mysql(mysql_bin)
    if not exe:
        raise BrokerError(
            "找不到 mysql 客户端。装一个（brew install mysql-client）"
            "或在 profile 的 db 段加 mysql_bin: <绝对路径>")
    cmd = [exe, "-h", host, "-P", str(port), "-u", user,
           "--batch", "--raw", "--column-names", "--connect-timeout=10"]
    if database:
        cmd += ["-D", database]
    cmd += ["-e", sql]

    # 继承现有 PATH，只额外注入口令——写死 PATH 会找不到 keg-only 的客户端
    env = dict(os.environ, MYSQL_PWD=password)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as e:
        raise BrokerError(f"找不到 mysql 客户端（{mysql_bin}）") from e
    except subprocess.TimeoutExpired as e:
        raise BrokerError(f"查询超时（{timeout}s）") from e

    if r.returncode != 0:
        # 错误信息里可能带连接串，但绝不能带口令——MYSQL_PWD 不会出现在 stderr
        msg = (r.stderr or "").strip().splitlines()
        msg = msg[-1] if msg else f"退出码 {r.returncode}"
        raise BrokerError(f"查询失败：{msg[:300]}")

    lines = r.stdout.rstrip("\n").split("\n") if r.stdout.strip() else []
    if not lines:
        return [], [], False
    cols = lines[0].split("\t")
    rows = [ln.split("\t") for ln in lines[1:]]
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    if redact:
        rows = [redact_row(cols, row) for row in rows]
    return cols, rows, truncated


def execute(sql, *, host, port, user, password, database=None,
            mysql_bin=None, timeout=60):
    """**受控写入**。只有显式授权路径才会走到这里。

    注意这里刻意**不做**只读校验——它就是用来写的。
    真正的边界在调用方：必须显式 --privileged + --yes，凭据来自独立命名空间，
    且每次调用都留审计。「AI 默认只读」这条靠「默认路径根本到不了这里」保证，
    而不是靠在这里再加一层正则。
    """
    if not sql or not sql.strip():
        raise BrokerError("SQL 为空")
    if MULTI_STMT.search(_strip_literals(sql).rstrip().rstrip(";")):
        raise BrokerError("拒绝：一次只允许一条语句")

    exe = resolve_mysql(mysql_bin)
    if not exe:
        raise BrokerError(
            "找不到 mysql 客户端。装一个（brew install mysql-client）"
            "或在 profile 的 db 段加 mysql_bin: <绝对路径>")
    cmd = [exe, "-h", host, "-P", str(port), "-u", user,
           "--batch", "--connect-timeout=10"]
    if database:
        cmd += ["-D", database]
    cmd += ["-e", sql]
    # 继承现有 PATH，只额外注入口令——写死 PATH 会找不到 keg-only 的客户端
    env = dict(os.environ, MYSQL_PWD=password)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise BrokerError(f"执行超时（{timeout}s）") from e
    if r.returncode != 0:
        msg = (r.stderr or "").strip().splitlines()
        raise BrokerError(f"执行失败：{(msg[-1] if msg else '')[:300]}")
    return (r.stdout or "").strip()


def format_table(cols, rows, truncated, max_rows=MAX_ROWS_DEFAULT):
    if not cols:
        return "(无结果)"
    widths = [len(c) for c in cols]
    for row in rows:
        for i, v in enumerate(row):
            if i < len(widths):
                widths[i] = min(max(widths[i], len(str(v))), 40)
    out = ["  ".join(c.ljust(widths[i])[:40] for i, c in enumerate(cols)),
           "  ".join("-" * widths[i] for i in range(len(cols)))]
    for row in rows:
        out.append("  ".join(str(v).ljust(widths[i])[:40]
                             for i, v in enumerate(row) if i < len(widths)))
    out.append(f"\n{len(rows)} 行")
    if truncated:
        out.append(f"⚠️ 结果超过 {max_rows} 行已截断，请缩小查询范围（加 WHERE / LIMIT）")
    return "\n".join(out)
