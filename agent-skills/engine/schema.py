# -*- coding: utf-8 -*-
"""表 → 库 的定位。

**为什么单列一个模块**：这是「查库前必须知道、但一直没人提供」的事实。
2026-08-24 实测：DEV 有 10 个库、557 张表，微服务按**服务**分库，
而 AI 只能按语义猜「订单应该在 shop 库」——猜错就是
`Table 'example_dev_shop.order_item' doesn't exist`。

更麻烦的是同名概念散落多库：带 order 的表在 order(22)/inventory(7)/
member(7)/scrm(5)/payment(1) 都有。就算猜对大方向也可能落错库。

所以这不是模型推理能力问题，是事实缺失。补上这一个查询，这类错误就绝迹。

**带缓存**：information_schema 在大实例上不便宜，而表分布变化很慢。
默认缓存到 `AISK_HOME/cache/`，可用 --refresh 强制刷新。
"""

import json
import os
import time
from pathlib import Path

CACHE_DIR = Path(os.environ.get("AISK_HOME") or (Path.home() / ".aisk")) / "cache"
CACHE_TTL = 24 * 3600  # 表分布变化慢，一天足够


def _cache_file(project, env):
    return CACHE_DIR / f"tables-{project}-{env}.json"


def load_cache(project, env, ttl=CACHE_TTL):
    f = _cache_file(project, env)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("ts", 0) > ttl:
        return None
    return data.get("tables")


def save_cache(project, env, tables):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_file(project, env).write_text(
        json.dumps({"ts": time.time(), "tables": tables}, ensure_ascii=False),
        encoding="utf-8")


LIST_SQL = (
    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ROWS "
    "FROM information_schema.TABLES "
    "WHERE TABLE_TYPE='BASE TABLE' AND TABLE_SCHEMA LIKE '{prefix}%' "
    "ORDER BY TABLE_SCHEMA, TABLE_NAME"
)


def fetch(query_fn, prefix):
    """query_fn 是注入进来的 broker 查询函数，本模块不碰凭据。"""
    cols, rows, _ = query_fn(LIST_SQL.format(prefix=prefix))
    if not cols:
        return []
    return [{"schema": r[0], "table": r[1],
             "rows": r[2] if len(r) > 2 else ""} for r in rows]


def _score(pattern, table):
    """给候选排序：完全相同 > 前缀/后缀 > 包含 > 模糊。"""
    p, t = pattern.lower(), table.lower()
    if p == t:
        return 0
    if t.startswith(p) or t.endswith(p):
        return 1
    if p in t:
        return 2
    return 3


def find(tables, pattern, fuzzy=True):
    """按表名找。支持 % 通配；找不到精确匹配时给相近建议。

    返回 (精确命中列表, 建议列表)。
    """
    p = pattern.strip()
    # 只有出现 % 才进模式匹配。**裸 `_` 不当通配**——真实表名里 `_` 遍地都是
    # （oms_order_item），把它当通配会误伤远多于帮忙。用户想模糊匹配时会写 %。
    if "%" in p:
        # SQL LIKE → 正则。不能先 re.escape 再替换 %——新版 Python 的
        # re.escape 不转义 %，替换会落空（2026-08-24 踩过）。
        # 逐字符翻译最稳：% → .*，_ → .，其余字面量转义。
        import re as _re
        parts = []
        for ch in p:
            if ch == "%":
                parts.append(".*")
            elif ch == "_":
                parts.append(".")
            else:
                parts.append(_re.escape(ch))
        rx = _re.compile("^" + "".join(parts) + "$", _re.I)
        hits = [t for t in tables if rx.match(t["table"])]
        hits.sort(key=lambda t: (t["schema"], t["table"]))
        return hits, []

    exact = [t for t in tables if t["table"].lower() == p.lower()]
    if exact:
        return exact, []

    contains = [t for t in tables if p.lower() in t["table"].lower()]
    if contains:
        return contains, []

    if not fuzzy:
        return [], []

    # 一个都没有：拆词找相近的，帮 AI 从「猜错的名字」跳到「真实的名字」
    import difflib
    names = sorted({t["table"] for t in tables})
    close = difflib.get_close_matches(p, names, n=8, cutoff=0.45)

    # 再按词根补一批（byxx_order → by / order 都试）
    parts = [x for x in p.replace("-", "_").split("_") if len(x) >= 2]
    for part in parts:
        for t in tables:
            if part.lower() in t["table"].lower() and t["table"] not in close:
                close.append(t["table"])
                if len(close) >= 15:
                    break
        if len(close) >= 15:
            break

    sugg = [t for t in tables if t["table"] in close]
    sugg.sort(key=lambda t: (_score(p, t["table"]), t["schema"], t["table"]))
    return [], sugg[:15]


