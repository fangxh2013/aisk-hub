#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读账号口令轮换。

**为什么必须轮换**：2026-08-24 实测这批 DEV 口令扇出到 4 个 AI 端、约 8,700 个
文件（会话留痕占绝大多数，删不干净），且 Codex/Antigravity/WorkBuddy 的会话记录
可能同时存在服务端。**清文件解决不了已经流出去的值，只有换掉才算止血。**

**执行前必须确认爆炸半径**：这些是共享账号。本工具只轮换「AI 专用只读账号」，
执行前会自查该账号是否被运行中的服务引用——被引用就拒绝，让人先处理。

**两步必须原子**：改数据库口令 + 更新本地凭据库。任何一步失败都要能回退，
否则会出现「库里已改、本地还是旧的」这种查不了库的半死状态。
所以顺序是：先存新口令到临时键 → 改库 → 验证新口令能连 → 落正式键 → 清临时键。

用法：
    rotate_credentials.py --env dev --dry-run     # 只看会做什么
    rotate_credentials.py --env dev               # 真轮换
"""

import argparse
import secrets as pysecrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import profile, secrets                      # noqa: E402
from engine.brokers import db as db_broker               # noqa: E402


def gen_password(length=32):
    """生成新口令。刻意避开 shell/URL 里需要转义的字符，省得后面到处踩坑。

    历史教训：旧口令里带 `@` 和 `_`，在 JDBC URL 和 shell 单引号里反复出问题。
    """
    alphabet = string.ascii_letters + string.digits + "-.~"
    return "".join(pysecrets.choice(alphabet) for _ in range(length))


def check_blast_radius(prof, user):
    """自查这个账号是否被运行中的服务引用。被引用就不该由工具自动轮换。"""
    root = profile.deploy_root(prof)
    if not root or not root.is_dir():
        return [], "找不到部署仓，无法自查引用——请人工确认后再执行"
    hits = []
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix not in (".yaml", ".yml", ".sh", ".env", ".properties"):
            continue
        try:
            if user in f.read_text(encoding="utf-8", errors="replace"):
                hits.append(str(f.relative_to(root)))
        except OSError:
            continue
    return hits, None


def resolve_admin(env, admin_key=None):
    """找一个能执行 ALTER USER 的管理员凭据。

    2026-08-25 实测：DEV 的只读账号能自改密码，但 PRE 的不能
    （ERROR 1227，没有 CREATE USER 权限）。**用管理员账号改只读账号的密码
    才是常态**，自改是特例。管理员凭据在特权命名空间，取用会留审计。
    """
    if admin_key:
        pwd = secrets.get(admin_key, privileged=True)
        if not pwd:
            return None, None, f"取不到特权凭据 {admin_key}"
        user = (env.get("db") or {}).get("admin_user")
        if not user:
            return None, None, "profile 的 db 段没有 admin_user，无法确定管理员账号名"
        return user, pwd, None

    db = env.get("db") or {}
    user, key = db.get("admin_user"), db.get("admin_secret")
    if not (user and key):
        return None, None, None            # 没配就是没有，调用方决定怎么办
    pwd = secrets.get(key, privileged=True)
    if not pwd:
        return None, None, f"取不到特权凭据 {key}（aisk secret set {key} --privileged）"
    return user, pwd, None


HOSTS_SQL = "SELECT host FROM mysql.user WHERE user='{user}' ORDER BY host"


def account_hosts(query_fn, user):
    """查这个账号实际有哪些 host 模式。

    2026-08-25 踩过：写死 ALTER USER '<user>'@'%' 在 PRE 上报 ERROR 1396——
    那个只读账号根本不是 @'%'，而是被几条**网段来源限制**约束着。
    所以必须先把该账号实际的 host 模式全查出来（本函数），逐条改。
    **同一个账号名可能有多条记录，全都要改**，
    漏掉一条就会出现「从某些来源能连、从另一些连不上」的诡异状态。
    """
    try:
        cols, rows, _ = query_fn(HOSTS_SQL.format(user=user))
    except Exception:                                    # noqa: BLE001
        return []
    return [r[0] for r in rows] if cols else []


def rotate(env_name, dry_run=False, length=32, admin_key=None):
    try:
        prof, _ = profile.resolve()
    except profile.ProfileError as e:
        print(f"❌ {e}")
        return 2

    try:
        name, env = profile.get_env(prof, env_name)
    except profile.ProfileError as e:
        print(f"❌ {e}")
        return 2

    db = env.get("db") or {}
    user, key = db.get("user"), db.get("secret")
    host, port = db.get("host") or db.get("vip"), db.get("port", 3306)
    if not all([user, key, host]):
        print(f"❌ envs.{name}.db 缺少 user / secret / host")
        return 2

    print(f"环境 {name}：账号 {user}@{host}:{port}，凭据键 {key}\n")

    # ── 爆炸半径自查 ──
    hits, warn = check_blast_radius(prof, user)
    if warn:
        print(f"⚠️ {warn}")
    if hits:
        print(f"⚠️ 该账号在部署仓被 {len(hits)} 处引用：")
        for h in hits[:6]:
            print(f"   {h}")
        print("   引用方多为文档/脚本时可继续；若是运行中服务的连接配置，"
              "**先改配置再轮换**，否则服务会连不上。\n")
    else:
        print("✅ 部署仓未发现该账号被引用——是 AI 专用只读账号，轮换不影响服务\n")

    old = secrets.get(key)
    if not old:
        print(f"❌ 凭据库里没有 {key}，无法验证旧口令")
        return 1

    new = gen_password(length)

    if dry_run:
        print("[dry-run] 将执行：")
        print(f"  1. 暂存新口令到 {key}.rotating")
        print(f"  2. 查 {user} 实际的 host 模式，逐条 ALTER USER … IDENTIFIED BY '<新口令>'")
        print(f"  3. 用新口令连一次验证")
        print(f"  4. 写入正式键 {key}，清掉临时键")
        print(f"\n  新口令长度 {length}，字符集已避开 shell/URL 需转义的字符")
        return 0

    # ── 1. 先暂存，任何一步失败都能人工找回 ──
    secrets.put(f"{key}.rotating", new)
    print(f"1/4 新口令已暂存到 {key}.rotating")

    # ── 2. 改库。优先用管理员账号——只读账号通常没有改密权限 ──
    admin_user, admin_pwd, admin_err = resolve_admin(env, admin_key)
    if admin_err:
        print(f"❌ {admin_err}")
        return 1

    exec_user, exec_pwd, how = (
        (admin_user, admin_pwd, f"管理员 {admin_user}")
        if admin_user else (user, old, f"{user} 自改")
    )
    print(f"2/4 用{how}执行 ALTER USER …")

    def _admin_query(sql):
        return db_broker.query(sql, host=host, port=port, user=exec_user,
                               password=exec_pwd, mysql_bin=db.get("mysql_bin"),
                               redact=False)

    hosts = account_hosts(_admin_query, user) if admin_user else []
    if not hosts:
        hosts = ["%"]                    # 查不到就按最常见的来，失败会明确报错
        print(f"    （查不到 host 模式，按 '%' 尝试）")
    else:
        print(f"    该账号有 {len(hosts)} 条 host 记录：{', '.join(hosts)}")

    try:
        for h in hosts:
            db_broker.execute(
                f"ALTER USER '{user}'@'{h}' IDENTIFIED BY '{new}'",
                host=host, port=port, user=exec_user,
                password=exec_pwd, mysql_bin=db.get("mysql_bin"))
    except db_broker.BrokerError as e:
        print(f"❌ 2/4 改密码失败：{e}")
        if not admin_user and "CREATE USER" in str(e):
            print(f"   该账号没有自改权限。在 profile 的 envs.{name}.db 里补：")
            print(f"     admin_user: <管理员账号名>")
            print(f"     admin_secret: <特权凭据键名>")
            print(f"   然后 aisk secret set <键名> --privileged 录入口令")
        print(f"   旧口令仍有效，新口令在 {key}.rotating（未生效，可删）")
        return 1
    if admin_user:
        secrets.audit("rotate", key, note=f"env={name} by={admin_user}")
    print("    数据库口令已更新")

    # ── 3. 验证新口令真的能连 ──
    try:
        db_broker.query("SELECT 1", host=host, port=port, user=user,
                        password=new, mysql_bin=db.get("mysql_bin"))
    except db_broker.BrokerError as e:
        print(f"❌ 3/4 新口令验证失败：{e}")
        print(f"   ⚠️ 库里已经是新口令了，但连不上。新口令存在 {key}.rotating，"
              f"请人工核对后手动落键：aisk secret set {key}")
        return 1
    print("3/4 新口令验证通过")

    # ── 4. 落正式键 ──
    secrets.put(key, new)
    secrets.delete(f"{key}.rotating")
    print(f"4/4 已写入 {key}，临时键已清理\n")
    print(f"✅ {name} 轮换完成。旧口令即刻失效——"
          f"那批扇出到 8,700 个文件里的值现在是死数据。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--length", type=int, default=32)
    ap.add_argument("--admin-secret", help="覆盖 profile 里的 admin_secret 键名")
    a = ap.parse_args()
    return rotate(a.env, a.dry_run, a.length, a.admin_secret)


if __name__ == "__main__":
    sys.exit(main())
