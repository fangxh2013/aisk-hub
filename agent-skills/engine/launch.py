# -*- coding: utf-8 -*-
"""带 TLS 预热的应用启动器。

**为什么需要**：2026-08-24 实测 Antigravity 黑屏的完整因果链——
  googleapis.com TLS 握手 9.5s × 3 个端点
    → 语言服务器初始化 32~36s
    → Electron 加载 UI 只等 30s，超时 ERR_TIMED_OUT
    → 放弃后不再重试 → 永久黑屏

只差 2~6 秒。TCP 连接本身只要 0.015s，慢的纯粹是 TLS 握手阶段
（境内 0.18s / 境外非 Google 1.2~5.6s / Google 9.5s，按目的地梯度恶化）。

预热的原理：先把这些 TLS 会话建起来，让后续握手能复用会话票据/连接，
把启动时的握手开销挪到应用启动之前，从而赢下那个 30 秒竞态。

预热失败不阻塞启动——网络不通时照样把应用拉起来，只是可能还会黑屏。
"""

import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from . import miniyaml

CONFIG = Path(os.environ.get("AISK_HOME") or (Path.home() / ".aisk")) / "launch.yaml"

DEFAULT = {
    "apps": {
        "antigravity": {
            "app": "Antigravity",
            "prewarm": [
                "https://daily-cloudcode-pa.googleapis.com/",
                "https://generativelanguage.googleapis.com/",
                "https://jetski-webchannel.googleapis.com/",
            ],
            "prewarm_timeout": 20,
            "note": "Electron 加载本地 UI 只等 30s；语言服务器初始化慢于此就黑屏",
        }
    }
}


class LaunchError(Exception):
    pass


def load_config():
    if CONFIG.is_file():
        data = miniyaml.load_file(CONFIG)
        if data.get("apps"):
            return data
    return DEFAULT


def _warm_one(url, timeout):
    """建立一次完整 TLS 连接。只关心握手能不能快，不关心返回什么。"""
    host = urlsplit(url).netloc
    t0 = time.time()
    ctx = ssl.create_default_context()
    # 目标是建立会话，404/403 都算成功；只有连不上才算失败
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aisk-prewarm"})
        urllib.request.urlopen(req, timeout=timeout, context=ctx).read(1)
        return host, time.time() - t0, "ok"
    except urllib.error.HTTPError:
        return host, time.time() - t0, "ok"          # 有 HTTP 响应即说明 TLS 通了
    except Exception as e:                            # noqa: BLE001
        return host, time.time() - t0, type(e).__name__


def prewarm(urls, timeout=20, out=print):
    if not urls:
        return []
    out(f"预热 {len(urls)} 个端点（并行，超时 {timeout}s）…")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        results = list(pool.map(lambda u: _warm_one(u, timeout), urls))
    for host, dur, status in results:
        mark = "✅" if status == "ok" else "⚠️"
        extra = "" if status == "ok" else f"  ({status})"
        out(f"  {mark} {host:<40} {dur:.2f}s{extra}")
    out(f"预热合计 {time.time()-t0:.2f}s（并行，取最慢的那个）")
    return results


def open_app(name):
    """启动应用。mac 用 open -a，Windows 用 start，Linux 试 xdg-open。"""
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", name], check=True)
    elif sys.platform.startswith("win"):
        subprocess.run(["cmd", "/c", "start", "", name], check=True)
    else:
        exe = shutil.which(name) or name
        subprocess.Popen([exe], start_new_session=True)


def launch(app_key, cfg=None, skip_prewarm=False, out=print):
    cfg = cfg or load_config()
    apps = cfg.get("apps") or {}
    if app_key not in apps:
        raise LaunchError(f"launch.yaml 里没有 {app_key}。现有：{', '.join(apps) or '(空)'}")
    spec = apps[app_key] or {}
    app_name = spec.get("app") or app_key
    if spec.get("note"):
        out(f"说明：{spec['note']}")
    if not skip_prewarm:
        prewarm(spec.get("prewarm") or [], int(spec.get("prewarm_timeout", 20)), out)
    out(f"启动 {app_name} …")
    open_app(app_name)
    return app_name


