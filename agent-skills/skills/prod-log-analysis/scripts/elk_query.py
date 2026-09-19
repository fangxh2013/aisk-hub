#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ELK 生产错误日志只读查询与归因（prod-log-analysis 技能）。

用法：
  python3 elk_query.py stat          --window 24h
  python3 elk_query.py recent        --window 2h
  python3 elk_query.py fetch         --window 24h --out /tmp/clusters.json
  python3 elk_query.py img-timeline  --services order,inventory,job --window 48h
  python3 elk_query.py xref          --window 24h --repo ~/work/xinhua-platform \
                                     --author fangxh2013

设计要点：
  * **本脚本不含任何地址、账号或口令**。端点全部来自 aisk 项目档案：envs.<env>.elk
    （url/user/secret/index_prefix/jump）与 envs.<env>.k8s.ns；缺项直接报错，不给默认值。
    端点变更只改档案。
  * 凭据从 aisk 的凭据后端取（engine.secrets），与查询体一起 base64 后
    经 `ssh <jump> 'bash -s'` 的 stdin 送入远程，不出现在命令行参数里。
  * 远程 curl **不能**同时用 `--config -` 和 `--data-binary @-`：两者抢同一个
    stdin，请求体会丢失并退化成 match_all。所以整段脚本走 stdin。
  * ES 索引按 UTC 分片，比北京时间少 8 小时，跨窗口必须拼多个日索引。
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ELK_KEYS = ("url", "user", "secret", "index_prefix", "jump")

NUM = re.compile(r"\d+")
HEX = re.compile(r"\b[0-9a-fA-F]{8,}\b")
QUOT = re.compile(r"'[^']*'|\"[^\"]*\"")
CLS = re.compile(r"at com\.etjbooks\.hbxhzt\.([A-Za-z0-9_.$]+)\.([A-Za-z0-9_$]+)\.")


