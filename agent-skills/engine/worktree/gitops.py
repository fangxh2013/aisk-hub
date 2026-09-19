# -*- coding: utf-8 -*-
"""git 调用封装。所有命令带 core.quotepath=false（中文文件名），输出按 UTF-8 解码；不继承调用方的 GIT_DIR 等变量。"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import names
from .config import IS_WIN, WtError

_GIT = None


def git_exe():
    global _GIT
    if _GIT:
        return _GIT
    exe = os.environ.get(names.ENV_GIT)
    if not exe and not IS_WIN and Path("/usr/bin/git").exists():
        exe = "/usr/bin/git"
    _GIT = exe or "git"
    return _GIT


def run(args, cwd=None, check=True, input_text=None, optional_locks=True, env_extra=None):
    cmd = [git_exe(), "-c", "core.quotepath=false"] + [str(a) for a in args]
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANGUAGE"] = "C"
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    if not optional_locks:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, input=input_text,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        raise WtError(f"git {' '.join(str(a) for a in args)} 失败（{cwd}）：\n{detail}")
    return r


def out(args, cwd=None, **kw):
    return run(args, cwd=cwd, **kw).stdout.strip()


def ok(args, cwd=None, **kw):
    return run(args, cwd=cwd, check=False, **kw).returncode == 0


def version():
    m = re.search(r"(\d+)\.(\d+)", out(["version"]))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def sha(cwd, ref):
    r = run(["rev-parse", "-q", "--verify", f"{ref}^{{commit}}"], cwd=cwd, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def is_ancestor(cwd, a, b):
    return ok(["merge-base", "--is-ancestor", a, b], cwd=cwd)


def merge_base(cwd, a, b):
    r = run(["merge-base", a, b], cwd=cwd, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def current_branch(cwd):
    r = run(["symbolic-ref", "-q", "--short", "HEAD"], cwd=cwd, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def dirty(cwd):
    r = run(["status", "--porcelain=v1", "--untracked-files=all"], cwd=cwd, optional_locks=False)
    return [l for l in r.stdout.splitlines() if l.strip()]


def dirty_paths(cwd):
    records = run(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=cwd,
                  optional_locks=False).stdout.split("\0")
    paths, i = [], 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec:
            continue
        paths.append(rec[3:])
        if "R" in rec[:2] or "C" in rec[:2]:
            if i < len(records) and records[i]:
                paths.append(records[i])
            i += 1
    return paths


def diff_names(cwd, a, b):
    return [p for p in run(["diff", "--name-only", "-z", a, b], cwd=cwd).stdout.split("\0") if p]


def commit_time(cwd, ref):
    r = run(["log", "-1", "--format=%ct", ref, "--"], cwd=cwd, check=False)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def log_messages(cwd, rng, no_merges=True):
    args = ["log", "--format=%H%x00%B%x01", rng]
    if no_merges:
        args.insert(1, "--no-merges")
    items = []
    for rec in run(args, cwd=cwd).stdout.split("\x01"):
        rec = rec.strip("\n")
        if rec:
            h, _, body = rec.partition("\0")
            items.append((h, body.strip()))
    return items


def worktree_list(repo):
    items, cur = [], {}
    for line in out(["worktree", "list", "--porcelain"], cwd=repo).splitlines() + [""]:
        if not line.strip():
            if cur:
                items.append(cur)
                cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": val, "branch": None, "detached": False, "bare": False, "locked": None, "prunable": None}
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.replace("refs/heads/", "", 1)
        elif key == "detached":
            cur["detached"] = True
        elif key == "bare":
            cur["bare"] = True
        elif key == "locked":
            cur["locked"] = val or "locked"
        elif key == "prunable":
            cur["prunable"] = val or "prunable"
    return items


def admin_dir_of(wt):
    gf = Path(wt) / ".git"
    if not gf.is_file():
        return None
    try:
        content = gf.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    p = content[len("gitdir:"):].strip()
    path = Path(p)
    if not path.is_absolute() and not p.startswith("//"):
        path = Path(wt) / p
    return path


def backlink_ok(wt):
    """串线检测：worktree 的 .git → 管理目录，管理目录的 gitdir 必须指回这个 worktree。"""
    adm = admin_dir_of(wt)
    if adm is None:
        return False, "没有 .git 文件"
    if str(adm).startswith("//"):
        return False, f"管理目录是网络路径写法 {adm}"
    if not adm.exists():
        return False, f"管理目录不存在 {adm}"
    gd = adm / "gitdir"
    if not gd.exists():
        return False, f"管理目录缺 gitdir 文件 {adm}"
    back = gd.read_text(encoding="utf-8", errors="replace").strip()
    try:
        same = Path(back).resolve() == (Path(wt) / ".git").resolve()
    except OSError:
        same = False
    if not same:
        return False, f"串线：管理目录 {adm.name} 登记的是 {back}"
    return True, adm.name


def merge_tree(repo, ours, theirs):
    r = run(["merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs], cwd=repo, check=False)
    if r.returncode not in (0, 1):
        raise WtError(f"git merge-tree 失败：{(r.stderr or r.stdout).strip()}")
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if not lines:
        raise WtError("git merge-tree 没有输出树对象")
    return r.returncode == 0, lines[0].strip(), sorted(set(lines[1:]))


def commit_tree(repo, tree, parents, message):
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    args += ["-F", "-"]
    return run(args, cwd=repo, input_text=message.rstrip("\n") + "\n").stdout.strip()


def reflog_entries(repo, ref, limit=200):
    r = run(["reflog", "show", "--format=%H%x09%gs", "-n", str(limit), ref, "--"], cwd=repo, check=False)
    if r.returncode != 0:
        return []
    return [(h.strip(), s.strip()) for h, _, s in (l.partition("\t") for l in r.stdout.splitlines())]


def config_get(repo, key, *scope):
    r = run(["config", *scope, "--get", key], cwd=repo, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def config_regexp(repo, pattern, *scope):
    r = run(["config", *scope, "--get-regexp", pattern], cwd=repo, check=False)
    return [l for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else []


def branch_exists(repo, name):
    return ok(["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], cwd=repo)


def common_dir(path):
    return Path(out(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path))


def registration_dir(repo, wt_path):
    """按 worktree 路径找它在主仓 worktrees/ 下的管理目录；worktree 目录已经不存在时也能找到。"""
    base = common_dir(repo) / "worktrees"
    want = os.path.normcase(os.path.abspath(os.path.join(str(wt_path), ".git")))
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        try:
            text = (d / "gitdir").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if os.path.normcase(os.path.abspath(text)) == want:
            return d
    return None


def worktree_add_unique(repo, final_path, admin_name, start, branch=None, detach=False, reason=""):
    """以唯一基名创建再移动到最终路径，得到稳定且不复用的管理目录名，并加锁；中途失败回滚。"""
    final_path = Path(final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = final_path.parent / admin_name
    if tmp.exists() or final_path.exists():
        raise WtError(f"目标路径已存在：{final_path if final_path.exists() else tmp}")
    if (common_dir(repo) / "worktrees" / admin_name).exists():
        raise WtError(f"管理目录名 {admin_name} 已被占用，拒绝复用")
    existed_branch = bool(branch and branch_exists(repo, branch))
    args = ["worktree", "add"]
    if detach:
        args += ["--detach", str(tmp), start]
    elif branch:
        args += [str(tmp), branch] if existed_branch else ["-b", branch, str(tmp), start]
    else:
        args += [str(tmp), start]
    run(args, cwd=repo)
    try:
        if tmp != final_path:
            run(["worktree", "move", str(tmp), str(final_path)], cwd=repo)
        run(["worktree", "lock", "--reason", reason or f"aisk task {admin_name}", str(final_path)], cwd=repo)
        good, detail = backlink_ok(final_path)
        if not good:
            raise WtError(detail)
    except Exception:
        actual = final_path if final_path.exists() else tmp
        run(["worktree", "unlock", str(actual)], cwd=repo, check=False)
        run(["worktree", "remove", "--force", str(actual)], cwd=repo, check=False)
        if branch and not existed_branch:
            run(["branch", "-D", branch], cwd=repo, check=False)
        raise
    return final_path
