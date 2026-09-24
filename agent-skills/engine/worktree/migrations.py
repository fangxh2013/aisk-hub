# -*- coding: utf-8 -*-
"""有序迁移的落地不变量：本次新增迁移的版本号必须大于集成分支上同组已有的最大版本。

worktree 隔离的是文件，隔离不了全局有序的版本号。Flyway 这类迁移的版本号在写代码时就定了，
生效顺序却由落地顺序决定：并行任务里先写、后落地的迁移，版本号会比集成分支上已有的更小。
执行端关闭乱序执行时，它在已经跑过更高版本的环境里会被校验拦下。2026-09-24 实测：一天 5 次
带迁移的落地里有 2 次倒序，另有两个版本号是写成未来时间的整点。

档案按仓库声明（不声明就不检查）：

    repos.<别名>.migrations:
      paths:                        # 迁移文件所在位置（仓库相对 glob）
        - "db/migrations/**"
      version: '^V(\\d+(?:[._]\\d+)*)__'   # 可选；从文件名取版本号，第 1 个分组是版本
      group: directory              # 可选；directory = 每个目录各自排序（一库一张历史表），repo = 全仓统一
      timestamp_format: "%Y%m%d%H%M%S"   # 可选；版本号是时间戳时，拒绝写成未来时间的版本号
      max_future_minutes: 10        # 可选；配合 timestamp_format 使用

未落地的迁移没在任何共享环境执行过，改名是安全的；已经执行过的迁移不能改名，这里也从不检查它们。
"""
from __future__ import annotations

import datetime as _dt
import posixpath
import re

from . import gitops as git
from .gates import path_match


def _key(version):
    return tuple(int(part) for part in re.split(r"[._]", version) if part != "")


def _entry(policy, path):
    """(组, 版本号, 排序键)；不是本仓库声明的迁移文件时返回 None。"""
    if not path_match(path, policy["paths"]):
        return None
    match = re.search(policy["version"], posixpath.basename(path))
    if not match:
        return None
    try:
        key = _key(match.group(1))
    except ValueError:
        return None
    group = posixpath.dirname(path) if policy.get("group", "directory") == "directory" else ""
    return group, match.group(1), key


def _existing(repo, ref, policy):
    """ref 上每组已有迁移的最大 (排序键, 版本号, 路径)。"""
    names = [p for p in git.out(["ls-tree", "-r", "-z", "--name-only", ref], cwd=repo).split("\0") if p]
    top = {}
    for path in names:
        entry = _entry(policy, path)
        if entry is None:
            continue
        group, version, key = entry
        if group not in top or key > top[group][0]:
            top[group] = (key, version, path)
    return top


def _added(repo, old, new, policy):
    out = git.out(["diff", "--name-only", "-z", "--diff-filter=AR", old, new], cwd=repo)
    rows = []
    for path in (p for p in out.split("\0") if p):
        entry = _entry(policy, path)
        if entry is not None:
            rows.append((path, *entry))
    return rows


def _rename_plan(rows, top, policy, now):
    """给出保持原相对顺序的改名命令：同组全部新增迁移整体排到已有最大版本与当前时间之后。"""
    fmt = policy.get("timestamp_format")
    if not fmt:
        return []
    plan = []
    for group in sorted({row[1] for row in rows}):
        mine = sorted((row for row in rows if row[1] == group), key=lambda row: row[3])
        start = now.replace(microsecond=0)
        if group in top:
            try:
                start = max(start, _dt.datetime.strptime(top[group][1], fmt) + _dt.timedelta(seconds=1))
            except ValueError:
                return []
        for offset, (path, _group, version, _key_) in enumerate(mine):
            fresh = (start + _dt.timedelta(seconds=offset)).strftime(fmt)
            name = posixpath.basename(path)
            target = posixpath.join(posixpath.dirname(path), name.replace(version, fresh, 1))
            plan.append(f"git mv {path} {target}")
    return plan


def violations(cfg, alias, repo, target_ref, old, new, now=None, label=None):
    """old..new 新增的迁移违反顺序时返回问题列表（每条可直接给人看）；未声明 migrations 返回空。

    target_ref 是将要落到的分支此刻的位置，old..new 是本次要落地的改动，label 是给人看的分支名。"""
    policy = cfg.repo(alias).migrations
    if not policy:
        return []
    rows = _added(repo, old, new, policy)
    if not rows:
        return []
    now = now or _dt.datetime.now()
    label = label or target_ref
    top = _existing(repo, target_ref, policy)
    problems, bad_groups = [], set()
    for path, group, version, key in rows:
        if group in top and key <= top[group][0]:
            problems.append(f"{path} 的版本 {version} 不大于 {label} 上同组已有的 {top[group][1]}"
                            f"（{posixpath.basename(top[group][2])}）")
            bad_groups.add(group)
    fmt = policy.get("timestamp_format")
    if fmt:
        limit = now + _dt.timedelta(minutes=int(policy.get("max_future_minutes", 10)))
        for path, group, version, _key_ in rows:
            try:
                stamp = _dt.datetime.strptime(version, fmt)
            except ValueError:
                continue
            if stamp > limit:
                problems.append(f"{path} 的版本 {version} 是未来时间（现在 {now.strftime(fmt)}），"
                                "请用仓库的迁移生成器取当前时间")
                bad_groups.add(group)
    if not problems:
        return []
    plan = _rename_plan([row for row in rows if row[1] in bad_groups], top, policy, now)
    if plan:
        problems.append("未落地的迁移没在任何环境执行过，可以按原相对顺序整体改名后重新 check/ready：\n    "
                        + "\n    ".join(plan))
    else:
        problems.append("未落地的迁移没在任何环境执行过，按原相对顺序改成更大的版本号后重新 check/ready")
    return problems
