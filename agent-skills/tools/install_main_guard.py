#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 main 保护 hook 装到各仓库，并保留既有 pre-push。

**为什么要链式而不是覆盖**：业务仓库的 pre-push 可能是 lefthook 等工具生成的。
直接覆盖会在下次 lefthook install 时被还原，保护静默消失——
而「以为有保护其实没有」正是这次事故的成因，不能再复制一次。

同一个原因，安装结果必须如实报告：
- 仓库设了 core.hooksPath 时 git 根本不执行 .git/hooks，只能报「未生效」；
  那个目录通常是团队跟踪的文件，这里不去改它；
- 分发脚本写的是内核的绝对路径。内核目录搬家后重跑本工具即可改指向；
  在那之前分发脚本明确报错并拒绝推送，而不是静默放行。
"""

import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "hooks" / "pre-push"
MARK = "HBXH_ALLOW_MAIN_PUSH"  # 守卫本体的特征：目标文件就是守卫本身时视为已装
DISPATCH_MARK = "aisk guard-main dispatch"
LEGACY_DISPATCH_MARK = "由 agent-skills 安装：先跑 main 保护"  # 早期分发脚本没有 DISPATCH_MARK

DISPATCH = """#!/bin/sh
# aisk guard-main dispatch：由 agent-skills 安装，先跑 main 保护，再跑仓库原有的 pre-push。
# 原有 hook 保存在 pre-push.orig；本文件被 lefthook 等工具覆盖、或内核目录搬家后，
# 重新执行 aisk guard-main 即可恢复。
guard={guard}
if [ ! -x "$guard" ]; then
    echo "main 保护脚本不存在或不可执行：$guard" >&2
    echo "内核目录搬家后请重新执行 aisk guard-main；在此之前拒绝推送。" >&2
    exit 1
fi
# git 从标准输入给出待推送的引用；守卫和原有 hook 都要读，先存下来各喂一份
refs=$(cat)
feed() {{ [ -n "$refs" ] && printf '%s\\n' "$refs"; }}
feed | "$guard" "$@" || exit $?
orig="$(dirname "$0")/pre-push.orig"
[ -x "$orig" ] || exit 0
feed | "$orig" "$@"
"""


def git_dir(repo):
    """worktree 的 .git 是文件，要解析到真实目录；hooks 由主仓库共享。"""
    g = repo / ".git"
    if g.is_file():
        line = g.read_text(encoding="utf-8").strip()
        if line.startswith("gitdir:"):
            g = Path(line.split(":", 1)[1].strip())
            # worktree 的 gitdir 指向 <main>/.git/worktrees/<name>
            if g.name and g.parent.name == "worktrees":
                g = g.parent.parent
    return g if g.is_dir() else None


def is_dispatch(text):
    return DISPATCH_MARK in text or LEGACY_DISPATCH_MARK in text


def hooks_path_override(repo, hooks):
    """core.hooksPath 让 git 不再执行 hooks 目录时，返回它的设置值；否则 None。"""
    r = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
                       capture_output=True, text=True)
    value = r.stdout.strip()
    if r.returncode != 0 or not value:
        return None
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        configured = repo / configured
    return None if configured.resolve() == hooks.resolve() else value


def install(repo):
    """返回 (是否生效, 说明)；不是 git 仓库时是否生效为 None。"""
    gd = git_dir(repo)
    if gd is None:
        return None, f"跳过 {repo.name}：不是 git 仓库"
    hooks = gd / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "pre-push"
    orig = hooks / "pre-push.orig"
    notes = []

    if target.is_file():
        text = target.read_text(encoding="utf-8", errors="replace")
        if MARK in text and not is_dispatch(text):
            return True, f"已装 {repo.name}（守卫本体）"
        if not is_dispatch(text) and not orig.exists():
            shutil.copy2(target, orig)
            orig.chmod(orig.stat().st_mode | stat.S_IEXEC)
    # 早期版本重复安装时会把分发脚本自己存成 pre-push.orig，执行时无限自调用；它不含任何原有内容
    if orig.is_file() and is_dispatch(orig.read_text(encoding="utf-8", errors="replace")):
        orig.unlink()
        notes.append("移除了自指的 pre-push.orig")

    target.write_text(DISPATCH.format(guard=shlex.quote(str(GUARD))), encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR | stat.S_IWUSR)
    suffix = f"（{'；'.join(notes)}）" if notes else ""
    override = hooks_path_override(repo, hooks)
    if override:
        return False, (f"⚠️ {repo.name}：未生效，core.hooksPath={override}，git 不执行 {hooks}；"
                       f"本仓库的 main 保护只能靠仓库自带钩子与权限层{suffix}")
    return True, f"✅ {repo.name}{suffix}"


def main(argv=None):
    roots = list(sys.argv[1:] if argv is None else argv)
    if not roots:
        print("用法：install_main_guard.py <仓库或包含仓库的目录>...（aisk guard-main 省略参数时取项目档案里的仓库）")
        return 2
    seen, inactive = [], []
    for root in roots:
        rp = Path(root).expanduser()
        if not rp.is_dir():
            print(f"  跳过 {rp}：目录不存在")
            continue
        cands = [rp] if (rp / ".git").exists() else sorted(
            d for d in rp.iterdir() if d.is_dir() and (d / ".git").exists())
        for c in cands:
            active, message = install(c)
            print("  " + message)
            if active is None:
                continue
            seen.append(c)
            if not active:
                inactive.append(c.name)
    if not seen:
        print("  没找到任何仓库")
        return 1
    print(f"\n共处理 {len(seen)} 个仓库。hook 只在同时有 origin/dev 与 origin/main 的仓库生效。")
    if inactive:
        print(f"其中 {len(inactive)} 个仓库的 .git/hooks 不会被 git 执行：{', '.join(inactive)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
