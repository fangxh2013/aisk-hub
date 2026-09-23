# -*- coding: utf-8 -*-
"""init（建锚点、门禁区、hub，登记受保护分支基线）、bind（机器级配置漂移比对与写入）、doctor（巡检）。

doctor 的原则：任何单项异常只报告、不中断；只读，除非显式 --fix / --ack-refs。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import names, bind, gitops as git, tasks
from .config import WtConfig, WtError
from .integrate import audit_branch, record_refs, report_audit
from .registry import LIVE_STATES, WORKING_STATES, Registry, now_iso, parse_iso, remove_tree, say

ANCHOR_PREFIX = names.ANCHOR_ADMIN_PREFIX
HUB_PUSHURL = "file:///dev/null/aisk-mac-never-pushes-hub"


def _same_path(a, b):
    return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()


def quarantine_stale_worktree(path):
    """隔离残留目录，保留现场后再让任务引擎重建有效 worktree。"""
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.stale-{stamp}")
    suffix = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.stale-{stamp}-{suffix}")
        suffix += 1
    path.rename(candidate)
    return candidate


# ------------------------------------------------------------------ init
def init_problems(cfg: WtConfig, reg: Registry):
    problems = []
    for alias in cfg.repo_order:
        rc = cfg.repo(alias)
        repo = rc.path
        if not repo.exists():
            problems.append(f"{alias}: 仓库不存在 {repo}")
            continue
        if git.current_branch(repo) != cfg.integration:
            problems.append(f"{alias}: 主工作区必须先切回 {cfg.integration}")
        found = audit_branch(cfg, reg, alias, repo, cfg.integration, strict=False)
        for anchor in list(rc.anchors) + list(rc.audited):
            found += audit_branch(cfg, reg, alias, repo, anchor, strict=True)
        problems += report_audit(found)
        if git.config_regexp(repo, r"^extensions\.", "--local"):
            problems.append(f"{alias}: 仍有仓库扩展；须先备份并迁移 config.worktree 设置，不能直接清除")
        for w in git.worktree_list(repo):
            if w.get("branch") in rc.anchors and Path(w["path"]).resolve() != cfg.anchor_path(alias, w["branch"]).resolve():
                gone = "" if Path(w["path"]).exists() else f"（目录已不存在，先 {names.CLI} doctor --fix 清理失效登记）"
                problems.append(f"{alias}: {w['branch']} 仍被 {w['path']} 占用{gone}")
    return problems


def cmd_init(cfg, reg, args):
    if git.version() < (2, 38):
        raise WtError("git 版本需 ≥ 2.38（merge-tree --write-tree）")
    if cfg.os == "windows":  # pragma: no cover - 仅 Windows
        return init_windows(cfg, reg, args)
    apply = args.apply
    problems = init_problems(cfg, reg)
    if problems:
        for p in problems:
            say("err", p)
        if apply:
            raise WtError("初始化预检未通过，未修改任何仓库或配置")
    if apply:
        for d in (cfg.tasks_dir, cfg.anchors_dir, cfg.task_state_dir, cfg.locks_dir, cfg.archive_dir, cfg.refs_dir,
                  cfg.logs_dir, cfg.salvage_dir, cfg.hub):
            d.mkdir(parents=True, exist_ok=True)
    for alias in cfg.repo_order:
        rc = cfg.repo(alias)
        repo = rc.path
        say("info", f"==== {alias}（{repo}）")
        if not repo.exists() or git.current_branch(repo) != cfg.integration:
            continue
        wts = git.worktree_list(repo)
        for b in rc.anchors:
            ap = cfg.anchor_path(alias, b)
            if not git.branch_exists(repo, b):
                if not git.sha(repo, f"origin/{b}"):
                    say("warn", f"本地与远端都没有 {b}，跳过该锚点")
                    continue
                if apply:
                    git.run(["branch", "--no-track", b, f"origin/{b}"], cwd=repo)
                say("ok", f"创建本地分支 {b}（来自 origin/{b}）")
            holder = [w for w in wts if w.get("branch") == b]
            if holder:
                if ap.exists() and Path(holder[0]["path"]).resolve() == ap.resolve():
                    if not holder[0].get("locked") and apply:
                        git.run(["worktree", "lock", "--reason", "aisk task anchor", str(ap)], cwd=repo)
                    say("ok", f"锚点 {b} 已在位")
                else:
                    say("err", f"分支 {b} 已被 {holder[0]['path']} 检出，锚点无法建立")
                continue
            if apply:
                git.worktree_add_unique(repo, ap, f"{ANCHOR_PREFIX}{alias}-{b}", b, branch=b, reason="aisk task anchor")
            say("ok", f"锚点 {b} → {ap}")
        gate = cfg.anchor_path(alias, "gate")
        if gate.exists() and not git.backlink_ok(gate)[0]:
            if apply:
                stale = quarantine_stale_worktree(gate)
                say("warn", f"门禁区回链失效，已保留旧目录 {stale}")
            else:
                say("warn", f"门禁区 {gate} 存在但回链失效；apply 时会隔离保留并重建")
        if not gate.exists():
            if apply:
                git.worktree_add_unique(repo, gate, f"{ANCHOR_PREFIX}{alias}-gate", cfg.integration, detach=True,
                                        reason="aisk task gate")
            say("ok", f"门禁区 → {gate}")
        bare = cfg.hub / f"{alias}.git"
        if not bare.exists():
            if apply:
                git.run(["init", "--bare", "-q", str(bare)])
                for k, v in (("gc.auto", "0"), ("receive.autogc", "false"), ("core.logAllRefUpdates", "true"),
                             ("receive.denyDeletes", "false")):
                    git.run(["config", k, v], cwd=bare)
            say("ok", f"hub 裸仓 → {bare}")
        have_hub = git.config_get(repo, "remote.hub.url")
        if not have_hub or not _same_path(have_hub, bare):
            # 已有 hub 却指向别处（数据根搬家、旧引擎目录被删）时改指向，否则 land 会一直从死路径取分支
            if apply:
                git.run(["remote", "set-url" if have_hub else "add", "hub", str(bare)], cwd=repo)
                git.run(["config", "--replace-all", "remote.hub.fetch", f"+refs/heads/{cfg.branch_prefix}*:refs/remotes/hub/{cfg.branch_prefix}*"], cwd=repo)
                git.run(["config", "--replace-all", "remote.hub.pushurl", HUB_PUSHURL], cwd=repo)
            say("ok", f"主仓只读远端 hub 改指向 {bare}（原 {have_hub}）" if have_hub else "主仓增加只读远端 hub")
        if apply:
            record_refs(reg, alias, repo, [cfg.integration] + list(rc.anchors) + list(rc.audited))
    if not apply:
        say("warn", "以上为预览；确认无误后执行 aisk task init --apply，机器级配置另用 aisk task bind 比对")
    return 1 if problems else 0


def converge_remote(repo, name, url, apply, pushurl=None):
    """把已有远端的地址改到该指向的位置；返回说明，未变化返回 None。

    对端目录搬家后，只在「远端不存在时才新建」会让旧地址一直留着——Mac 侧实测过一次：
    hub 搬了家，init 说一切正常，land 却一直从死路径取分支。
    """
    have = git.config_get(repo, f"remote.{name}.url")
    if have is None or _same_path(have, url):
        if apply and pushurl and git.config_get(repo, f"remote.{name}.pushurl") != pushurl:
            git.run(["config", "--replace-all", f"remote.{name}.pushurl", pushurl], cwd=repo)
        return None
    if apply:
        git.run(["remote", "set-url", name, str(url)], cwd=repo)
        if pushurl:
            git.run(["config", "--replace-all", f"remote.{name}.pushurl", pushurl], cwd=repo)
    return f"远端 {name} 改指向 {url}（原 {have}）"


def check_windows_remotes(cfg: WtConfig, rep):
    """Windows 侧的裸对象库：mac 与 hub 两个远端都指向 Y: 上的 Mac 目录，对端搬家后必须改指向。"""
    for alias in cfg.repo_order:
        rc = cfg.repo(alias)
        bare = rc.path
        if not bare.exists():
            continue
        for name, want in (("hub", cfg.hub / f"{alias}.git"), ("mac", rc.mac_source)):
            if not want:
                continue
            have = git.config_get(bare, f"remote.{name}.url")
            if have and not _same_path(have, want):
                rep.err(f"{alias}: 远端 {name} 指向 {have}，应为 {want}（{names.CLI} init --apply 改正）")


def init_windows(cfg, reg, args):  # pragma: no cover - 仅 Windows
    apply = args.apply
    for d in (cfg.tasks_dir, cfg.task_state_dir, cfg.locks_dir, cfg.archive_dir, cfg.logs_dir, cfg.salvage_dir):
        if apply:
            d.mkdir(parents=True, exist_ok=True)
    for alias in cfg.repo_order:
        rc = cfg.repo(alias)
        bare = rc.path
        if not rc.mac_source:
            say("err", f"{alias}: Windows 档案需声明 worktrees.repos.{alias}.mac_source")
            continue
        say("info", f"==== {alias}：{bare}")
        integration = None
        if cfg.windows_integration:
            integration = cfg.integration_repo(alias)
            if not integration.exists():
                say("err", f"{alias}: Windows 集成仓库不存在：{integration}")
                continue
            if git.current_branch(integration) != cfg.integration:
                say("err", f"{alias}: Windows 集成仓库必须位于 {cfg.integration}：{integration}")
                continue
        hub_path = str(cfg.hub / f"{alias}.git").replace("\\", "/")
        hub = cfg.hub / f"{alias}.git"
        if not hub.exists():
            if apply:
                cfg.hub.mkdir(parents=True, exist_ok=True)
                git.run(["init", "--bare", "-q", str(hub)])
                for k, v in (("gc.auto", "0"), ("receive.autogc", "false"),
                             ("core.logAllRefUpdates", "true"), ("receive.denyDeletes", "false")):
                    git.run(["config", k, v], cwd=hub)
            say("ok", f"Windows 交换 hub 裸仓 {'已创建' if apply else '待创建'} → {hub}")
        elif not (hub / "HEAD").is_file():
            say("err", f"{alias}: Windows hub 路径不是可用裸仓：{hub}")
            continue
        if apply:
            safe = git.run(["config", "--global", "--get-all", "safe.directory"], check=False).stdout.splitlines()
            if hub_path not in safe:
                git.run(["config", "--global", "--add", "safe.directory", hub_path])
        if bare.exists():
            changed = [converge_remote(bare, "hub", hub_path, apply),
                       converge_remote(bare, "mac", rc.mac_source, apply, pushurl=names.PUSH_DISABLED_URL)]
            for note in [c for c in changed if c]:
                say("ok", note)
            if not any(changed):
                say("ok", "本地裸对象库已存在")
        else:
            if apply:
                git.run(["clone", "--bare", "--no-local", "--no-tags", rc.mac_source, str(bare)])
                git.run(["remote", "rename", "origin", "mac"], cwd=bare)
                git.run(["config", "--replace-all", "remote.mac.fetch",
                         f"+refs/heads/{cfg.integration}:refs/remotes/mac/{cfg.integration}"], cwd=bare)
                git.run(["config", "--add", "remote.mac.fetch",
                         f"+refs/remotes/origin/{rc.trunk}:refs/remotes/mac/origin-{rc.trunk}"], cwd=bare)
                git.run(["config", "remote.mac.pushurl", names.PUSH_DISABLED_URL], cwd=bare)
                git.run(["remote", "add", "hub", hub_path], cwd=bare)
                git.run(["config", "remote.hub.push", f"refs/heads/{cfg.branch_prefix}*:refs/heads/{cfg.branch_prefix}*"], cwd=bare)
                for k, v in (("core.autocrlf", "false"), ("core.longpaths", "true"), ("core.symlinks", "false"),
                             ("core.filemode", "false"), ("gc.auto", "0")):
                    git.run(["config", k, v], cwd=bare)
                hooks_path = ((cfg.raw.get("repos") or {}).get(alias) or {}).get("hooks_path")
                if rc.hooks_required and hooks_path:
                    git.run(["config", "core.hooksPath", str(hooks_path)], cwd=bare)
                git.run(["fetch", "mac"], cwd=bare)
            say("ok", f"已从 {rc.mac_source} 建本地裸对象库")
        if integration:
            gate = cfg.anchor_path(alias, "gate")
            if not gate.exists():
                if apply:
                    git.worktree_add_unique(integration, gate, f"{ANCHOR_PREFIX}{alias}-gate",
                                            cfg.integration, detach=True, reason="aisk task gate")
                say("ok", f"Windows 集成门禁区 {'已创建' if apply else '待创建'} → {gate}")
    return 0


# ------------------------------------------------------------------ bind
def cmd_bind(cfg, reg, args):
    items = bind.plan(cfg)
    drift = [it for it in items if it[2] != it[3]]
    for desc, path, cur, want in items:
        say("ok" if cur == want else "warn", f"{desc}：{'一致' if cur == want else '需要更新'}（{path}）")
    if not drift:
        return 0
    if not args.apply:
        say("info", "以上为预览；确认后 aisk task bind --apply（写前自动备份到 ~/.aisk/backups/worktree/）")
        return 1
    for desc in bind.apply_plan(items):
        say("ok", f"已写入：{desc}")
    return 0


# ------------------------------------------------------------------ doctor
class Report:
    def __init__(self):
        self.errs, self.warns = [], []

    def err(self, m):
        self.errs.append(m)
        say("err", m)

    def warn(self, m):
        self.warns.append(m)
        say("warn", m)

    @staticmethod
    def ok(m):
        say("ok", m)


def safely(rep, label, fn, *a):
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001  单项巡检崩溃只报告
        rep.err(f"{label} 巡检异常：{e}")


def check_repo(cfg, reg, rep, alias, args):
    rc = cfg.repo(alias)
    repo = rc.path
    if not repo.exists():
        return
    say("info", f"==== {alias}")
    exts = git.config_regexp(repo, r"^extensions\.", "--local")
    if exts:
        rep.err(f"{alias}: 仓库开着扩展 {exts}（Antigravity 已知不兼容）")
    if cfg.os == "windows":
        return
    cur = git.current_branch(repo)
    if cur != cfg.integration:
        rep.err(f"{alias}: 主工作区在 {cur}，应在 {cfg.integration}（上面的在制品可 aisk task adopt-branch）")
    for key, want in (cfg.raw.get("pinned_git_config") or {}).items():
        want = str(want).replace("{integration}", cfg.integration)
        have = git.config_get(repo, key, "--local")
        if have is not None and have != want:
            if args.fix:
                git.run(["config", "--local", key, want], cwd=repo)
                rep.ok(f"{alias}: {key} 被串写成 {have}，已恢复为 {want}")
            else:
                rep.err(f"{alias}: {key} 被串写成 {have}（aisk task doctor --fix 恢复为 {want}）")
    have_hub = git.config_get(repo, "remote.hub.url")
    want_hub = cfg.hub / f"{alias}.git"
    if have_hub and not _same_path(have_hub, want_hub):
        rep.err(f"{alias}: 只读远端 hub 指向 {have_hub}，应为 {want_hub}（{names.CLI} init --apply 改正）")
    elif have_hub and not (want_hub / "HEAD").is_file():
        # hub 是跨系统交接的唯一通道：它不可用时 land 会取不到对端分支，必须当场报出来而不是等落地失败
        rep.err(f"{alias}: hub 裸仓 {want_hub} 不存在或不可读，跨系统交接会失败（{names.CLI} init --apply 重建）")
    for lock, hint in stale_git_locks(repo):
        rep.warn(f"{alias}: 残留 git 锁 {lock}（{hint}）；确认没有 git 进程在跑后删除它")
    by_path = {}
    for w in git.worktree_list(repo):
        p = Path(w["path"])
        if not w.get("bare") and not p.exists():
            check_dead_registration(cfg, rep, alias, repo, w, args.fix)
            continue
        by_path[str(p)] = w
        if w.get("prunable"):
            rep.warn(f"{alias}: 可清理条目（任务引擎不自动 prune）{w['path']}")
            continue
        if w.get("bare") or (p.exists() and (p / ".git").is_dir()):
            continue
        adm = git.admin_dir_of(p) if p.exists() else None
        adm_name = adm.name if adm else ""
        if adm_name.startswith((ANCHOR_PREFIX, *names.LEGACY_ANCHOR_ADMIN_PREFIXES)):
            if not w.get("locked"):
                rep.warn(f"{alias}: 锚点未加锁 {p}")
            continue
        if adm_name.startswith((names.TASK_ADMIN_PREFIX, *names.LEGACY_TASK_ADMIN_PREFIXES)):
            if not w.get("locked"):
                rep.warn(f"{alias}: 任务 worktree 未加锁 {p}")
            if w.get("branch") in cfg.protected:
                rep.err(f"{alias}: 任务 worktree {p} 检出了受保护分支 {w['branch']}")
            continue
        if not str(p.resolve()).startswith(str(cfg.data_root.resolve())):
            rep.warn(f"{alias}: 外来 worktree {p}（分支 {w.get('branch') or '游离'}）；会话结束后可 aisk task adopt 收编")
    for b in rc.anchors:
        if not git.branch_exists(repo, b) and not git.sha(repo, f"origin/{b}"):
            continue
        ap = cfg.anchor_path(alias, b)
        w = by_path.get(str(ap))
        if not w:
            rep.err(f"{alias}: 锚点 {b} 缺失（aisk task init --apply）")
        elif w.get("branch") != b:
            rep.err(f"{alias}: 锚点 {ap} 不在 {b} 上")
    if str(cfg.anchor_path(alias, "gate")) not in by_path:
        rep.err(f"{alias}: 门禁区缺失（aisk task init --apply）")
    found = audit_branch(cfg, reg, alias, repo, cfg.integration, strict=False)
    for b in list(rc.anchors) + list(rc.audited):
        found += audit_branch(cfg, reg, alias, repo, b, strict=True)
    probs = [m for level, m in found if level == "err"]
    for level, message in found:
        if level == "warn":
            rep.warn(message)
    for b in rc.audited:
        # 只登记不检出：没有工作区占着它，位置被改写只能靠审计发现，所以必须有登记基线
        if git.sha(repo, b) and not reg.load_refs(alias).get(b):
            rep.warn(f"{alias}: 受审计分支 {b} 还没有登记基线，{names.CLI} doctor --ack-refs 记一次")
    if found and args.ack_refs:  # warn 级（与远端同步）同样要能重新登记，否则提示了却按不下去
        record_refs(reg, alias, repo, [cfg.integration] + list(rc.anchors) + list(rc.audited))
        rep.ok(f"{alias}: 已人工确认并重新登记受保护分支位置")
    else:
        for p in probs:
            rep.err(p)
    for s in git.out(["stash", "list"], cwd=repo, optional_locks=False).splitlines():
        m = re.search(r"(?:WIP on|On) ([^:]+):", s)
        if m and m.group(1).startswith(cfg.branch_prefix):
            rep.warn(f"{alias}: 任务分支 {m.group(1)} 留下了 stash（所有 worktree 共用）：{s}")


def _maybe_unmounted(path):
    """最近一个存在的上级是文件系统根或挂载点时，「目录不存在」可能只是外接盘没挂上，不能当成已删除。"""
    anc = Path(path)
    while not anc.exists():
        if anc.parent == anc:
            return True
        anc = anc.parent
    return anc == Path(anc.anchor) or anc.is_mount() or anc.name in ("Volumes", "mnt", "media")


def check_dead_registration(cfg, rep, alias, repo, w, fix):
    """worktree 目录已不存在的登记。加过锁的 git 不会标成可清理，会一直占着分支：锚点建不起来，主工作区也检出不了。"""
    p = Path(w["path"])
    branch = w.get("branch")
    holds = f"，仍占着分支 {branch}" if branch else ""
    report = rep.err if branch else rep.warn
    if str(p.resolve()).startswith(str(cfg.data_root.resolve())):
        report(f"{alias}: 任务引擎的目录 {p} 丢失{holds}；先查登记簿与 hub，用 {names.CLI} restore 或 init --apply 重建，不要清理登记")
        return
    if _maybe_unmounted(p):
        report(f"{alias}: worktree 登记 {p} 的目录不存在{holds}；所在的盘可能没挂载，确认后再处理")
        return
    head = w.get("head") or ""
    kept = git.run(["for-each-ref", "--contains", head, "--format=%(refname)"], cwd=repo, check=False).stdout.split() \
        if head else []
    salvage = "" if kept or not head else f"，提交 {head[:10]} 只被这条登记引用，会先存到 {names.REF_NS}/salvage/"
    if not fix:
        report(f"{alias}: 失效登记 {p}：目录已不存在{holds}（{names.CLI} doctor --fix 清理{salvage}）")
        return
    admin = git.registration_dir(repo, p)
    if admin is None:
        report(f"{alias}: 失效登记 {p} 找不到对应的管理目录，未清理")
        return
    if head and not kept:
        ref = f"{names.REF_NS}/salvage/{alias}-{admin.name}"
        git.run(["update-ref", ref, head], cwd=repo)
        rep.ok(f"{alias}: 提交 {head[:10]} 已存为 {ref}")
    remove_tree(admin)
    rep.ok(f"{alias}: 已清理失效登记 {p}" + (f"，分支 {branch} 已释放" if branch else ""))


def stale_git_locks(repo, minutes=10):
    """进程被杀会留下 git 锁文件，之后每条 git 命令都报 Unable to create ... index.lock，看着像仓库坏了。
    只报超过 minutes 分钟没动过的，避免把正在跑的 git 当成残留。"""
    try:
        common = git.common_dir(repo)
    except Exception:  # noqa: BLE001
        return []
    out, deadline = [], time.time() - minutes * 60
    candidates = [common / "index.lock", common / "shallow.lock", common / "HEAD.lock",
                  common / "config.lock", common / "packed-refs.lock"]
    candidates += sorted(common.glob("worktrees/*/index.lock"))
    for lock in candidates:
        try:
            mtime = lock.stat().st_mtime
        except OSError:
            continue
        if mtime < deadline:
            out.append((lock, f"{int((time.time() - mtime) / 60)} 分钟未变化"))
    return out


def check_task(cfg, reg, rep, t):
    if t.get("os") != cfg.os:
        return
    started = parse_iso(t.get("creating"))
    if started:
        if time.time() - started.timestamp() > tasks.CREATING_STALE_SECONDS:
            rep.err(f"{t['id']}: 创建中断于 {t['creating']}；确认没有进程在创建后删除 state/tasks/{t['id']}.json 与残留目录")
        return
    if t.get("archiving"):
        detail = f"，上次错误：{t['archive_error']}" if t.get("archive_error") else ""
        rep.err(f"{t['id']}: 归档在 {t['archiving']} 中断{detail}；重跑 {names.CLI} archive {t['id']} 完成")
        return
    if not Path(t["dir"]).exists():
        rep.err(f"{t['id']}: 登记为 {t['state']} 但目录不存在")
        return
    try:
        tasks.require_local(cfg, t)
    except WtError as e:
        rep.err(f"{t['id']}: {e}")
        return
    for alias, r in t["repos"].items():
        path = r["path"]
        if git.config_get(path, names.GIT_MARK_KEY) != "true":
            rep.err(f"{t['id']}/{alias}: 任务 git 配置（includeIf）没有生效，aisk task bind --apply")
        if names.PUSH_DISABLED_URL not in (git.config_get(path, "remote.origin.pushurl") or ""):
            rep.err(f"{t['id']}/{alias}: origin 推送护栏未生效")
        if git.config_get(path, "protocol.allow") != "never":
            rep.err(f"{t['id']}/{alias}: 任务 git 配置缺少传输封锁（protocol.allow=never），{names.CLI} bind --apply")
        rc = cfg.repo(alias)
        if rc.hooks_required:
            want = ((cfg.raw.get("repos") or {}).get(alias) or {}).get("hooks_path")
            have = git.config_get(path, "core.hooksPath")
            if not have or (want and have != str(want)):
                rep.err(f"{t['id']}/{alias}: 仓库钩子路径缺失或不符（{have}）")
    owner = t.get("owner")
    if owner and t["state"] not in LIVE_STATES:
        rep.warn(f"{t['id']}: 状态 {t['state']} 仍登记执行者 {owner.get('tool')}")


def check_dirs(cfg, reg, rep):
    known = {Path(t["dir"]).resolve() for t in reg.all(include_archived=True, include_hub=False)}
    if cfg.tasks_dir.exists():
        for d in sorted(cfg.tasks_dir.iterdir()):
            if not d.is_dir() or d.resolve() in known:
                continue
            rep.err(f"{d}: 未登记目录（不是任务引擎创建的），用 aisk task adopt 收编或由创建者清理")
            for sub in sorted(d.iterdir()):
                if (sub / ".git").is_file():
                    good, info = git.backlink_ok(sub)
                    if not good:
                        rep.err(f"{sub}: {info}（立即把 .git 改名隔离，勿在此执行 git）")
    lanes = tasks.legacy_lanes(cfg)
    if lanes and lanes.exists():
        slots_dir = cfg.legacy_root / "state" / "slots"
        imported = {t.get("legacy_id") for t in reg.all(include_archived=True, include_hub=False)}
        pending = []
        if slots_dir.exists():
            for f in sorted(slots_dir.glob("*.json")):
                try:
                    old = json.loads(f.read_text(encoding="utf-8"))
                except ValueError:
                    rep.warn(f"旧登记 {f.name} 无法解析")
                    continue
                if old.get("state") != "archived" and old.get("id") not in imported:
                    pending.append(old.get("id"))
        if pending:
            rep.warn(f"旧引擎仍管理 {len(pending)} 个槽位（{'、'.join(pending[:8])}）；迁移用 aisk task import-legacy 预览")


def check_legacy_config(cfg, rep):
    from . import legacy
    old = legacy.legacy_config(cfg)
    if old is None:
        return
    diffs = legacy.compare_config(cfg, old)
    for d in diffs:
        rep.warn(f"档案与旧 policy/config.json 不一致：{d}")
    if not diffs:
        rep.ok("档案 worktrees 一节与旧 policy/config.json 逐键一致")


def check_bind(cfg, rep):
    for desc, path, cur, want in bind.plan(cfg):
        if cur != want:
            rep.warn(f"机器配置漂移：{desc}（{path}），aisk task bind 查看、--apply 写入")
    if cfg.os == "mac":
        gpath = bind.global_gitconfig_path()
        text = gpath.read_text(encoding="utf-8") if gpath.exists() else ""
        if bind.GIT_BEGIN in text and not text.rstrip().endswith(bind.GIT_END):
            rep.err("全局 git 配置里任务工作区的 includeIf 块不在文件末尾（清空凭据助手会失效）")


def check_data_root(cfg, rep):
    root = cfg.data_root
    if (root / ".git").exists():
        rep.err(f"数据根 {root} 是 git 仓库：误执行 git clean -fdX 会删掉登记簿、hub 与任务文件，备份后移除 .git")
    text = str(root).replace("\\", "/")
    if cfg.os == "windows" and (text.startswith("//") or any(
            str(profile_expand(p)).replace("\\", "/").rstrip("/") and text.lower().startswith(
                str(profile_expand(p)).replace("\\", "/").rstrip("/").lower())
            for p in (cfg.raw.get("guard_protected") or []))):
        rep.err(f"Windows 数据根 {root} 在共享盘上：按决策 12 应放在虚拟机本地盘（构建与文件监听会很慢且会写到 Mac）")
    for anc in root.parents:
        if (anc / ".git").exists():
            rep.warn(f"数据根在 git 仓库 {anc} 之内：在那里执行 git clean -fdx 会连任务数据一起删（写进 .gitignore 也挡不住 -x）")
            break
    if (root / "lanes").is_dir() and not cfg.legacy_root:
        rep.warn(f"数据根下有旧版 lanes/ 布局但档案没有声明 worktrees.legacy.root：旧槽位不会被识别，迁移步骤见 WORKTREE.md")
    if not root.exists():
        rep.err(f"数据根 {root} 不存在：先 {names.CLI} init 预览再 --apply")


def profile_expand(path):
    from .. import profile as profile_mod
    return profile_mod._expand(path)


def check_retention(cfg: WtConfig, reg: Registry, rep):
    """任务目录只在归档时回收，而归档排在人工验收之后——没人盯着就会一直堆。"""
    archivable, artifacts = tasks.gc_candidates(cfg, reg)
    hours = cfg.retention.get("promoted_hours", 72)
    if archivable:
        rep.warn(f"{len(archivable)} 个任务已上主干/已验收且超过 {hours} 小时没动："
                 f"{'、'.join(t['id'] for t in archivable[:6])}；{names.CLI} gc 预览回收")
    budget = float(cfg.retention.get("disk_budget_gb", 10))
    used = tasks.dir_size(cfg.tasks_dir) if cfg.tasks_dir.is_dir() else 0
    if used > budget * (1 << 30):
        detail = f"，其中构建产物 {sum(tasks.dir_size(d) for _t, ds in artifacts for d in ds) // (1 << 30):.0f}GB" if artifacts else ""
        rep.warn(f"任务目录占用 {used / (1 << 30):.1f}GB，超过档案预算 {budget}GB{detail}；"
                 f"{names.CLI} gc --apply --build-artifacts 可回收")


def cmd_doctor(cfg, reg, args):
    rep = Report()
    safely(rep, "数据根", check_data_root, cfg, rep)
    if cfg.os == "windows":
        safely(rep, "Windows 远端", check_windows_remotes, cfg, rep)
    for alias in cfg.repo_order:
        safely(rep, alias, check_repo, cfg, reg, rep, alias, args)
    for t in reg.all(include_hub=False):
        safely(rep, t.get("id", "?"), check_task, cfg, reg, rep, t)
    safely(rep, "目录", check_dirs, cfg, reg, rep)
    safely(rep, "旧配置比对", check_legacy_config, cfg, rep)
    safely(rep, "机器配置", check_bind, cfg, rep)
    live = [t for t in reg.all(include_hub=False) if t["state"] in WORKING_STATES and t.get("os") == cfg.os]
    if len(live) > cfg.quota_active:
        rep.warn(f"进行中的任务 {len(live)} 个，超过配额 {cfg.quota_active}")
    safely(rep, "磁盘回收", check_retention, cfg, reg, rep)
    tasks.refresh_board(cfg, reg)
    print()
    if rep.errs:
        say("err", f"巡检发现 {len(rep.errs)} 个错误、{len(rep.warns)} 个警告（{now_iso()[:16]}）")
        return 1
    say("ok", f"巡检通过（{len(rep.warns)} 个警告）")
    return 0
