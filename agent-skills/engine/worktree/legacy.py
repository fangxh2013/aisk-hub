# -*- coding: utf-8 -*-
"""把旧引擎（泳道 + 槽位号）的登记导入为新任务，不改名、不移动、不碰分支。

- 只导入本系统、未归档、未排除的槽位；排除规则 = 档案 worktrees.legacy.exclude + 命令行 --exclude（fnmatch）；
- 新任务保留旧目录、旧分支 ai/<泳道>/<任务号>、旧端口，登记 legacy_id 以便 find/claim 用旧编号查到；
- 默认只预览；--apply 写登记簿；--rewrite-files 额外按新模板重写任务须知（保留 TASK/PROGRESS/HANDOFF）；
- 旧登记文件原样保留：导入后请只用新引擎操作这些任务。
"""
from __future__ import annotations

import fnmatch
import json
import re

from . import bind, model, tasks
from .config import WtError
from .registry import say


# 旧引擎把提交说明格式写死在代码里（要求含中文），不在 config.json 中
LEGACY_MESSAGE_PATTERN = r"^[a-z]+(?:\([^)]+\))?: .*[\u4e00-\u9fff]"
LEGACY_GATES = {"maven": "maven-modules", "npm": "npm-build", "none": "none"}


def compare_config(cfg, old):
    """档案 worktrees 一节与旧 policy/config.json 逐键比对（阶段 2 切换前置条件）。返回差异列表，空 = 一致。
    只比对新引擎仍在使用的键；泳道、自治级别等已被任务模型取代的键不比。"""
    diffs = []

    def cmp(label, have, want):
        if have != want:
            diffs.append(f"{label}：档案 {have!r}，旧配置 {want!r}")

    cmp("integration_branch", cfg.integration, old.get("integration_branch"))
    cmp("protected", sorted(cfg.protected), sorted(old.get("protected_branches") or []))
    prefixes = cfg.push_block_prefixes
    if len(prefixes) > 1:
        diffs.append("push_block_prefix：旧配置只支持一个前缀，档案声明了多个")
    cmp("push_block_prefix", prefixes[0] if len(prefixes) == 1 else "", old.get("gitea_prefix") or "")
    cmp("commit.max_chars", cfg.max_msg_chars, old.get("max_msg_chars"))
    cmp("commit.ai_names", cfg.ai_re.pattern, old.get("ai_name_regex"))
    cmp("AI 邮箱规则", cfg.ai_email_re.pattern, old.get("ai_email_regex"))
    cmp("commit.pattern", unescape_regex(cfg.message_re.pattern), unescape_regex(LEGACY_MESSAGE_PATTERN))
    cmp("repo_order", cfg.repo_order, old.get("repo_order"))
    cmp("sensitive_paths", sorted(cfg.sensitive_paths), sorted(old.get("sensitive_paths") or []))
    cmp("maven_args", str(cfg.raw.get("maven_args") or ""), (old.get("maven_env") or {}).get("MAVEN_ARGS", ""))
    ports = old.get("ports") or {}
    cmp("ports.base", cfg.port_base, ports.get("base"))
    cmp("ports.block", cfg.port_block, ports.get("per_slot"))
    quotas = (old.get("quotas") or {}).get(cfg.os) or {}
    cmp("quotas.active", cfg.quota_active, quotas.get("active_slots"))
    cmp("quotas.builds", cfg.quota_builds, quotas.get("maven"))
    old_repos = old.get("repos") or {}
    for alias in sorted(set(old_repos) | set(cfg.repos)):
        if alias not in cfg.repos:
            diffs.append(f"repos.{alias}：档案缺少该仓库")
            continue
        if alias not in old_repos:
            diffs.append(f"repos.{alias}：旧配置没有该仓库")
            continue
        o, rc = old_repos[alias], cfg.repo(alias)
        if cfg.os == "mac":
            cmp(f"repos.{alias} 目录名", rc.path.name, o.get("dir"))
        cmp(f"repos.{alias}.trunk", rc.trunk, o.get("trunk"))
        cmp(f"repos.{alias}.push_branch", rc.push_branch, o.get("push_branch"))
        cmp(f"repos.{alias}.promote", rc.promote, o.get("promote"))
        cmp(f"repos.{alias}.anchors", list(rc.anchors), list(o.get("anchors") or []))
        cmp(f"repos.{alias}.hooks_required", rc.hooks_required, bool(o.get("hooks_required", False)))
        kind = LEGACY_GATES.get(str(o.get("gate") or "none"), str(o.get("gate")))
        cmp(f"repos.{alias}.gate.kind", rc.gate.get("kind", "none"), kind)
        npm = o.get("npm_build") or []
        if kind == "npm-build" and len(npm) == 3 and npm[:2] == ["npm", "run"]:
            cmp(f"repos.{alias}.gate.script", rc.gate.get("script"), npm[2])
        java_cmd = ((old.get("roots") or {}).get("mac") or {}).get("java17_home_cmd")
        if kind == "maven-modules" and java_cmd and cfg.os == "mac":
            cmp(f"repos.{alias}.gate.jdk", str(rc.gate.get("jdk") or ""), str(java_cmd[-1]))
    protected = {str(profile_expand(p)) for p in (cfg.raw.get("guard_protected") or [])}
    for d in old.get("legacy_dirs") or []:
        if str(profile_expand(d)) not in protected:
            diffs.append(f"guard_protected：缺少旧方案目录 {d}")
    return diffs