COLS_SQL = (
    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_COMMENT "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA='{schema}' AND TABLE_NAME='{table}' "
    "ORDER BY ORDINAL_POSITION"
)


def fetch_columns(query_fn, schema, table):
    """取字段清单。**带上 COLUMN_COMMENT**——中文注释是 AI 最需要的线索，
    没它就只能靠字段名猜语义（buyer_id 还是 user_id？）。"""
    cols, rows, _ = query_fn(COLS_SQL.format(schema=schema, table=table))
    if not cols:
        return []
    return [{"name": r[0], "type": r[1], "nullable": r[2],
             "key": r[3], "comment": r[4] if len(r) > 4 else ""} for r in rows]


def suggest_columns(columns, wrong):
    """AI 猜错字段名时，指向真实的那个。

    2026-08-25 实测场景：AI 凭电商惯例猜 order_sn / order_no，
    实际是 beiyue_order_no —— 一次 Unknown column 就该给出答案，
    而不是让它 DESC 一遍再重猜。三层匹配覆盖最常见的猜错方式。
    """
    if not columns:
        return []
    names = [c["name"] for c in columns]
    w = wrong.lower().strip("`\"'")
    hits = []

    # 1) 后缀/前缀含关系：order_no ⊂ beiyue_order_no
    for c in columns:
        n = c["name"].lower()
        if n.endswith(w) or n.startswith(w) or w in n:
            hits.append(c)
    if hits:
        return hits[:8]

    # 2) 词根重合：order_sn → 拆出 order，命中所有含 order 的字段
    parts = [x for x in w.replace("-", "_").split("_") if len(x) >= 2]
    for part in parts:
        for c in columns:
            if part in c["name"].lower() and c not in hits:
                hits.append(c)
    if hits:
        return hits[:8]

    # 3) 编辑距离兜底
    import difflib
    close = difflib.get_close_matches(w, [n.lower() for n in names], n=6, cutoff=0.5)
    return [c for c in columns if c["name"].lower() in close][:8]


# 中文查询词 → 英文字段名/注释里可能出现的写法。
# 2026-08-25 踩过：`--like 状态` 匹配不到 status 字段（注释里写的是英文枚举
# WAIT_AUDIT/PROCESSING/...，"状态"二字一次没出现），等于过滤功能形同虚设。
_ZH_EN = {
    "状态": ["status", "state", "stat"],
    "时间": ["time", "date", "_at", "created", "updated"],
    "金额": ["amount", "price", "fee", "money", "total"],
    "退款": ["refund"],
    "订单": ["order"],
    "用户": ["user", "buyer", "member", "customer"],
    "手机": ["phone", "mobile", "tel"],
    "地址": ["address", "addr", "province", "city", "district"],
    "数量": ["num", "count", "qty", "quantity"],
    "备注": ["remark", "note", "comment", "memo"],
    "类型": ["type", "kind", "category"],
    "原因": ["reason", "cause"],
    "编号": ["no", "code", "sn", "id"],
    "店铺": ["shop", "store"],
    "商品": ["goods", "product", "item", "sku", "spu"],
    "物流": ["logistics", "express", "shipping", "delivery"],
    "支付": ["pay", "payment"],
    "审核": ["audit", "review", "approve"],
}


def _expand_query(word):
    """把查询词扩成一组匹配项：原词 + 中英对照 + 反向（英文词也带出中文）。"""
    w = word.lower().strip()
    out = {w}
    for zh, ens in _ZH_EN.items():
        if zh in word:
            out.update(ens)
        if any(e == w or e in w for e in ens):
            out.add(zh)
            out.update(ens)
    return out


def format_columns(schema, table, columns, only=None):
    """紧凑输出——这是要进 AI 上下文的，每行都要有信息量。"""
    if not columns:
        return f"(找不到 {schema}.{table} 的字段)"
    show = columns
    if only:
        terms = _expand_query(only)
        show = [c for c in columns
                if any(t in c["name"].lower() or t in (c["comment"] or "").lower()
                       for t in terms)]
        if not show:
            show = columns
    out = [f"{schema}.{table}  {len(columns)} 字段"]
    for c in show:
        key = {"PRI": " [PK]", "UNI": " [UQ]", "MUL": " [IDX]"}.get(c["key"], "")
        null = "" if c["nullable"] == "YES" else " NOT NULL"
        # 注释完整输出，不截断——状态字段的枚举值常常就写在注释里
        # （如 "WAIT_AUDIT/PROCESSING/COMPLETED/CANCELLED"），截断就丢了关键信息
        cm = f"  — {c['comment']}" if c["comment"] else ""
        out.append(f"  {c['name']:<26} {c['type']}{key}{null}{cm}")
    if only and len(show) < len(columns):
        out.append(f"  （已按 '{only}' 过滤，共 {len(columns)} 个字段）")
    return "\n".join(out)