# netcheck 的默认目标：分层对照，一眼看出慢在哪一段
NETCHECK_TARGETS = [
    ("https://www.baidu.com/", "境内基准"),
    ("https://api.anthropic.com/", "境外非 Google"),
    ("https://github.com/", "境外非 Google"),
    ("https://generativelanguage.googleapis.com/", "Antigravity 启动必需"),
    ("https://daily-cloudcode-pa.googleapis.com/", "Antigravity 启动必需"),
    ("https://jetski-webchannel.googleapis.com/", "Antigravity 启动必需"),
]

# Electron 加载本地 UI 的超时线。语言服务器初始化超过这个数就黑屏。
ELECTRON_LOAD_TIMEOUT = 30


def _tls_timing(url, timeout=20):
    """分段测量：TCP 连接 vs TLS 握手。区分「链路不通」和「握手被拖慢」。"""
    parts = urlsplit(url)
    host = parts.netloc
    port = 443
    if ":" in host:
        host, port = host.rsplit(":", 1)
        port = int(port)
    import socket
    t0 = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"host": host, "connect": None, "tls": None, "err": type(e).__name__}
    t_conn = time.time() - t0
    t1 = time.time()
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=host):
            pass
        return {"host": host, "connect": t_conn, "tls": time.time() - t1, "err": None}
    except Exception as e:  # noqa: BLE001
        return {"host": host, "connect": t_conn, "tls": None, "err": type(e).__name__}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def netcheck(out=print, timeout=20):
    """测各目的地的 TLS 握手耗时，并判断 Antigravity 会不会黑屏。

    切换代理节点后重跑本命令对比——配置文件是加密的，没法自动枚举节点，
    只能这样一次一个地量。
    """
    out(f"{'目标':<44}{'TCP':>9}{'TLS握手':>11}   说明")
    out("-" * 84)
    google = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda t: (_tls_timing(t[0], timeout), t[1]), NETCHECK_TARGETS))
    for r, tag in results:
        if r["err"]:
            out(f"  {r['host']:<42}{'—':>9}{'失败':>11}   {tag} ({r['err']})")
            continue
        tls = r["tls"]
        out(f"  {r['host']:<42}{r['connect']:>8.3f}s{tls:>10.2f}s   {tag}")
        if "googleapis.com" in r["host"]:
            google.append(tls)

    if google:
        est = sum(google)
        out("")
        out(f"三个 Google 端点 TLS 合计 {est:.1f}s")
        if est >= ELECTRON_LOAD_TIMEOUT:
            out(f"❌ 超过 Electron 的 {ELECTRON_LOAD_TIMEOUT}s 超时线 —— Antigravity 大概率黑屏")
            out("   → 在代理里给 *.googleapis.com 换个更快的节点，再重跑本命令")
        elif est >= ELECTRON_LOAD_TIMEOUT * 0.7:
            out(f"⚠️ 逼近 {ELECTRON_LOAD_TIMEOUT}s 超时线，仍可能黑屏（还要算上认证等其他开销）")
        else:
            out(f"✅ 远低于 {ELECTRON_LOAD_TIMEOUT}s 超时线，不该黑屏")
    return 0


def write_default_config():
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists():
        return False
    lines = [
        "# aisk launch 的应用定义。",
        "# 预热的意义见 engine/launch.py 顶部说明：",
        "# 某些应用启动时要连境外端点，TLS 握手慢会导致 UI 加载超时黑屏。",
        "",
        "apps:",
    ]
    for k, v in DEFAULT["apps"].items():
        lines.append(f"  {k}:")
        lines.append(f"    app: {v['app']}")
        lines.append(f"    prewarm_timeout: {v['prewarm_timeout']}")
        lines.append(f"    note: \"{v['note']}\"")
        lines.append("    prewarm:")
        for u in v["prewarm"]:
            lines.append(f"      - {u}")
    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True