def unescape_regex(pattern):
    """\\uXXXX 与字面字符在正则里等价；档案解析器会把前者转成后者，比对前统一。"""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), pattern or "")


def profile_expand(p):
    from .. import profile as profile_mod
    return profile_mod._expand(p)


def legacy_config(cfg):
    if not cfg.legacy_root:
        return None
    f = cfg.legacy_root / "policy" / "config.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def legacy_slug(old, cfg):
    raw = str(old.get("task") or old.get("id") or "task")
    parts = raw.split("-", 1)
    body = parts[1] if len(parts) == 2 and parts[0].isdigit() else raw
    return model.slug_from_name(body, cfg.ai_re)


def load_legacy(cfg):
    if not cfg.legacy_root:
        raise WtError("档案没有声明 worktrees.legacy.root（旧引擎根目录）")
    slots_dir = cfg.legacy_root / "state" / "slots"
    out = []
    if slots_dir.exists():
        for f in sorted(slots_dir.glob("*.json")):
            out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def plan_import(cfg, reg, exclude=()):
    patterns = list(cfg.legacy_exclude) + list(exclude or [])
    imported = {t.get("legacy_id") for t in reg.all(include_archived=True, include_hub=False)}
    rows = []
    for old in load_legacy(cfg):
        sid = old.get("id", "?")
        if old.get("state") == "archived":
            reason = "已归档"
        elif old.get("os") != cfg.os:
            reason = f"属于 {old.get('os')}"
        elif sid in imported:
            reason = "已导入"
        elif any(fnmatch.fnmatchcase(sid, p) or fnmatch.fnmatchcase(old.get("task", ""), p) for p in patterns):
            reason = "按规则排除"
        else:
            reason = None
        rows.append((old, reason))
    return rows


def cmd_import_legacy(cfg, reg, args):
    rows = plan_import(cfg, reg, args.exclude)
    todo = [old for old, reason in rows if reason is None]
    for old, reason in rows:
        title = old.get("title") or old.get("task")
        say("info" if reason else "ok", f"{old.get('id')}（{old.get('state')}）{title}：{reason or '将导入'}")
    if not todo:
        say("info", "没有需要导入的旧槽位")
        return 0
    if not args.apply:
        say("warn", "以上为预览；确认后 aisk task import-legacy --apply [--rewrite-files]")
        return 0
    mapping = {t["legacy_id"]: t["id"] for t in reg.all(include_archived=True, include_hub=False) if t.get("legacy_id")}
    created = []
    with reg.lock():
        for old in sorted(todo, key=lambda o: (bool(o.get("base_slot")), o.get("created_at") or "")):
            tid = reg.next_id()
            slug = legacy_slug(old, cfg)
            ports = old.get("ports") or {}
            block = (int(ports.get("base", cfg.port_base)) - cfg.port_base) // max(1, cfg.port_block)
            task = {
                "id": tid, "slug": slug, "name": f"{tid}-{slug}", "title": (old.get("title") or old.get("task"))[:30],
                "goal": "", "accept": "", "os": old.get("os"), "dir": old["dir"], "port_block": block, "ports": ports,
                "base_task": mapping.get(old.get("base_slot")), "scope": old.get("scope") or [],
                "repos": old.get("repos") or {}, "source_branches": {}, "state": old.get("state"), "owner": None,
                "created_at": old.get("created_at"), "history": old.get("history") or [], "legacy": True,
                "legacy_id": old.get("id"), "base_short": old.get("base_short"),
            }
            for key in ("check", "land", "verify", "last_reject"):
                if old.get(key) is not None:
                    task[key] = old[key]
            reg.save(task)
            reg.event("import-legacy", task=tid, legacy_id=old.get("id"))
            mapping[old.get("id")] = tid
            created.append(task)
    for task in created:
        if args.rewrite_files:
            bind.write_task_files(cfg, task, tasks.java_home(cfg, list(task["repos"])), keep_notes=True)
        say("ok", f"{task['legacy_id']} → {task['id']}（目录与分支不变）")
    say("warn", "导入后请只用新引擎操作这些任务；旧登记文件保留未动")
    tasks.refresh_board(cfg, reg)
    return 0