def die(msg):
    print("ERROR: %s" % msg, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- 内核、档案与凭据
def _kernel_candidates():
    """内核位置不写死：AISK_KERNEL → 脚本所在的内核检出 → aisk link 记下的位置 → PATH 上的 aisk。

    AISK_HOME 是 aisk 的数据目录（档案与 kernel-root 指针在那），不是内核目录。
    """
    if os.environ.get("AISK_KERNEL"):
        yield Path(os.environ["AISK_KERNEL"]).expanduser()
    yield from Path(__file__).resolve().parents
    pointer = Path(os.environ.get("AISK_HOME") or Path.home() / ".aisk") / "kernel-root"
    try:
        recorded = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""
    if recorded:
        yield Path(recorded)
    exe = shutil.which("aisk")
    if exe:
        yield Path(exe).resolve().parent.parent


def _engine_root():
    """定位 aisk 内核检出（engine/ 所在处）。"""
    for cand in _kernel_candidates():
        if (cand / "engine" / "__init__.py").is_file():
            return str(cand)
    die("找不到 agent-skills 内核：设置 AISK_KERNEL=<内核目录>，或在内核目录执行一次 aisk link")


def _engine():
    root = _engine_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from engine import profile, secrets
    except Exception as e:
        die("导入 aisk 内核失败：%s（请用 ~/.aisk/venv/bin/python 运行）" % e)
    return profile, secrets


def _elk_of(prof, env):
    cfg = (prof.get("envs") or {}).get(env)
    return cfg.get("elk") if isinstance(cfg, dict) else None


def _resolve_profile(profile, env, explicit):
    """--profile / AISK_PROFILE → 当前所在仓库 → 唯一配了 envs.<env>.elk 的档案。

    技能目录不在任何项目仓库里，按仓库反查必然落空，这时「谁配了 ELK」就是答案；
    内核明确拒绝猜测（当前仓库同时属于多个档案）时不往下兜底。
    """
    explicit = explicit or os.environ.get("AISK_PROFILE")
    try:
        return profile.resolve(explicit=explicit)
    except profile.ProfileError as e:
        if explicit or "拒绝猜测" in str(e):
            die(str(e))
        reason = str(e).splitlines()[0]
    hits = []
    for path in profile.list_profiles():
        try:
            prof = profile.load(path)
        except Exception:  # noqa: BLE001  坏档案不该拖垮别的项目
            continue
        if isinstance(_elk_of(prof, env), dict):
            hits.append(prof)
    if len(hits) == 1:
        return hits[0], "当前目录判定不了档案，它是唯一配了这一段的"
    if hits:
        die("%s\n多个档案都配了 envs.%s.elk（%s），拒绝猜测：用 --profile 指定"
            % (reason, env, ", ".join(p["project"] for p in hits)))
    die("档案 envs.%s.elk 未配置（url/user/secret/index_prefix/jump）：现有档案都没有这一段" % env)


def endpoint(env, explicit_profile=None):
    """端点全部来自档案：envs.<env>.elk 与 envs.<env>.k8s.ns。缺项报错退出，不给默认值。"""
    profile, _ = _engine()
    prof, why = _resolve_profile(profile, env, explicit_profile)
    try:
        profile.get_env(prof, env)
    except profile.ProfileError as e:
        die(str(e))
    elk = _elk_of(prof, env)
    missing = [k for k in ELK_KEYS if not (isinstance(elk, dict) and elk.get(k))]
    if missing:
        note = "，缺 %s" % "/".join(missing) if isinstance(elk, dict) else ""
        die("档案 envs.%s.elk 未配置（url/user/secret/index_prefix/jump）%s：%s" % (env, note, prof["_path"]))
    k8s = prof["envs"][env].get("k8s")
    ep = dict(elk, ns=k8s.get("ns") if isinstance(k8s, dict) else None)
    print("端点取自档案 %s 的 envs.%s.elk（%s）：跳板 %s，索引 %s-*，命名空间 %s"
          % (prof["project"], env, why, ep["jump"], ep["index_prefix"], ep["ns"] or "未配置"), file=sys.stderr)
    return ep


def get_secret(key):
    _, secrets = _engine()
    val = secrets.get(key)
    if not val:
        die("取不到凭据 %s（检查 aisk secret list）" % key)
    return val


# ---------------------------------------------------------------- 传输
def es(ep, path, body=None, method="GET", timeout=240):
    pw_b64 = base64.b64encode(get_secret(ep["secret"]).encode()).decode()
    body_b64 = ""
    if body is not None:
        body_b64 = base64.b64encode(
            json.dumps(body, ensure_ascii=False).encode()).decode()
    script = (
        "PW=$(printf '%s' '{pw}' | base64 -d)\n"
        "BODY=$(printf '%s' '{body}' | base64 -d)\n"
        "curl -s -m 200 -u \"{user}:$PW\" -X {method} "
        "'{es}{path}' -H 'Content-Type: application/json' --data-binary \"$BODY\"\n"
    ).format(pw=pw_b64, body=body_b64, user=ep["user"], method=method,
             es=ep["url"].rstrip("/"), path=path)
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ep["jump"], "bash -s"],
        input=script, capture_output=True, text=True, timeout=timeout,
    )
    try:
        return json.loads(p.stdout)
    except Exception:
        die("ES 返回非 JSON：%s | stderr=%s" % (p.stdout[-400:], p.stderr[-300:]))


def window_norm(window):
    """接受 `6h` / `24h` / `now-24h` 三种写法，统一成 ES 的 `now-24h`。"""
    w = (window or "").strip()
    if not w.startswith("now-"):
        w = "now-" + w
    if not re.fullmatch(r"now-\d+[hdm]", w):
        die("window 格式应为 <N>h / <N>d / <N>m（如 24h、2h），或 now-<N>h")
    return w


