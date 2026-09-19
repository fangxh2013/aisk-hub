# -*- coding: utf-8 -*-
"""把「只在本机、没有版本管理」的那部分快照进一个 git 仓库。

**为什么不是整目录进 git**：任务数据根里 99% 是团队仓库的完整检出（本机实测 3.8GB），
公司源码不放个人账号，锚点与 hub 还是嵌套 git 仓库，根本提交不进去。
真正需要异地副本的是剩下那 100KB：任务登记簿与项目档案——丢了就没人知道谁在做什么、基线在哪。

**绝不快照**：凭据密钥（age/GPG）、未加密口令、任何仓库检出。加密后的 SOPS 文件默认也不带，
要带得显式 --secrets（密钥仍然只在本机，仓库里只有密文）。
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import profile as profile_mod
from .worktree import gitops

SKIP_DIR_NAMES = {".git", "node_modules", "target", "dist", "__pycache__"}
KERNEL_ROOT = Path(__file__).resolve().parents[1]


def _copy_tree(src: Path, dst: Path):
    if not src.is_dir():
        return 0
    count = 0
    for item in sorted(src.rglob("*")):
        if any(part in SKIP_DIR_NAMES for part in item.relative_to(src).parts):
            continue
        target = dst / item.relative_to(src)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            count += 1
    return count


def _wip_snapshot(worktree: Path, dst: Path):
    """未提交的改动：补丁 + 未跟踪文件原样复制。它们只在本机，丢了没有第二份。"""
    diff = subprocess.run(["git", "-C", str(worktree), "diff", "--binary"], capture_output=True, text=True)
    untracked = subprocess.run(["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True).stdout.split()
    if not diff.stdout.strip() and not untracked:
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "changes.patch").write_text(diff.stdout, encoding="utf-8")
    branch = subprocess.run(["git", "-C", str(worktree), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (dst / "README.md").write_text(
        f"# 未提交的在制品快照\n\n- 来源：`{worktree}`\n- 分支：`{branch}`\n- HEAD：`{head}`\n\n"
        f"恢复：在同一分支上 `git apply changes.patch`，再把 `untracked/` 下的文件拷回去。\n", encoding="utf-8")
    for rel in untracked:
        source = worktree / rel
        if source.is_file():
            target = dst / "untracked" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return 1 + len(untracked)


def discover_worktrees(repo):
    """repo 登记的全部工作区（主检出 + linked worktree），只返回目录仍在的。

    repo 不是 git 仓库时返回空列表，由调用方决定怎么提示，不让整个备份失败。
    """
    try:
        items = gitops.worktree_list(repo)
    except (gitops.WtError, OSError):
        return []
    return [Path(w["path"]) for w in items if not w["bare"] and Path(w["path"]).is_dir()]


def snapshot(dest: Path, worktrees=(), include_secrets=False, discover_from=KERNEL_ROOT):
    """把档案、各项目的任务状态、未提交在制品抄进 dest，返回 (文件数, 明细)。

    在制品先扫描 discover_from 仓库的全部 worktree，再合并 worktrees 显式给的目录（按 resolve 后的路径去重）。
    **依赖「每次都记得传全」的备份等于没有备份**：wip/ 每次整体重建，只写回本次列出的目录。
    2026-09-17 另一个会话跑了一次不带 --wip 的 aisk backup，内核里未跟踪的技能就从最新快照里消失了。
    discover_from=None 关掉自动发现（测试不该扫描真实内核仓库）。
    """
    dest = Path(dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    detail, total = [], 0
    home = profile_mod.runtime_root()

    for name in ("profiles", "state", "wip", "secrets"):
        stale = dest / name
        if stale.is_dir():
            shutil.rmtree(stale)  # 全量覆盖：删掉的任务不该留在备份里冒充还在

    count = _copy_tree(profile_mod.PROFILE_DIR, dest / "profiles")
    detail.append(f"项目档案 {count} 份")
    total += count

    for path in sorted(profile_mod.list_profiles()):
        prof = profile_mod.load(path)
        wt = prof.get("worktrees") or {}
        root = wt.get("data_root")
        if not root:
            continue
        state = profile_mod._expand(root) / "state"
        if not state.is_dir():
            continue
        count = _copy_tree(state, dest / "state" / prof["project"])
        detail.append(f"{prof['project']} 任务状态 {count} 个文件")
        total += count

    discovered = []
    if discover_from is not None:
        discovered = discover_worktrees(discover_from)
        if not discovered:
            detail.append(f"⚠️ 没能从 {discover_from} 发现 worktree（不是 git 仓库或 git 不可用），它的在制品不在这次快照里")
    seen = set()
    for worktree in [*discovered, *worktrees]:
        worktree = Path(worktree).expanduser()
        key = worktree.resolve()
        if key in seen:
            continue
        seen.add(key)
        count = _wip_snapshot(worktree, dest / "wip" / worktree.name)
        if count:
            detail.append(f"{worktree.name} 未提交改动 {count} 项")
            total += count

    if include_secrets:
        vault = dest / "secrets"
        vault.mkdir(parents=True, exist_ok=True)
        for item in sorted(home.glob("*.sops.yaml")):  # 只带密文；age/GPG 密钥永远留在本机
            shutil.copy2(item, vault / item.name)
            total += 1
            detail.append(f"加密凭据 {item.name}")
    return total, detail


def commit(dest: Path, push=False):
    """有变化才提交。返回 (是否有提交, 说明)。"""
    dest = Path(dest).expanduser()
    if not (dest / ".git").exists():
        return False, f"{dest} 不是 git 仓库：先 git init 或 git clone 到这里"
    status = subprocess.run(["git", "-C", str(dest), "status", "--porcelain"], capture_output=True, text=True).stdout
    if not status.strip():
        return False, "与上次快照一致，无需提交"
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    message = f"chore: 状态快照 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    subprocess.run(["git", "-C", str(dest), "commit", "-q", "-m", message], check=True)
    if push:
        r = subprocess.run(["git", "-C", str(dest), "push"], capture_output=True, text=True)
        if r.returncode != 0:
            return True, f"已提交但推送失败：{r.stderr.strip()[-200:]}"
        return True, "已提交并推送"
    return True, "已提交（未推送，加 --push 推到远端）"