RELATED_SQL = (
    "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.COLUMN_COMMENT, "
    "       (SELECT COUNT(*) FROM information_schema.STATISTICS s "
    "        WHERE s.TABLE_SCHEMA=c.TABLE_SCHEMA AND s.TABLE_NAME=c.TABLE_NAME "
    "          AND s.COLUMN_NAME=c.COLUMN_NAME) AS idxed "
    "FROM information_schema.COLUMNS c "
    "WHERE c.TABLE_SCHEMA='{schema}' AND c.COLUMN_NAME IN ({cols}) "
    "  AND c.TABLE_NAME <> '{table}' "
    "ORDER BY idxed DESC, c.TABLE_NAME"
)

# 这些列名到处都有，用它们做关联毫无意义（每张表都有 id/create_time）
_JOIN_NOISE = {"id", "create_time", "update_time", "created_at", "updated_at",
               "create_by", "update_by", "del_flag", "is_deleted", "remark",
               "status", "version", "tenant_id"}


def join_candidates(columns):
    """挑出适合做关联键的列：业务编号类，排除通用噪声列。"""
    out = []
    for c in columns:
        n = c["name"].lower()
        if n in _JOIN_NOISE:
            continue
        # 业务键的典型形态：*_no / *_code / *_sn / *_id（但不是裸 id）
        if n.endswith(("_no", "_code", "_sn")) or (n.endswith("_id") and n != "id"):
            out.append(c)
    return out


def find_related(query_fn, schema, table, columns):
    """靠共同的业务键推断表关联——这套库没有外键约束，只能这么找。

    2026-08-25 实测场景：排查一笔订单要跨 主单→售后→售后日志 三张表，
    AI 不知道它们靠什么字段关联，只能猜 JOIN 条件。
    返回 {关联字段: [(表名, 该表是否给这列建了索引)]}。
    """
    keys = join_candidates(columns)
    if not keys:
        return {}
    names = ", ".join("'" + c["name"].replace("'", "") + "'" for c in keys)
    try:
        cols, rows, _ = query_fn(
            RELATED_SQL.format(schema=schema, cols=names, table=table))
    except Exception:                                    # noqa: BLE001
        return {}
    if not cols:
        return {}
    out = {}
    for r in rows:
        tname, cname = r[0], r[1]
        idxed = (r[3] not in ("0", "", None)) if len(r) > 3 else False
        out.setdefault(cname, []).append((tname, idxed))
    return out


def format_related(schema, table, rel, columns):
    if not rel:
        return (f"{schema}.{table} 没找到可关联的表"
                f"（该库无外键约束，靠共同业务键推断）")
    cmt = {c["name"]: c.get("comment", "") for c in columns}
    out = [f"{schema}.{table} 的关联线索（无外键，按共同业务键推断）:"]

    def rank(kv):
        """**关联表越少越精准**，排前面。

        2026-08-25 踩过：原先按关联表数量降序，结果 order_no（22 张表，
        泛化的中台单号）排在 beiyue_order_no（3 张表，正是要查的售后链路）前面——
        把最该看的那条埋在了后面。数量多说明这个键太泛，不是好的关联入口。
        专属前缀的键（表名前缀 ⊂ 列名）额外优先。
        """
        col, tables = kv
        prefix = table.split("_")[0].lower()
        specific = 0 if col.lower().startswith(prefix) else 1
        return (specific, len(tables), col)

    for col, tables in sorted(rel.items(), key=rank):
        note = f"  — {cmt.get(col, '')}" if cmt.get(col) else ""
        out.append(f"\n  通过 {col}{note}")
        for tname, idxed in tables[:10]:
            mark = " [有索引]" if idxed else ""
            out.append(f"    {schema}.{tname}{mark}")
        if len(tables) > 10:
            out.append(f"    … 另有 {len(tables)-10} 张")
    return "\n".join(out)


def schema_summary(tables):
    """各库表数统计——回答「这个项目到底有几个库」。"""
    out = {}
    for t in tables:
        out[t["schema"]] = out.get(t["schema"], 0) + 1
    return sorted(out.items())
