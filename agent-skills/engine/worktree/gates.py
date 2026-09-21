# -*- coding: utf-8 -*-
"""构建门禁策略：maven-modules / npm-build / command / none。所有参数来自档案 worktrees.repos.<仓>.gate。

构建信号量沿用旧引擎的锁名（build-maven 供 Maven 与前端构建共用、build-cmd 供命令门禁），迁移期新旧引擎共享同一配额。

maven-modules 的范围规则：
- 改动文件 → 最近的含 pom.xml 的模块目录；根 pom 改动 → 全量；模块外文件（脚本、SQL、文档）→ 不编译。
- 命中 gate.also_dependents 里声明的路径（如公共模块）时追加 -amd，把依赖它的下游模块一起编译。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import names
from .config import IS_WIN, WtConfig, WtError
from .registry import atomic_json, now_iso, semaphore


def path_match(path, patterns):
    path = path.replace("\\", "/")
    for pat in patterns:
        pat = pat.replace("\\", "/")
        if fnmatch.fnmatchcase(path, pat):
            return True
        if pat.endswith("/**") and (path == pat[:-3] or path.startswith(pat[:-2])):
            return True
    return False


def java_home_for(gate):
    """jdk 版本来自档案；Windows 必须在档案给出 jdk_home，mac 用系统 java_home 查询。"""
    if gate.get("jdk_home"):
        return str(gate["jdk_home"])
    version = str(gate.get("jdk") or "")
    if not version:
        return os.environ.get("JAVA_HOME", "")
    if not IS_WIN and Path("/usr/libexec/java_home").exists():
        r = subprocess.run(["/usr/libexec/java_home", "-v", version], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    return ""


def maven_modules(worktree, changed):
    worktree = Path(worktree)
    full, mods = False, set()
    for p in changed:
        parts = Path(p).parts
        found = None
        for i in range(len(parts) - 1, -1, -1):
            d = Path(*parts[:i]) if i > 0 else Path(".")
            if (worktree / d / "pom.xml").exists():
                found = d
                break
        if found is None:
            continue
        if str(found) == ".":
            full = full or p == "pom.xml"
            continue
        mods.add(found.as_posix())
    return full, sorted(mods)


def run_logged(cmd, cwd, env, log):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"\n===== {now_iso()} $ {' '.join(cmd)}  (cwd={cwd})\n")
        fh.flush()
        r = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=fh, stderr=subprocess.STDOUT)
        fh.write(f"===== exit={r.returncode}\n")
    return r.returncode


def tail(log, n=30):
    try:
        return "\n".join(Path(log).read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _tm_exclude(paths):
    if IS_WIN or not Path("/usr/bin/tmutil").exists():
        return
    for p in paths:
        if Path(p).exists():
            try:
                # tmutil 在受管控/未授权的 macOS 环境可能等待系统服务，不能让
                # aisk task check 永久挂住并表现成“所有 worktree 都不能提交”。
                subprocess.run(["/usr/bin/tmutil", "addexclusion", str(p)], capture_output=True, timeout=15)
            except subprocess.TimeoutExpired:
                # 排除备份失败不改变代码门禁结论；只让构建继续并把原因写入后续日志。
                continue
            except OSError:
                continue


LOCKFILES = (("pnpm-lock.yaml", "pnpm", ["install", "--frozen-lockfile"]),
             ("yarn.lock", "yarn", ["install", "--frozen-lockfile"]),
             ("package-lock.json", "npm", ["ci", "--no-audit", "--no-fund"]))


def package_manager(worktree, gate=None):
    """按锁文件判断包管理器；档案 gate.package_manager / gate.install 可覆盖。返回 (程序, 安装参数)。"""
    gate = gate or {}
    worktree = Path(worktree)
    if gate.get("install"):
        argv = [str(a) for a in gate["install"]]
        return argv[0], argv[1:]
    if gate.get("package_manager"):
        pm = str(gate["package_manager"])
        for _lock, name, args in LOCKFILES:
            if name == pm:
                return pm, args
        raise WtError(f"不支持的包管理器 {pm}")
    for lock, name, args in LOCKFILES:
        if (worktree / lock).exists():
            return name, args
    return "npm", ["ci", "--no-audit", "--no-fund"]


def executable(name):
    return f"{name}.cmd" if IS_WIN and name in ("npm", "pnpm", "yarn", "mvn") else name


def ensure_node_modules(worktree, log, gate=None):
    worktree = Path(worktree)
    dst = worktree / "node_modules"
    if dst.is_symlink():
        raise WtError(f"node_modules 不允许软链接共享：{dst}")
    manifests = [worktree / n for n in ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", ".npmrc")]
    digest = hashlib.sha256(b"".join(m.name.encode() + (m.read_bytes() if m.exists() else b"")
                                     for m in manifests)).hexdigest()
    marker = dst / names.DEPS_MARKER
    if marker.is_file():
        try:
            if json.loads(marker.read_text(encoding="utf-8")).get("digest") == digest:
                return "依赖清单一致，复用本任务独立 node_modules"
        except (ValueError, OSError):
            pass
    pm, args = package_manager(worktree, gate)
    if run_logged([executable(pm)] + args, worktree, os.environ.copy(), log) != 0:
        raise WtError(f"{pm} 安装依赖失败，见 {log}")
    atomic_json(marker, {"digest": digest})
    _tm_exclude([dst])
    return f"已按当前清单 {pm} 安装依赖"


def run_gate(cfg: WtConfig, alias, worktree, changed, log, clean=False):
    """返回 (ok, 摘要)。"""
    gate = cfg.repo(alias).gate
    kind = gate.get("kind", "none")
    worktree = Path(worktree)
    if kind == "none":
        return True, "该仓库无构建门禁"
    if kind == "command":
        argv = gate.get("argv")
        if not isinstance(argv, list) or not argv:
            raise WtError(f"{alias}.gate.argv 必须是非空列表")
        with semaphore(cfg, "build-cmd", cfg.quota_builds):
            rc = run_logged([str(a) for a in argv], worktree, os.environ.copy(), log)
        return rc == 0, "门禁命令通过" if rc == 0 else f"门禁命令失败（exit {rc}）"
    if kind == "maven-modules":
        precheck = gate.get("precheck")
        when = gate.get("precheck_when") or []
        if precheck and when and any(path_match(p, when) for p in changed):
            script = worktree / precheck
            if not script.is_file():
                return False, f"缺少预检脚本 {precheck}"
            if run_logged(["bash", str(script)], worktree, os.environ.copy(), log) != 0:
                return False, f"预检失败：{precheck}"
        full, mods = maven_modules(worktree, changed)
        if not full and not mods:
            return True, "改动不涉及 Maven 模块（脚本/SQL/文档），无需编译"
        env = os.environ.copy()
        jh = java_home_for(gate)
        java = str(Path(jh) / "bin" / ("java.exe" if IS_WIN else "java")) if jh else shutil.which("java")
        if not java:
            return False, "找不到 JDK，请在档案 gate.jdk_home 配置或设置 JAVA_HOME"
        want = str(gate.get("jdk") or "")
        if want:
            v = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=15)
            if v.returncode or not re.search(r'version "' + re.escape(want) + r'(?:[."]|$)', v.stdout + v.stderr):
                return False, f"门禁要求 JDK{want}，当前 Java 版本不匹配"
        if jh:
            env["JAVA_HOME"] = jh
        if cfg.raw.get("maven_args"):
            env["MAVEN_ARGS"] = (env.get("MAVEN_ARGS", "") + " " + str(cfg.raw["maven_args"])).strip()
        mvn = executable("mvn")
        dependents = gate.get("also_dependents") or []
        amd = bool(dependents) and any(path_match(p, dependents) for p in changed)
        goals = ["clean", "compile"] if clean else ["compile"]
        cmd = [mvn, "-B", "-q", "-DskipTests"]
        if not full:
            cmd += ["-pl", ",".join(mods), "-am"] + (["-amd"] if amd else [])
        cmd += goals
        with semaphore(cfg, "build-maven", cfg.quota_builds):
            rc = run_logged(cmd, worktree, env, log)
        _tm_exclude([worktree / m / "target" for m in mods] + [worktree / "target"])
        scope = "全量" if full else "、".join(mods) + ("（含下游 -amd）" if amd else "")
        return rc == 0, f"编译通过：{scope}" if rc == 0 else f"编译失败：{scope}"
    if kind == "npm-build":
        if not [p for p in changed if not p.endswith(".md")]:
            return True, "只改了文档，无需构建"
        note = ensure_node_modules(worktree, log, gate)
        pm, _ = package_manager(worktree, gate)
        with semaphore(cfg, "build-maven", cfg.quota_builds):
            rc = run_logged([executable(pm), "run", str(gate.get("script") or "build")], worktree, os.environ.copy(), log)
        return rc == 0, f"前端构建通过（{note}）" if rc == 0 else f"前端构建失败（{note}）"
    raise WtError(f"未知门禁类型 {kind}")
