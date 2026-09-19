#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把旧 credentials.local.md 的 KEY=VALUE 迁进 aisk 凭据后端。

**绝不打印任何口令值**——只输出键名与成功/失败。值从读取到写入全程在内存里，
不经过 stdout、不进 shell 历史、不进模型上下文。

旧文件是 markdown 里夹 KEY=VALUE 行的格式（历史遗留）。本脚本按行提取，
按下面的映射改成 aisk 的分层键名（env/服务/角色），映射不到的保留原名小写。

用法：
    python3 tools/import_legacy_credentials.py --src <credentials.local.md> --dry-run
    python3 tools/import_legacy_credentials.py --src <credentials.local.md>
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import secrets  # noqa: E402

# 旧键名 → aisk 分层键名。只映射真正的口令类，URL/USER 这类非机密不迁。
KEY_MAP = {
    "DEV_MYSQL_READ_PASS": "dev/mysql/ro",
    "DEV_NACOS_READ_PASS": "dev/nacos/ro",
    "MYSQL_READ_PRE_PASSWORD": "pre/mysql/ro",
    "PROD_MYSQL_READ_PASS": "prod/mysql/ro",
    "PROD_NACOS_READ_PASS": "prod/nacos/ro",
    "DEV_REDIS_PASS": "dev/redis/pass",
    "DEV_ES_PASS": "dev/es/pass",
    "DEV_JENKINS_PASS": "dev/jenkins/pass",
    "PROD_JENKINS_PASS": "prod/jenkins/pass",
    "DEV_SSH_READ_PASS": "dev/ssh/ro",
    "PROD_ES_BIZ_PASS": "prod/es-biz/pass",
    "PROD_ES_LOG_PASS": "prod/es-log/pass",
    "PROD_KIBANA_LOG_PASS": "prod/kibana-log/pass",
    "REDIS_PASSWORD_PRE": "pre/redis/pass",
    "NACOS_PASSWORD_PRE": "pre/nacos/pass",
}

# 写权限凭据 → 特权命名空间。AI 默认够不着，需显式 --privileged 且留审计。
# 判定按「账号角色」而非「像不像口令」——root/DBA/业务写/迁移工具/复制账号
# 一个都不能落进只读空间。
PRIVILEGED_PAT = re.compile(
    r"(ROOT|DBA|_APP_|FLYWAY|REPL|LOADER|ADMIN|SUPER|WRITE|_RW_)", re.I)

# 非机密（URL / 用户名）不迁进凭据库——它们属于 profile 的拓扑信息
SKIP_SUFFIX = ("_URL", "_USER", "_USERNAME", "_HOST", "_HOSTS", "_VIP")

# 只迁看起来像口令的
SECRET_HINT = re.compile(r"(PASS|PASSWORD|SECRET|TOKEN|KEY)$")


def parse(path):
    """提取 KEY=VALUE。返回 {key: value}，**调用方不得打印 value**。"""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if v:
            out[k] = v
    return out


def target_key(old):
    """返回 (新键名, 是否特权)。返回 (None, _) 表示不迁。"""
    if old.endswith(SKIP_SUFFIX):
        return None, False
    if not SECRET_HINT.search(old):
        return None, False
    priv = bool(PRIVILEGED_PAT.search(old))
    if old in KEY_MAP:
        return KEY_MAP[old], priv
    return ("privileged/" if priv else "legacy/") + old.lower(), priv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backend")
    a = ap.parse_args()

    src = Path(a.src).expanduser()
    if not src.is_file():
        print(f"❌ 找不到 {src}")
        return 1

    pairs = parse(src)
    ro, priv, skipped = [], [], []
    for k in sorted(pairs):
        t, is_priv = target_key(k)
        if not t:
            skipped.append(k)
        elif is_priv:
            priv.append((k, t))
        else:
            ro.append((k, t))

    backend = secrets.backend_name(a.backend)
    print(f"源文件 {src.name}：{len(pairs)} 个键，后端 {backend}")
    print(f"  只读命名空间 {len(ro)} 个 · 特权命名空间 {len(priv)} 个 · 跳过非机密 {len(skipped)} 个\n")

    print("→ 只读命名空间（broker 默认可取，★=已映射成分层键名）：")
    for old, new in ro:
        print(f"  {'★' if old in KEY_MAP else ' '} {old:<34} → {new}")
    print("\n→ 特权命名空间（默认够不着，需 --privileged 且留审计）：")
    for old, new in priv:
        print(f"  ⚠️ {old:<34} → {new}")

    plan = [(o, n, False) for o, n in ro] + [(o, n, True) for o, n in priv]

    if a.dry_run:
        print("\n[dry-run] 未写入任何东西")
        return 0

    ok = fail = 0
    for old, new, is_priv in plan:
        try:
            secrets.put(new, pairs[old], a.backend, privileged=is_priv)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {new}: {type(e).__name__}")
            fail += 1

    print(f"\n✅ 成功 {ok} 个，失败 {fail} 个")
    print("核对: aisk secret list          # 只读")
    print("      aisk secret list --privileged   # 特权")
    print("\n⚠️ 迁完请确认无误后再删除源文件——本脚本不会替你删。")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