def window_days(window):
    m = re.fullmatch(r"now-(\d+)([hdm])", window_norm(window))
    n, unit = int(m.group(1)), m.group(2)
    hours = n * {"h": 1, "d": 24, "m": 1 / 60.0}[unit]
    return int(hours // 24) + 1


def indices(ep, window):
    """按 UTC 日期拼出窗口覆盖的索引名。"""
    pref = ep["index_prefix"]
    today = datetime.now(timezone.utc).date()
    days = window_days(window)
    return ",".join("%s-%s" % (pref, (today - timedelta(days=i)).strftime("%Y.%m.%d"))
                    for i in range(days))


def err_filter(window):
    return {"bool": {"filter": [
        {"range": {"@timestamp": {"gte": window, "lte": "now"}}},
        {"match_phrase": {"message": '"level":"ERROR"'}}]}}


def norm(m, n=140):
    m = HEX.sub("<hex>", m or "")
    m = NUM.sub("N", m)
    m = QUOT.sub("'X'", m)
    return re.sub(r"\s+", " ", m).strip()[:n]


def parse_msg(raw):
    try:
        j = json.loads(raw)
    except Exception:
        return {"level": "?", "logger": "?", "message": raw[:200], "stack_trace": ""}
    if not j.get("level") and "stack_trace" not in j:
        j["level"] = "?"
    return j


# ---------------------------------------------------------------- 子命令
def cmd_stat(a):
    idx = indices(a.ep, a.window)
    body = {"size": 0, "track_total_hits": True, "query": err_filter(a.window),
            "aggs": {
                "svc": {"terms": {"field": "kubernetes.deployment.name.keyword", "size": 60}},
                "img": {"terms": {"field": "container.image.name.keyword", "size": 60}}}}
    r = es(a.ep, "/%s/_search" % idx, body)
    if "aggregations" not in r:
        die(json.dumps(r, ensure_ascii=False)[:800])
    print("窗口 %s   ERROR 总数: %d" % (a.window, r["hits"]["total"]["value"]))
    print("\n=== 按服务 ===")
    for b in r["aggregations"]["svc"]["buckets"]:
        print("  %-22s %7d" % (b["key"], b["doc_count"]))
    print("\n=== 按镜像 ===")
    for b in r["aggregations"]["img"]["buckets"]:
        print("  %-74s %7d" % (b["key"].split("/")[-1], b["doc_count"]))


def _fetch(ep, window, size=1000):
    idx = indices(ep, window)
    docs, after = [], None
    while True:
        body = {"size": size, "track_total_hits": False,
                "_source": ["@timestamp", "message", "kubernetes.deployment.name",
                            "container.image.name"],
                "sort": [{"@timestamp": "asc"}, {"_id": "asc"}],
                "query": err_filter(window)}
        if after:
            body["search_after"] = after
        r = es(ep, "/%s/_search" % idx, body)
        hits = r.get("hits", {}).get("hits", [])
        if not hits:
            break
        for h in hits:
            h["_source"]["_id"] = h["_id"]
            docs.append(h["_source"])
        after = hits[-1]["sort"]
        print("  ... 已拉取 %d" % len(docs), file=sys.stderr)
        if len(hits) < size:
            break
    return docs


def _clusters(docs):
    cl = defaultdict(list)
    for s in docs:
        dep = (s.get("kubernetes") or {}).get("deployment", {}).get("name", "?")
        img = (s.get("container") or {}).get("image", {}).get("name", "").split(":")[-1]
        j = parse_msg(s.get("message", ""))
        if j.get("level") != "ERROR":
            continue
        exc = str(j.get("exception") or j.get("stack_trace") or "")
        key = (dep, j.get("logger", "?"), norm(j.get("message", "")))
        cl[key].append({"ts": s["@timestamp"], "img": img, "msg": j.get("message", ""),
                        "exc": exc[:1500], "logger": j.get("logger", "?")})
    return sorted(cl.items(), key=lambda kv: -len(kv[1]))


def cmd_fetch(a):
    docs = _fetch(a.ep, a.window)
    ranked = _clusters(docs)
    print("ERROR 文档 %d 条 → 错误簇 %d 个" % (len(docs), len(ranked)))
    out = []
    for i, ((dep, lg, nm), v) in enumerate(ranked, 1):
        ts = sorted(x["ts"] for x in v)
        print("\n[%02d] %s | %s | x%d" % (i, dep, lg.split(".")[-1], len(v)))
        print("     %s ~ %s   img=%s" % (ts[0][:19], ts[-1][:19], v[0]["img"]))
        print("     %s" % (v[0]["msg"] or "")[:180])
        if v[0]["exc"]:
            print("     %s" % v[0]["exc"].splitlines()[0][:180])
        out.append({"dep": dep, "logger": lg, "norm": nm, "n": len(v),
                    "samples": v[:3]})
    if a.out:
        with open(a.out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("\n已写出 %s" % a.out)


def cmd_recent(a):
    a.out = None
    docs = _fetch(a.ep, a.window)
    ranked = _clusters(docs)
    print("窗口 %s（仍在发生的错误）" % a.window)
    for dep, lg, nm in [(k[0], k[1], k[2]) for k, _ in ranked]:
        pass
    for (dep, lg, nm), v in ranked:
        ts = sorted(x["ts"] for x in v)
        print("\n[%s] x%d  %s ~ %s" % (dep, len(v), ts[0][:19], ts[-1][:19]))
        print("   %s" % (v[0]["msg"] or "")[:180])
        if v[0]["exc"]:
            print("   %s" % v[0]["exc"].splitlines()[0][:180])


def cmd_img_timeline(a):
    svcs = [s.strip() for s in a.services.split(",") if s.strip()]
    idx = indices(a.ep, a.window)
    for svc in svcs:
        body = {"size": 0, "track_total_hits": False,
                "query": {"bool": {"filter": [
                    {"term": {"kubernetes.deployment.name.keyword": svc}},
                    {"range": {"@timestamp": {"gte": a.window, "lte": "now"}}}]}},
                "aggs": {"tl": {"terms": {"field": "container.image.name.keyword", "size": 8},
                                "aggs": {"first": {"min": {"field": "@timestamp"}},
                                         "last": {"max": {"field": "@timestamp"}}}}}}
        r = es(a.ep, "/%s/_search" % idx, body)
        if "aggregations" not in r:
            print("%-14s 查询失败" % svc)
            continue
        rows = sorted((b["first"]["value_as_string"][:19], b["last"]["value_as_string"][:19],
                       b["key"].split(":")[-1], b["doc_count"])
                      for b in r["aggregations"]["tl"]["buckets"])
        print("=== %s ===" % svc)
        for f, l, tag, n in rows:
            print("   %s → %s  %-26s %d 条" % (f, l, tag, n))


def cmd_xref(a):
    repo = os.path.expanduser(a.repo)
    if not os.path.isdir(repo):
        die("仓库不存在：%s" % repo)
    docs = _fetch(a.ep, a.window)
    cls_cnt = defaultdict(int)
    for (dep, lg, nm), v in _clusters(docs):
        for x in v:
            for m in CLS.finditer(x["exc"]):
                cls_cnt[m.group(2)] += len(v)
    ls = subprocess.run(["git", "-C", repo, "ls-files", "*.java"],
                        capture_output=True, text=True).stdout.splitlines()
    idx = defaultdict(list)
    for rel in ls:
        if "/src/main/" in rel:
            idx[os.path.basename(rel)[:-5]].append(rel)
    print("%-9s %-32s %-14s %s" % ("错误数", "类", "最近提交人", "提交"))
    print("-" * 128)
    for simple, cnt in sorted(cls_cnt.items(), key=lambda kv: -kv[1]):
        for rel in idx.get(simple, [])[:2]:
            info = subprocess.run(["git", "-C", repo, "log", "-1",
                                   "--format=%an|%ad|%s", "--date=short", "--", rel],
                                  capture_output=True, text=True).stdout.strip()
            author = info.split("|")[0] if info else "?"
            mark = " ★" if a.author and author == a.author else ""
            print("%-9d %-32s %-14s %s%s" % (cnt, simple[:32], author,
                                             info.split("|", 2)[-1][:40], mark))


def main():
    p = argparse.ArgumentParser(description="ELK 生产错误日志只读查询与归因（端点取自 aisk 项目档案）")
    p.add_argument("--env", default="prod", help="档案里的环境名，读 envs.<env>.elk（默认 prod）")
    p.add_argument("--profile", default=None,
                   help="档案名；省略时按 AISK_PROFILE → 当前所在仓库 → 唯一配了 elk 的档案判定")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        s = sub.add_parser(name, **kw)
        s.set_defaults(fn=fn)
        return s

    s = add("stat", cmd_stat, help="ERROR 总量与按服务/镜像分布")
    s.add_argument("--window", default="now-24h")

    s = add("recent", cmd_recent, help="短窗口内仍在发生的错误")
    s.add_argument("--window", default="now-2h")

    s = add("fetch", cmd_fetch, help="拉取并聚类，可落盘 JSON")
    s.add_argument("--window", default="now-24h")
    s.add_argument("--out", default=None)

    s = add("img-timeline", cmd_img_timeline, help="服务镜像切换时间线")
    s.add_argument("--services", required=True)
    s.add_argument("--window", default="now-48h")

    s = add("xref", cmd_xref, help="错误类 → 最近提交人 交叉验证")
    s.add_argument("--window", default="now-24h")
    s.add_argument("--repo", required=True)
    s.add_argument("--author", default=None, help="命中该作者时标 ★")

    a = p.parse_args()
    if getattr(a, "window", None):
        a.window = window_norm(a.window)
    a.ep = endpoint(a.env, a.profile)
    a.fn(a)


if __name__ == "__main__":
    main()
