# -*- coding: utf-8 -*-
"""查询报错的自纠错层：把 MySQL 的裸报错换成「真正的答案」。

**为什么必须独立成模块**：2026-08-27 实测的失效——字段自纠错只写在 CLI 里，
MCP 的 handle_db_query 直接 `return f"❌ 查询失败: {e}"` 把裸报错透传出去。
于是走 CLI 的会话有护栏，走 MCP 的 Antigravity/Codex 什么都没有，
只能凭猜表名反复撞 `Table 'xxx' doesn't exist`。

**同一个能力有两条客户端路径，只教育了其中一条，另一条暴露的是裸的危险原语。**
纠错逻辑放在这里，两条路径都必须调用它。

**为什么用报错而不是靠模型先调 fact table**：前置调用要靠模型记得，
而报错是它撞上去时必然会读到的。把答案放在它必然经过的地方，比放在
它可能跳过的地方可靠——何况 MCP 层根本没有暴露 fact table 这个动词。
"""

import re

from engine import schema

UNKNOWN_COL = re.compile(r"Unknown column '([^']+)'", re.I)
NO_TABLE = re.compile(r"Table '([^']*?)\.?([A-Za-z0-9_]+)' doesn't exist", re.I)
TABLE_IN_SQL = re.compile(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+`?([A-Za-z0-9_]+)`?\.?`?([A-Za-z0-9_]*)`?", re.I)


def _rank_fuzzy(hits, want_schema, wrong):
    """近似候选排序：调用方指定的库优先，其次名字更接近的。

    模型写 SQL 时已经选了一个库，那是它的意图信号——把同库的候选埋在
    另一个库的一堆同后缀表底下，等于让它再猜一次。
    """
    def key(h):
        same_schema = 0 if h["schema"] == want_schema else 1
        name = h["table"]
        # 名字长度差越小越像；再按是否包含完整关键词
        return (same_schema, 0 if wrong in name else 1, abs(len(name) - len(wrong)), name)
    return sorted(hits, key=key)


def _locate(prof, env_name, table, run=None, prefix=None, want_schema=None):
    """在整个环境里找这张表真正在哪个库。找不到就退回模糊匹配给近似名。

    **缓存冷时必须现取**。第一版只读缓存，结果新会话（缓存必然为空）拿到的是
    「没有名字相近的表」——一句斩钉截铁的错话，比不给提示更糟：
    模型会据此以为这张表真的不存在，转去猜别的名字。
    而新会话恰恰就是这个错误最常发生的时刻。
    """
    tables = schema.load_cache(prof["project"], env_name) or []
    if not tables and run is not None and prefix:
        try:
            tables = schema.fetch(run, prefix)
            schema.save_cache(prof["project"], env_name, tables)
        except Exception:                                # noqa: BLE001
            tables = []
    if not tables:
        return None, []
    exact, _ = schema.find(tables, table)
    if exact:
        return exact, []
    # 名字不对：给近似候选（beiyue_order → beiyue_orders 这种一个字母之差最常见）
    fuzzy, _ = schema.find(tables, f"%{table}%")
    if not fuzzy:
        head = table.split("_")[0]
        if len(head) >= 3:
            fuzzy, _ = schema.find(tables, f"%{head}%")
    return None, _rank_fuzzy(fuzzy, want_schema, table)[:6]


def explain(err, sql, prof, env_name, fetch_columns=None, run=None, prefix=None):
    """把报错翻译成可执行的下一步。无话可说时返回 None，调用方原样透传报错。

    fetch_columns: 可选的 (schema, table) -> columns 函数；不给就跳过字段建议。
    """
    err = str(err)

    m = NO_TABLE.search(err)
    if m:
        want_schema, wrong = m.group(1) or None, m.group(2)
        exact, fuzzy = _locate(prof, env_name, wrong, run, prefix, want_schema)
        if exact:
            lines = [f"表 {wrong} 不在你指定的库里，它实际在："]
            lines += [f"  {h['schema']}.{h['table']}" for h in exact[:5]]
            lines.append(f"\n改用 --database {exact[0]['schema']} 重跑即可。")
            return "\n".join(lines)
        if fuzzy:
            lines = [f"没有叫 {wrong} 的表。名字相近的有："]
            lines += [f"  {h['schema']}.{h['table']}" for h in fuzzy]
            lines.append(f"\n全环境检索: aisk fact table '%{wrong}%' --env {env_name}")
            return "\n".join(lines)
        return (f"没有叫 {wrong} 的表，也没有名字相近的。\n"
                f"先看有哪些表: aisk fact table --env {env_name}")

    m = UNKNOWN_COL.search(err)
    if m and fetch_columns:
        wrong = m.group(1).split(".")[-1]
        t = TABLE_IN_SQL.search(sql or "")
        if not t:
            return None
        a, b = t.group(1), t.group(2)
        schema_name, table = (a, b) if b else (None, a)
        if not schema_name:
            exact, _ = _locate(prof, env_name, table, run, prefix)
            if not exact or len(exact) != 1:
                return None
            schema_name, table = exact[0]["schema"], exact[0]["table"]
        try:
            cols = fetch_columns(schema_name, table)
        except Exception:                                # noqa: BLE001
            return None
        if not cols:
            return None
        sugg = schema.suggest_columns(cols, wrong)
        lines = []
        if sugg:
            lines.append(f"{schema_name}.{table} 里没有 {wrong}，你要找的可能是：")
            for c in sugg:
                cm = f"  — {c['comment']}" if c["comment"] else ""
                lines.append(f"  {c['name']:<26} {c['type']}{cm}")
        lines.append(f"\n完整字段: aisk fact columns {schema_name}.{table} --env {env_name}")
        return "\n".join(lines)

    return None

# FROM / JOIN 后面的表名。**只认这两个**：UPDATE/INTO 属于写操作，走不到这里；
# 子查询与 CTE 里的别名不在此列，正是靠 fail-open 兜住。
_FROM_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?(?:\s*\.\s*`?([A-Za-z_][A-Za-z0-9_]*)`?)?", re.I)
_SQL_KEYWORDS = {"select", "dual", "lateral", "unnest", "values"}


def precheck_tables(sql, prof, env_name, want_schema=None):
    """执行前先看表名对不对。对就返回 None，不对就返回提示（并且不必去查库了）。

    **为什么比「撞了再提示」更好**：撞一次要花一个失败往返——模型发请求、等报错、
    读提示、再发一次。执行前拦下来，它第一次就拿到正确的库名。
    调研过的两个成熟项目（mcp-alchemy、SqlSchemaBridgeMCP）都只做「让模型先查」，
    没有做这一层；而「先查」要靠模型记得，这一层不用。

    **必须 fail-open**。CTE、子查询别名、临时表、跨库 JOIN 都可能让静态检查误判，
    而误拦一条本来能跑的查询，比放过一条会报错的查询糟得多——后者还有自纠错兜底。
    所以：只有在「缓存已热」且「表名明确不存在于整个环境」时才拦。
    缓存冷时直接放行，不为了检查而多打一次库。
    """
    tables = schema.load_cache(prof.get("project"), env_name) or []
    if not tables:
        return None                                  # 缓存冷 → 放行，交给自纠错

    # **必须按「指定的库」判定，不能只看「表名在这个环境存在过」**。
    # 第一版用的是全环境表名集合，于是 beiyue_orders 存在就跳过了——
    # 恰好漏掉「库指错」这个最常见的情况（表确实有，只是不在你说的那个库里）。
    if want_schema:
        known = {t["table"].lower() for t in tables
                 if t["schema"].lower() == want_schema.lower()}
    else:
        # 没指定库时走连接默认库，静态判断不可靠 → 全部放行
        return None
    aliases = set(re.findall(r"\bAS\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", sql or "", re.I))
    aliases |= set(re.findall(r"\bWITH\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", sql or "", re.I))
    aliases = {a.lower() for a in aliases}

    for m in _FROM_JOIN.finditer(sql or ""):
        a, b = m.group(1), m.group(2)
        name = (b or a)
        low = name.lower()
        if low in _SQL_KEYWORDS or low in known or low in aliases:
            continue
        # 明确写了库名的，交给数据库报错——跨库场景静态判断不可靠
        exact, fuzzy = _locate(prof, env_name, name, want_schema=want_schema or a if b else want_schema)
        if exact:
            lines = [f"⚠️ 执行前检查：表 {name} 不在你指定的库里，它实际在："]
            lines += [f"  {h['schema']}.{h['table']}" for h in exact[:5]]
            lines.append(f"\n改用 --database {exact[0]['schema']} 重跑即可（本次未查库）。")
            return "\n".join(lines)
        if fuzzy:
            lines = [f"⚠️ 执行前检查：这个环境里没有叫 {name} 的表。名字相近的有："]
            lines += [f"  {h['schema']}.{h['table']}" for h in fuzzy]
            lines.append(f"\n（本次未查库。全环境检索: aisk fact table '%{name}%' --env {env_name}）")
            return "\n".join(lines)
        # 完全不认识：也可能是我没覆盖到的语法，放行让数据库说话
    return None
