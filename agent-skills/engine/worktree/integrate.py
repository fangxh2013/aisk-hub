# -*- coding: utf-8 -*-
"""集成面：sync / land（合入集成分支）/ promote（集成分支 → 推送分支 → 主干）/ verify / revert，
以及受保护分支来源审计（堵 git update-ref 绕过锚点的洞）。

锁名与旧引擎一致（state/locks/land-<仓>.lock），迁移期新旧引擎落地互斥。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import actor, gates, gitops as git, model, names, registry, tasks
from ..action_context import ActionContext
from .config import WtConfig, WtError
from .registry import LIVE_STATES, Registry, file_lock, now_iso, say, stamp

NORMAL_PREFIX = ("commit", "merge ", "pull", "cherry-pick", "revert", "reset: moving to", "rebase", "am", "xw ", "aisk task ")
SUSPICIOUS_PREFIX = ("branch: Reset to", "branch: Created from", "fetch", "push", "update by push")


class Reject(WtError):
    pass


MANUAL_ONLY_BRANCHES = {"main", "master"}
DEFAULT_PROTECTED_PUSH_BRANCHES = {"dev", "main", "master"}


def merge_policy(cfg: WtConfig):
    policy = cfg.raw.get("merge_policy") if isinstance(cfg.raw, dict) else None
    return policy if isinstance(policy, dict) else {}


def manual_merge_required(cfg: WtConfig):
    return bool(merge_policy(cfg).get("confirm_land", False))


def push_requires_confirmation(cfg: WtConfig, alias, branch):
    """判断 promote 写入的远端分支是否需要人工确认。

    个人集成面（新华为 ``fxh``）及其个人推送分支（通常为 ``fxh-dev``）是
    日常开发闭环，默认允许提交/推送；``dev``、``main``、``master`` 以及仓库
    trunk 始终需要确认。历史 profile 可能把 ``fxh`` 误列进 protected，不能
    让那份旧配置重新封锁个人分支；如确实需要确认个人推送，可显式设置
    ``merge_policy.confirm_personal_push: true``。
    """
    policy = merge_policy(cfg)
    if bool(policy.get("confirm_personal_push", False)):
        return True
    rc = cfg.repo(alias)
    branch = str(branch or "")
    personal = {str(cfg.integration), str(rc.push_branch)} - {"", str(rc.trunk)}
    if branch in personal:
        return False
    protected = set(DEFAULT_PROTECTED_PUSH_BRANCHES) | {str(b) for b in cfg.protected}
    return branch == str(rc.trunk) or branch in protected


def main_operations_forbidden(cfg: WtConfig):
    return bool(merge_policy(cfg).get("forbid_main_operations", False))


def _workbuddy_action(args):
    tool = getattr(args, "tool", None) or actor.detect_tool()
    return tool in ("workbuddy", "workbuddy-ai")


def _repository_label_for_workbuddy(args, repo):
    """只给 WorkBuddy 的标准弹窗补仓库名；其它端保持既有标题协议。"""
    if not _workbuddy_action(args):
        return ""
    remote = git.out(["remote", "get-url", "origin"], cwd=repo, check=False).strip()
    value = remote.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    value = value.removesuffix(".git")
    return value or Path(str(repo)).name


def _push_expect(args):
    return "确认推送" if _workbuddy_action(args) else "推送"


def action_context(args, action, task_id="", summary="", repository=""):
    try:
        return ActionContext.from_env(
            action,
            task_id=task_id,
            session_id=getattr(args, "session", "") or "",
            tool=getattr(args, "tool", None),
            summary=summary,
            repository=repository,
        )
    except ValueError:
        # 没有工具归属时不弹窗、不继续；registry.confirm_human 会安全拒绝。
        # 保留 None 是为了让旧的纯函数调用和测试替身仍可观察到确认调用。
        return None


def reject_main_target(cfg: WtConfig, *branches):
    # main is an unconditional, non-configurable no-write target. Keep master on
    # the existing project policy path; the two branches intentionally differ.
    def branch_name(value):
        name = str(value or "").strip().lower().split(":")[-1]
        prefixes = ("refs/heads/", "refs/remotes/", "origin/", "mac/")
        while True:
            prefix = next((p for p in prefixes if name.startswith(p)), None)
            if not prefix:
                break
            name = name[len(prefix):]
        return name

    blocked_main = [str(b) for b in branches if branch_name(b) == "main"]
    if blocked_main:
        raise Reject(f"禁止通过 aisk task 修改 main：{'、'.join(blocked_main)}；该限制不可配置或确认解除。")
    if main_operations_forbidden(cfg):
        blocked_master = [str(b) for b in branches if branch_name(b) == "master"]
        if blocked_master:
            raise Reject(f"禁止通过 aisk task 自动操作 {'、'.join(blocked_master)}；master 仍受项目策略约束。")


def landing_prompt(tid, title, target, plans, blocked=None):
    lines = [f"任务 {tid} {title} 将落地到本地 {target}（只快进，不推送）："]
    for alias, plan in plans.items():
        if plan["status"] != "candidate":
            lines.append(f"{alias}: 已包含在 {target}，无需改动")
            continue
        gate = plan.get("gate", {}).get("summary", "门禁待执行")
        lines.append(f"{alias}: {plan['head'][:9]} → {plan['cand'][:9]}，改动 {len(plan['changed'])} 个文件，门禁：{gate}")
        if plan.get("sensitive"):
            lines.append(f"  敏感路径：{'、'.join(plan['sensitive'][:12])}")
        if plan.get("changed"):
            lines.append(f"  文件：{'、'.join(plan['changed'][:20])}")
    for alias, why in (blocked or {}).items():
        lines.append(f"{alias}: 本次不落地——{why}")
    return "\n".join(lines)


def sync_prompt(alias, integration, trunk, head, cand, changed, summary):
    return (f"仓库 {alias} 需要把 origin/{trunk} 的最新提交同步到 {integration}（只快进，不推送）：\n"
            f"{head[:9]} → {cand[:9]}，改动 {len(changed)} 个文件，门禁：{summary}\n"
            f"文件：{'、'.join(changed[:20])}")


def integration_repo(cfg: WtConfig, alias):
    """取集成工作区；Windows 只有档案明确授权时才允许写入。"""
    repo = cfg.integration_repo(alias)
    if not repo.exists():
        raise Reject(f"{alias} 集成仓库不存在：{repo}")
    return repo


def task_tip_ref(cfg: WtConfig, task, alias):
    """Windows 显式集成从 hub 读取任务分支，不把本地裸对象库当成集成工作区。"""
    use_hub = (cfg.os == "windows" and cfg.windows_integration) or task.get("os") != cfg.os
    branch = task["repos"][alias]["branch"]
    return f"refs/remotes/hub/{branch}" if use_hub else branch


def fetch_task_refs(cfg: WtConfig, alias, repo):
    """把任务分支读进集成仓库，不写入共享仓库的远端配置。"""
    if cfg.os == "windows":
        source = cfg.hub / f"{alias}.git"
        if not source.exists():
            raise Reject(f"Windows 集成找不到 hub 仓库：{source}")
        refspec = f"+refs/heads/{cfg.branch_prefix}*:refs/remotes/hub/{cfg.branch_prefix}*"
        result = git.run(["fetch", str(source), "--prune", refspec], cwd=repo, check=False)
    else:
        result = git.run(["fetch", "hub", "--prune"], cwd=repo, check=False)
    if result.returncode != 0:
        raise Reject(f"{alias} 拉取任务分支失败：{result.stderr.strip()[-300:]}")


def ff_anchor_re(cfg: WtConfig):
    return re.compile(rf"^merge (origin/\S+|{re.escape(cfg.integration)}|[0-9a-f]{{7,40}}): Fast-forward$")


def ff_compare_and_swap(repo, branch, expected_old, candidate, *, journal_path=None,
                        preserve_journal_on_failure=False):
    """Safely fast-forward a checked-out branch with a real expected-old CAS.

    ``update-ref --stdin`` prepares the branch ref transaction first, which
    verifies and locks the exact old object id. While that lock is held,
    ``read-tree -m -u`` performs Git's normal index/worktree safety checks and
    updates only paths changed by the fast-forward. The ref transaction is
    committed only after that succeeds. This avoids the check-then-merge race
    without using ``reset --hard`` or discarding unrelated staged changes.
    """
    branch = str(branch or "")
    ref = f"refs/heads/{branch}"
    if not branch or not git.ok(["check-ref-format", ref], cwd=repo):
        raise Reject(f"无效的集成分支引用：{branch!r}")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(expected_old or "")):
        raise Reject("CAS 缺少有效的预期旧提交")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(candidate or "")):
        raise Reject("CAS 缺少有效的候选提交")
    if git.current_branch(repo) != branch:
        raise Reject(f"主工作区不在 {branch}")
    if not git.is_ancestor(repo, expected_old, candidate):
        raise Reject(f"候选提交不是 {branch} 的快进提交")

    journal = Path(journal_path) if journal_path else None
    journal_data = {"version": 1, "branch": branch, "expected_old": expected_old, "candidate": candidate}
    if journal and journal.exists():
        try:
            existing = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Reject(f"{branch} 落地恢复记录损坏；保留现场并停止：{journal}") from exc
        if existing != journal_data:
            raise Reject(f"{branch} 存在另一笔未恢复的落地事务；先运行恢复检查：{journal}")
    elif journal:
        registry.atomic_json(journal, journal_data)

    git_dir_lock = Path(git.out(["rev-parse", "--git-path", f"{ref}.lock"], cwd=repo))
    if not git_dir_lock.is_absolute():
        git_dir_lock = Path(repo) / git_dir_lock
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    env["LC_ALL"] = "C"
    env["LANGUAGE"] = "C"
    command = [git.git_exe(), "-c", "core.quotepath=false", "update-ref", "--stdin",
               "-m", f"merge {branch}: Fast-forward"]
    process = subprocess.Popen(command, cwd=str(repo), env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace", bufsize=1)

    def collect(timeout=None):
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        code = process.wait(timeout=timeout)
        stdout, stderr = process.stdout.read(), process.stderr.read()
        process.stdout.close()
        process.stderr.close()
        return stdout, stderr, code

    prepared = False
    tree_updated = False
    try:
        process.stdin.write(f"start\nupdate {ref} {candidate} {expected_old}\nprepare\n")
        process.stdin.flush()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr, _ = collect()
                detail = (stderr or stdout).strip()[-300:]
                raise Reject(f"{branch} CAS 失败（预期仍为 {expected_old[:9]}）：{detail}")
            try:
                if git_dir_lock.read_text(encoding="ascii").strip() == candidate:
                    prepared = True
                    break
            except (FileNotFoundError, OSError, UnicodeError):
                pass
            time.sleep(0.01)
        if not prepared:
            process.stdin.write("abort\n")
            process.stdin.flush()
            process.stdin.close()
            stdout, stderr, _ = collect()
            if journal and not preserve_journal_on_failure:
                journal.unlink(missing_ok=True)
            raise Reject(f"{branch} CAS 准备超时；未移动分支引用。{(stderr or stdout).strip()[-200:]}")

        tree_update = git.run(["read-tree", "-m", "-u", expected_old, candidate], cwd=repo, check=False)
        if tree_update.returncode != 0:
            process.stdin.write("abort\n")
            process.stdin.flush()
            process.stdin.close()
            stdout, stderr, _ = collect()
            detail = (tree_update.stderr or stderr or stdout).strip()[-300:]
            if journal and not preserve_journal_on_failure:
                journal.unlink(missing_ok=True)
            raise Reject(f"{branch} 工作区无法安全快进，引用保持不变：{detail}")
        tree_updated = True

        process.stdin.write("commit\n")
        process.stdin.flush()
        process.stdin.close()
        stdout, stderr, returncode = collect()
        if returncode != 0:
            actual = git.sha(repo, ref)
            raise Reject(f"{branch} CAS 提交失败（当前 {str(actual)[:9]}，预期 {candidate[:9]}）："
                         f"{(stderr or stdout).strip()[-300:]}")
    except Exception:
        if process.poll() is None:
            try:
                process.stdin.write("abort\n")
                process.stdin.flush()
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                collect(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                collect()
        if journal and not tree_updated and not preserve_journal_on_failure:
            journal.unlink(missing_ok=True)
        raise
    actual = git.sha(repo, ref)
    if actual != candidate:
        raise Reject(f"{branch} 快进完成后引用不是候选提交：{str(actual)[:9]} != {candidate[:9]}")
    if journal:
        journal.unlink(missing_ok=True)
    return actual


def land_cas_journal_path(cfg, alias):
    return cfg.state_dir / "land-cas" / f"{alias}.json"


def recover_land_cas(repo, branch, journal_path):
    """Resume or retire a prior fast-forward interrupted around index/ref commit."""
    journal = Path(journal_path)
    if not journal.exists():
        return False
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Reject(f"{branch} 落地恢复记录不可读；不会改动工作区：{journal}") from exc
    if (not isinstance(data, dict) or data.get("version") != 1 or data.get("branch") != branch
            or not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(data.get("expected_old") or ""))
            or not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(data.get("candidate") or ""))):
        raise Reject(f"{branch} 落地恢复记录与当前操作不匹配；不会改动工作区：{journal}")
    if git.current_branch(repo) != branch:
        raise Reject(f"{branch} 落地事务中断后当前工作区已切换分支；保留恢复记录：{journal}")
    current = git.sha(repo, f"refs/heads/{branch}")
    if current == data["candidate"]:
        # The ref transaction committed only after read-tree completed.
        journal.unlink(missing_ok=True)
        return True
    if current != data["expected_old"]:
        raise Reject(f"{branch} 落地事务中断后引用已再次变化；保留恢复记录：{journal}")
    ff_compare_and_swap(repo, branch, data["expected_old"], data["candidate"], journal_path=journal,
                        preserve_journal_on_failure=True)
    return True


# ------------------------------------------------------------------ 来源审计
def synced_with_remote(repo, branch, base, cur):
    """本地分支被快进到 origin/<branch>，且旧位置是新位置的祖先：这是与远端同步，不是本地改写。
    只登记不检出的受保护分支（如 main）每次团队合完 PR 都会发生一次，报成错误就是狼来了。"""
    remote = git.sha(repo, f"origin/{branch}")
    return bool(remote) and remote == cur and git.is_ancestor(repo, base, cur)


def report_audit(found):
    """打印 warn 级发现，返回仍要拦下的 err 级说明。"""
    blocking = []
    for level, message in found:
        if level == "warn":
            say("warn", message)
        else:
            blocking.append(message)
    return blocking


def audit_branch(cfg: WtConfig, reg: Registry, alias, repo, branch, strict):
    """返回 [(级别, 说明)]，级别为 err / warn。"""
    base = reg.load_refs(alias).get(branch)
    cur = git.sha(repo, branch)
    if not base or not cur or base == cur:
        return []
    if synced_with_remote(repo, branch, base, cur):
        return [("warn", f"{alias}/{branch}：本地已快进到 origin/{branch}（{cur[:9]}），是与远端同步不是本地改写；"
                         f"确认后 {names.CLI} doctor --ack-refs 重新登记")]
    newer, found = [], False
    for h, subj in git.reflog_entries(repo, branch):
        if h == base:
            found = True
            break
        newer.append((h, subj))
    if not found:
        return [("err", f"{alias}/{branch}：reflog 里找不到上次登记的 {base[:9]}（被重写或 reflog 过期），"
                         f"人工核对后执行 {names.CLI} doctor --ack-refs")]
    problems = []
    ff = ff_anchor_re(cfg)
    for h, subj in newer:
        if strict:
            if not ff.match(subj):
                problems.append(("err", f"{alias}/{branch} 出现非任务引擎的移动记录：{h[:9]} "
                                        f"「{subj or '（空说明，疑似 update-ref）'}」"))
        elif not subj:
            problems.append(("err", f"{alias}/{branch}：{h[:9]} 的 reflog 说明为空，疑似 git update-ref 直接改动"))
        elif subj.startswith(SUSPICIOUS_PREFIX) or not subj.startswith(NORMAL_PREFIX):
            problems.append(("err", f"{alias}/{branch}：{h[:9]} 被非常规方式移动「{subj}」"))
    return problems


def record_refs(reg: Registry, alias, repo, branches):
    data = reg.load_refs(alias)
    for b in branches:
        s = git.sha(repo, b)
        if s:
            data[b] = s
    data["_at"] = now_iso()
    reg.save_refs(alias, data)


# ------------------------------------------------------------------ sync
def cmd_sync(cfg, reg, args):
    failed = False
    with file_lock(cfg.locks_dir / "sync.lock", wait_msg="另一个 sync 正在进行，等待"):
        for alias in cfg.repo_order:
            repo = cfg.repo(alias).path
            if not repo.exists():
                continue
            remote = "mac" if cfg.os == "windows" else "origin"
            r = git.run(["fetch", remote, "--prune"], cwd=repo, check=False)
            say("ok" if r.returncode == 0 else "err",
                f"{alias}: fetch {remote} {'完成' if r.returncode == 0 else r.stderr.strip()[-300:]}")
            failed = failed or r.returncode != 0
            if cfg.os == "mac" and git.config_get(repo, "remote.hub.url"):
                h = git.run(["fetch", "hub", "--prune"], cwd=repo, check=False)
                if h.returncode != 0:
                    failed = True
                    say("warn", f"{alias}: fetch hub 失败 {h.stderr.strip()[-200:]}")
    return 1 if failed else 0


# ------------------------------------------------------------------ land
def gate_prepare(cfg: WtConfig, alias, cand):
    gate = cfg.anchor_path(alias, "gate")
    if not gate.exists():
        raise Reject(f"{alias} 缺少门禁区 {gate}（先 aisk task init --apply）")
    if git.dirty(gate):
        raise Reject(f"{alias} 门禁区有残留改动，请检查后恢复：{gate}")
    if cfg.os == "windows" and cfg.windows_integration:
        fetch_task_refs(cfg, alias, gate)
    git.run(["checkout", "--detach", cand], cwd=gate)
    git.run(["clean", "-fdq", "-e", "node_modules"], cwd=gate)
    return gate


def find_landing_merge(repo, tip, integration):
    for line in git.out(["rev-list", "--merges", "--ancestry-path", "--reverse", "--parents",
                         f"{tip}..{integration}"], cwd=repo).splitlines():
        parts = line.split()
        if tip in parts[1:]:
            return parts[0]
    return None


def landing_message(cfg, task):
    for msg in (f"merge: 落地{task['title'][:60]}({task['id']})", f"merge: 落地任务({task['id']})"):
        if not model.check_message(cfg, msg):
            return msg
    raise Reject(model.check_message(cfg, f"merge: 落地任务({task['id']})"))


def plan_landing(cfg: WtConfig, reg: Registry, task, alias, from_hub):
    """单个仓库的落地计划。不满足条件就抛 Reject——只挡这一个仓库，任务里的其他仓库照常落地。"""
    r = task["repos"][alias]
    repo = integration_repo(cfg, alias)
    if not r.get("ready_sha"):
        raise Reject(f"还没有 ready：修好后 {names.CLI} ready {task['id']} --repos {alias}")
    tip = git.sha(repo, task_tip_ref(cfg, task, alias) if from_hub else r["branch"])
    if not tip or tip != r["ready_sha"]:
        raise Reject(f"分支头 {str(tip)[:9]} 与 ready 登记 {r['ready_sha'][:9]} 不一致，请重新 {names.CLI} ready")
    head = git.sha(repo, cfg.integration)
    if head and git.is_ancestor(repo, tip, head):
        # 没有要落的提交：主工作区干不干净都与它无关，不该被挡
        return {"status": "already", "head": head, "tip": tip}
    if head and not git.merge_base(repo, head, tip):
        remote_integration = git.sha(repo, f"origin/{cfg.integration}")
        hint = ""
        if remote_integration and git.merge_base(repo, remote_integration, tip):
            hint = (f"；origin/{cfg.integration}@{remote_integration[:9]} 与任务有共同祖先，"
                    f"请先把本地 {cfg.integration} 安全同步到远端主线")
        raise Reject(
            f"本地 {cfg.integration}@{head[:9]} 与任务提交 {tip[:9]} 没有共同祖先，"
            f"拒绝无关历史合并{hint}；请先核对主线/远端是否切换或重建"
        )
    if git.current_branch(repo) != cfg.integration:
        raise Reject(f"主工作区不在 {cfg.integration}")
    dirty = git.dirty_paths(repo)
    if dirty:
        # 只拦「脏文件与本次落地内容相交」——主工作区几乎总有点无关的草稿/本地配置，
        # 逐个字节都要求干净只会把交付链堵死在一个和落地无关的文件上。真正的保护在后面：
        # merge --ff-only 本身拒绝覆盖本地修改，:394 处的门禁通过后复检也按同样口径重查。
        clash = sorted(set(dirty) & set(git.diff_names(repo, head, tip)))
        if clash:
            raise Reject(f"主工作区这些文件与本次落地内容冲突，请先提交或还原：{'、'.join(clash[:15])}")
        say("warn", f"{alias}: 主工作区有 {len(dirty)} 个无关未提交文件，本次落地不碰它们")
    probs = report_audit(audit_branch(cfg, reg, alias, repo, cfg.integration, strict=False))
    if probs:
        raise Reject("；".join(probs))
    clean, tree, conflicts = git.merge_tree(repo, head, tip)
    if not clean:
        raise Reject(f"与 {cfg.integration} 冲突 {len(conflicts)} 个文件：{'、'.join(conflicts[:15])}。"
                     f"请在任务里 git rebase {cfg.integration} 解决后重新 check/ready")
    cand = git.commit_tree(repo, tree, [head, tip], landing_message(cfg, task))
    changed = git.diff_names(repo, head, cand)
    return {"status": "candidate", "head": head, "tip": tip, "cand": cand, "changed": changed,
            "sensitive": [p for p in changed if gates.path_match(p, cfg.sensitive_paths)]}


def repo_landed(cfg: WtConfig, task, alias):
    """这个仓库的任务提交是否已全部在集成分支里。按提交关系判断，不信登记：没有新提交的仓库天然算已落地。"""
    repo = integration_repo(cfg, alias)
    tip = git.sha(repo, task_tip_ref(cfg, task, alias))
    head = git.sha(repo, cfg.integration)
    return bool(tip and head and git.is_ancestor(repo, tip, head))


def cmd_land(cfg, reg, args):
    """落地到本地集成分支。**各仓库各自判定**：未 ready、主工作区不干净、冲突、门禁失败都只挡住那一个仓库，
    其余仓库照常落地；--repos 只落指定仓库。全部仓库都落地后任务才算 landed。

    2026-09-18 T021（后端 + 前端 + 文档）因为文档主工作区有别人的未提交改动，三个仓库一起被拒——
    前后端早已就绪，却要等一个与它们无关的目录。仓库之间不能有这种绑定。"""
    if cfg.os == "windows" and not cfg.windows_integration:
        raise WtError("land 默认只在 mac 集成面执行；Windows 请显式配置 worktrees.windows_integration")
    reject_main_target(cfg, cfg.integration)
    dry = args.dry_run
    task = reg.find_by_ref(args.task)
    tid = task["id"]
    if task["state"] not in ("ready", "queued", "rejected"):
        raise WtError(f"任务状态 {task['state']}，需要先 aisk task ready")
    aliases = tasks.select_repos(cfg, task, getattr(args, "repos", None))
    if not any(task["repos"][a].get("ready_sha") for a in aliases):
        raise WtError("所选仓库都还没有 ready，修复后先 aisk task ready")
    if task.get("base_task"):
        parent = reg.load(task["base_task"])
        if parent["state"] not in ("landed", "promoted", "verified", "archived"):
            raise WtError(f"父任务 {parent['id']} 状态 {parent['state']}，必须先落地父任务")
    logdir = cfg.logs_dir / tid

    with contextlib.ExitStack() as stack:
        for a in aliases:
            stack.enter_context(file_lock(tasks.land_lock_path(cfg, a), wait_msg=f"{a} 有别的落地/上主干正在进行，排队"))
        task = reg.load(tid)  # 加锁后重读，防止两个 land 拿着过期的 ready 记录重复落地
        if task["state"] not in ("ready", "queued", "rejected"):
            raise WtError(f"任务状态已变为 {task['state']}")
        from_hub = (cfg.os == "windows" and cfg.windows_integration) or task.get("os") != cfg.os
        if not from_hub:
            tasks.require_local(cfg, task, ("ready", "queued", "rejected"))
        if task["state"] != "queued" and not dry:
            reg.set_state(task, "queued")
        try:
            if not dry:
                for a in aliases:
                    repo = integration_repo(cfg, a)
                    recover_land_cas(repo, cfg.integration, land_cas_journal_path(cfg, a))
            if from_hub:
                for a in aliases:
                    fetch_task_refs(cfg, a, integration_repo(cfg, a))
            plans, blocked = {}, {}
            for alias in aliases:
                try:
                    plans[alias] = plan_landing(cfg, reg, task, alias, from_hub)
                except Reject as e:
                    blocked[alias] = str(e)

            sens = {a: p["sensitive"] for a, p in plans.items() if p.get("sensitive")}
            if sens and not manual_merge_required(cfg):
                prompt = (f"任务 {tid} {task['title']} 改到了敏感路径（落地后会在沙箱外被执行或影响团队构建）：\n"
                          + "\n".join(f"{a}: {'、'.join(v[:12])}" for a, v in sens.items()))
                print(prompt)
                if dry:
                    say("info", "（dry-run：此处需要操作者确认）")
                elif not registry.confirm_human(prompt, "落地", context=action_context(args, "git合并", tid, prompt)):
                    for a in sens:
                        plans.pop(a)
                        blocked[a] = "改到敏感路径，未获操作者确认"

            for alias, p in list(plans.items()):
                if p["status"] != "candidate":
                    say("info", f"{alias}: 已包含在 {cfg.integration} 中，跳过")
                    continue
                if dry:
                    say("ok", f"{alias}: 可无冲突合并，改动 {len(p['changed'])} 个文件（dry-run 不编译不快进）")
                    continue
                log = logdir / f"land-{alias}-{stamp()}.log"
                try:
                    gate = gate_prepare(cfg, alias, p["cand"])
                    ok, summary = gates.run_gate(cfg, alias, gate, p["changed"], log, clean=True)
                except WtError as e:
                    ok, summary = False, str(e)
                p["gate"] = {"ok": ok, "summary": summary, "log": str(log)}
                if not ok:
                    if log.exists():
                        print(gates.tail(log))
                    plans.pop(alias)
                    blocked[alias] = f"门禁失败——{summary}（日志 {log}）"
                    continue
                say("ok", f"{alias}: 门禁通过——{summary}")

            for alias, why in blocked.items():
                say("err", f"{alias}: {why}")
            if dry:
                if manual_merge_required(cfg) and any(p["status"] == "candidate" for p in plans.values()):
                    say("info", "实际 land 前必须出现 aisk task 弹窗，由操作者确认后才会快进集成分支")
                return 1 if blocked else 0
            if not plans:
                raise Reject("；".join(f"{a}: {why}" for a, why in blocked.items()))

            if manual_merge_required(cfg) and any(p["status"] == "candidate" for p in plans.values()):
                prompt = landing_prompt(tid, task["title"], cfg.integration, plans, blocked)
                print(prompt)
                if not registry.confirm_human(prompt, "落地", context=action_context(args, "git合并", tid, prompt)):
                    raise Reject("未确认落地，集成分支保持不变")

            for alias, p in plans.items():
                repo = integration_repo(cfg, alias)
                r = task["repos"][alias]
                if p["status"] == "candidate":
                    if git.current_branch(repo) != cfg.integration:
                        blocked[alias] = "门禁期间主工作区切换了分支"
                    elif git.sha(repo, cfg.integration) != p["head"]:
                        blocked[alias] = f"门禁期间 {cfg.integration} 前进了，请重新 land"
                    else:
                        # 和 plan_landing 同一口径：只拦新冒出来的、与本次落地内容相交的改动。
                        # 按原样查「是否有任何脏文件」会把 plan_landing 已经放行的无关草稿
                        # 在这里重新挡一次，等于门禁收窄白做——TOCTOU 防护要护住的是
                        # 「这段时间内容变了」，不是「工作区从始至终有点无关的东西」。
                        clash = sorted(set(git.dirty_paths(repo)) & set(p["changed"]))
                        if clash:
                            blocked[alias] = f"门禁期间主工作区这些文件出现冲突改动，请先处理：{'、'.join(clash[:15])}"
                    if alias in blocked:
                        say("err", f"{alias}: {blocked[alias]}")
                        continue
                    ff_compare_and_swap(repo, cfg.integration, p["head"], p["cand"],
                                        journal_path=land_cas_journal_path(cfg, alias))
                    r["landed_sha"] = p["cand"]
                    say("ok", f"{alias}: {cfg.integration} 快进到 {p['cand'][:9]}")
                elif p["tip"] != r.get("base_sha"):
                    # 没有新提交的仓库没有落地合并：别把后来别人的合并登记成本任务的（revert 会撤错）
                    r["landed_sha"] = r.get("landed_sha") or find_landing_merge(repo, p["tip"], cfg.integration)
                reg.save(task)  # 多仓部分落地也要可恢复
                record_refs(reg, alias, repo, [cfg.integration])

            done = [a for a in plans if a not in blocked]
            land = task.setdefault("land", {})
            land["at"] = now_iso()
            land.setdefault("plans", {}).update(
                {a: {k: v for k, v in plans[a].items() if k != "changed"} for a in done})
            pending = [a for a in tasks.select_repos(cfg, task, None) if not repo_landed(cfg, task, a)]
            landed_note = "、".join(f"{a}@{(task['repos'][a].get('landed_sha') or '')[:9]}" for a in done)
            if not pending:
                task["owner"] = None
                reg.set_state(task, "landed", note=landed_note)
                say("ok", f"{tid} 已落地到本地 {cfg.integration}。上主干："
                          f"aisk task promote --repos {','.join(tasks.select_repos(cfg, task, None))}")
            else:
                note = (f"已落地：{landed_note}；" if done else "") + "未落地：" + "、".join(pending)
                if blocked:
                    task["last_reject"] = {"at": now_iso(), "reason": "；".join(f"{a}: {w}" for a, w in blocked.items())}
                reg.set_state(task, "rejected" if blocked else "ready", note=note[:500])
                if done:
                    say("ok", f"{'、'.join(done)} 已落地到本地 {cfg.integration}，可以单独上主干："
                              f"aisk task promote --repos {','.join(done)}")
                say("warn", f"{tid} 还有仓库没落地：{'、'.join(pending)}。处理后 aisk task land {tid} --repos {','.join(pending)}")
            tasks.refresh_board(cfg, reg)
            return 1 if blocked else 0
        except WtError as e:
            if dry:
                raise
            task["last_reject"] = {"at": now_iso(), "reason": str(e)}
            reg.set_state(task, "rejected", note=str(e)[:500])
            raise


def land_automatic(cfg, reg, task, alias):
    """Automatically land one ready Xinhua BE/FE task into local fxh.

    This path has no confirmation dialog and has no promote/dev/push step.
    It is intentionally restricted to the two approved task-worktree repos,
    one repository per task, and fails closed on sensitive paths.
    """
    tid = str((task or {}).get("id") or "")
    if not tid:
        raise Reject("自动落地缺少任务编号")
    if alias not in ("be", "web"):
        raise Reject("自动落地仅适用于新华后端 be 与前端 web 仓库")
    _personal_delivery_config(cfg, alias)
    rc = cfg.repo(alias)
    if rc.promote != "ff-trunk":
        raise Reject(f"{alias} 不是前后端 fxh→dev 交付配置")
    if getattr(rc, "workspace_mode", "task-worktree") != "task-worktree":
        raise Reject(f"{alias} 未配置为独立 worktree 仓库，拒绝自动落地")

    with file_lock(tasks.land_lock_path(cfg, alias), wait_msg=f"{alias} 有落地/上主干正在进行，排队"):
        current = reg.load(tid)
        if current.get("state") not in ("ready", "queued", "rejected"):
            raise Reject(f"任务状态 {current.get('state')}，自动落地要求 ready")
        if list(current.get("repos", {})) != [alias]:
            raise Reject("自动落地要求任务只包含本次指定的一个仓库")
        tasks.require_local(cfg, current, ("ready", "queued", "rejected"))
        if cfg.os != "mac" or cfg.integration != "fxh":
            raise Reject("自动落地仅允许在 mac 集成面写入 fxh")
        if current.get("base_task"):
            parent = reg.load(current["base_task"])
            if parent.get("state") not in ("landed", "promoted", "verified", "archived"):
                raise Reject(f"父任务 {parent['id']} 尚未落地")
        if current["state"] != "queued":
            reg.set_state(current, "queued")

        repo = integration_repo(cfg, alias)
        try:
            recover_land_cas(repo, "fxh", land_cas_journal_path(cfg, alias))
            plan = plan_landing(cfg, reg, current, alias, from_hub=False)
            if plan.get("sensitive"):
                raise Reject("自动落地涉及敏感路径，必须转人工处理：" + "、".join(plan["sensitive"][:12]))
            if plan["status"] == "candidate":
                log = cfg.logs_dir / current["id"] / f"land-auto-{alias}-{stamp()}.log"
                gate = gate_prepare(cfg, alias, plan["cand"])
                ok, summary = gates.run_gate(cfg, alias, gate, plan["changed"], log, clean=True)
                if not ok:
                    if log.exists():
                        print(gates.tail(log))
                    raise Reject(f"自动落地门禁失败——{summary}（{log}）")
                if git.current_branch(repo) != "fxh":
                    raise Reject("门禁期间主工作区切换了分支")
                if git.sha(repo, "fxh") != plan["head"]:
                    raise Reject("门禁期间 fxh 前进了，请重新 land")
                clash = sorted(set(git.dirty_paths(repo)) & set(plan["changed"]))
                if clash:
                    raise Reject("门禁期间主工作区出现冲突改动：" + "、".join(clash[:15]))
                ff_compare_and_swap(repo, "fxh", plan["head"], plan["cand"],
                                    journal_path=land_cas_journal_path(cfg, alias))
                current["repos"][alias]["landed_sha"] = plan["cand"]
                say("ok", f"{alias}: fxh 快进到 {plan['cand'][:9]}（自动落地）")
            else:
                tip = plan["tip"]
                if tip != current["repos"][alias].get("ready_sha") or not git.is_ancestor(repo, tip, "fxh"):
                    raise Reject("fxh 已包含候选的判断与 ready SHA 不一致；拒绝补写落地状态")
                # A fast-forward has no merge commit to discover. Recording
                # the ready tip is also what makes replay after a crash between
                # CAS and task-state persistence idempotent.
                current["repos"][alias]["landed_sha"] = (
                    current["repos"][alias].get("landed_sha") or tip)

            reg.save(current)
            record_refs(reg, alias, repo, ["fxh"])
            current["owner"] = None
            reg.set_state(current, "landed", note=f"{alias}@{(current['repos'][alias].get('landed_sha') or plan.get('tip') or '')[:9]}")
            tasks.refresh_board(cfg, reg)
            return 0
        except WtError as exc:
            current["last_reject"] = {"at": now_iso(), "reason": str(exc)}
            reg.set_state(current, "rejected", note=str(exc)[:500])
            raise


# ------------------------------------------------------------------ promote
def pending(repo, rng):
    return git.out(["log", "--oneline", "--no-decorate", *rng.split()], cwd=repo).splitlines()


def promotion_task_id(reg, alias, repo, head):
    ids = []
    for task in reg.all(include_hub=True):
        if task.get("state") not in ("landed", "queued", "promoted"):
            continue
        landed = (task.get("repos") or {}).get(alias, {}).get("landed_sha")
        if landed and git.is_ancestor(repo, landed, head):
            ids.append(task.get("id", ""))
    return ",".join(x for x in ids if x)


def required_promotion_task_id(reg, args, alias, repo, head):
    task_id = getattr(args, "task_id", "") or promotion_task_id(reg, alias, repo, head)
    if not task_id:
        raise Reject(f"{alias} 推送确认无法绑定任务号；请显式传 --task-id，禁止使用无归属弹窗")
    return task_id


def _personal_delivery_config(cfg, alias):
    """Validate the Xinhua personal-publish route before any Git side effect."""
    rc = cfg.repo(alias)
    reject_main_target(cfg, cfg.integration, rc.push_branch, rc.trunk)
    automatic = getattr(rc, "automatic", {}) or {}
    if (getattr(cfg, "profile_name", "") != "xinhua" or alias not in ("be", "web")
            or rc.workspace_mode != "task-worktree" or cfg.integration != "fxh"
            or rc.trunk != "dev" or rc.push_branch != "fxh-dev"
            or automatic.get("commit_task_branch") is not True
            or automatic.get("land_to_local") != "fxh"
            or automatic.get("push_only") != "fxh-dev"):
        raise Reject(f"{alias} 不是 fxh → fxh-dev/dev 前后端交付配置；拒绝复用个人推送流程")
    return rc


def _validate_xinhua_origin(cfg, alias, repo):
    """Require the exact profile fetch and push origin before separated routes act."""
    rc = _personal_delivery_config(cfg, alias)
    expected = (rc.automatic or {}).get("expected_origin_url")
    if not isinstance(expected, str) or not expected or expected != expected.strip():
        raise Reject(f"xinhua.{alias} 必须显式配置 automatic.expected_origin_url")

    def config_values(key):
        result = git.run(["config", "--local", "--get-all", key], cwd=repo, check=False)
        if result.returncode == 1 and not (result.stdout or "").strip():
            return []
        if result.returncode != 0:
            raise Reject(f"无法读取 {alias} 的 Git 远端配置；拒绝继续")
        return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]

    fetch_config = config_values("remote.origin.url")
    push_config = config_values("remote.origin.pushurl")
    if fetch_config != [expected] or (push_config and push_config != [expected]):
        raise Reject(f"{alias} origin 配置与 xinhua.{alias} 档案 expected_origin_url 不匹配")

    fetch = git.run(["remote", "get-url", "--all", "origin"], cwd=repo, check=False)
    push = git.run(["remote", "get-url", "--push", "--all", "origin"], cwd=repo, check=False)
    if fetch.returncode != 0 or push.returncode != 0:
        raise Reject(f"无法解析 {alias} origin 的 fetch/push 地址；拒绝继续")
    fetch_urls = [line.strip() for line in (fetch.stdout or "").splitlines() if line.strip()]
    push_urls = [line.strip() for line in (push.stdout or "").splitlines() if line.strip()]
    if fetch_urls != [expected] or push_urls != [expected]:
        raise Reject(f"{alias} origin fetch/push 地址必须唯一且匹配档案 expected_origin_url")
    mirror = config_values("remote.origin.mirror")
    if any(value.lower() in {"1", "true", "yes", "on"} for value in mirror):
        raise Reject(f"{alias} origin mirror 模式禁止 separated delivery 操作")


def publish_personal_branch(cfg, reg, alias, args=None, *, dry=False):
    """Run only the personal fxh → fxh-dev publish step; never merge or push dev.

    This is deliberately separate from ``promote_one``: callers that want the
    personal branch update can invoke this operation without entering the
    legacy combined personal-push/dev-push flow.
    """
    _validate_xinhua_origin(cfg, alias, integration_repo(cfg, alias))
    repo = integration_repo(cfg, alias)
    with file_lock(tasks.land_lock_path(cfg, alias), wait_msg=f"{alias} 有落地正在进行，排队"):
        fetched = git.run(["fetch", "origin", "--prune"], cwd=repo, check=False)
        if fetched.returncode != 0:
            raise Reject(f"fetch origin 失败：{fetched.stderr.strip()[-300:]}")
        if git.current_branch(repo) != "fxh":
            raise Reject(f"主工作区不在 fxh：{repo}")
        found = audit_branch(cfg, reg, alias, repo, "fxh", strict=False)
        problems = report_audit(found)
        if problems:
            raise Reject("；".join(problems))
        head = git.sha(repo, "fxh")
        if not head:
            raise Reject(f"{alias} 找不到本地 fxh 提交")

        remote = git.sha(repo, "origin/fxh-dev")
        if remote and not git.is_ancestor(repo, remote, head):
            raise Reject("本地 fxh 与 origin/fxh-dev 已分叉；拒绝改写个人远端分支")
        commits = pending(repo, f"origin/fxh-dev..{head}") if remote else pending(repo, f"-20 {head}")
        if not commits:
            say("info", f"{alias}: origin/fxh-dev 已是最新")
            return 0
        summary = f"{alias}：fxh → origin/fxh-dev，仅推送个人分支，不合并或推送 dev；{len(commits)} 个提交：\n" + "\n".join(commits[:25])
        print(summary)
        if dry:
            say("info", "（dry-run 不推送）")
            return 0

        gate = gate_prepare(cfg, alias, head)
        log = cfg.logs_dir / "promote" / f"personal-{alias}-{stamp()}.log"
        base = remote or git.out(["hash-object", "-t", "tree", "--stdin"], cwd=repo, input_text="")
        changed = git.diff_names(repo, base, head)
        ok, gate_summary = gates.run_gate(cfg, alias, gate, changed, log, clean=True)
        if not ok:
            if log.exists():
                print(gates.tail(log))
            raise Reject(f"个人分支推送前门禁失败：{gate_summary}（{log}）")
        if git.sha(repo, "fxh") != head:
            raise Reject("门禁期间 fxh 引用发生变化，请重新发布")

        if push_requires_confirmation(cfg, alias, "fxh-dev"):
            task_id = required_promotion_task_id(reg, args, alias, repo, head)
            if not registry.confirm_human(
                    summary, _push_expect(args),
                    context=action_context(args, "git推送", task_id, summary,
                                           repository=_repository_label_for_workbuddy(args, repo))):
                raise Reject("未确认推送 fxh-dev，停止")
        # Explicit refspec is the entire mutation surface of this operation.
        _validate_xinhua_origin(cfg, alias, repo)
        git.run(["push", "origin", f"{head}:refs/heads/fxh-dev"], cwd=repo)
        say("ok", f"已推送 origin/fxh-dev（仅个人分支）")
        return 0


def merge_integration_to_local_dev(cfg, reg, alias, args=None, *, dry=False):
    """Confirm and fast-forward the local dev anchor from fxh; this never pushes."""
    _personal_delivery_config(cfg, alias)
    repo = integration_repo(cfg, alias)
    anchor = cfg.anchor_path(alias, "dev")
    _validate_xinhua_origin(cfg, alias, repo)
    _validate_xinhua_origin(cfg, alias, anchor)
    with file_lock(tasks.land_lock_path(cfg, alias), wait_msg=f"{alias} 有落地/推送正在进行，排队"):
        if not anchor.exists() or git.current_branch(anchor) != "dev":
            raise Reject(f"本地 dev 锚点不存在或分支错误：{anchor}")
        if git.dirty(anchor):
            raise Reject(f"本地 dev 锚点有未提交改动：{anchor}")
        if git.current_branch(repo) != "fxh":
            raise Reject(f"主工作区不在 fxh：{repo}")
        old_dev = git.sha(anchor, "refs/heads/dev")
        fxh = git.sha(repo, "refs/heads/fxh")
        if not old_dev or not fxh:
            raise Reject("找不到本地 dev 或 fxh 提交")
        if old_dev == fxh:
            say("info", f"{alias}: 本地 dev 已包含全部 fxh 提交")
            return 0
        if not git.is_ancestor(repo, old_dev, fxh):
            raise Reject("本地 dev 无法快进到 fxh；先核对分叉提交，不会自动解冲突")
        commits = pending(repo, f"{old_dev}..{fxh}")
        prompt = f"{alias}：确认将本地 fxh 快进合并到本地 dev（不推送），{len(commits)} 个提交：\n" + "\n".join(commits[:25])
        print(prompt)
        if dry:
            say("info", "（dry-run 需要操作者确认；未修改本地 dev）")
            return 0
        task_id = required_promotion_task_id(reg, args, alias, repo, fxh)
        if not registry.confirm_human(prompt, "落地", context=action_context(args, "git合并", task_id, prompt)):
            raise Reject("未确认合并本地 dev，分支保持不变")
        if git.current_branch(anchor) != "dev" or git.dirty(anchor):
            raise Reject("确认期间本地 dev 锚点切换或出现未提交改动")
        if git.sha(anchor, "refs/heads/dev") != old_dev or git.sha(repo, "refs/heads/fxh") != fxh:
            raise Reject("确认期间本地 dev 或 fxh 已变化，请重新执行")
        git.run(["merge", "--ff-only", fxh], cwd=anchor)
        if git.sha(anchor, "refs/heads/dev") != fxh:
            raise Reject("本地 dev 快进结果与确认时 fxh 不一致")
        record_refs(reg, alias, repo, ["dev", "fxh"])
        say("ok", f"{alias}: 本地 dev 已快进到 fxh（未推送）")
        return 0


def push_local_dev(cfg, reg, alias, args=None, *, dry=False):
    """Confirm and push local dev; this operation never merges fxh into dev."""
    _personal_delivery_config(cfg, alias)
    repo = integration_repo(cfg, alias)
    anchor = cfg.anchor_path(alias, "dev")
    _validate_xinhua_origin(cfg, alias, repo)
    _validate_xinhua_origin(cfg, alias, anchor)
    with file_lock(tasks.land_lock_path(cfg, alias), wait_msg=f"{alias} 有落地/推送正在进行，排队"):
        if not anchor.exists() or git.current_branch(anchor) != "dev":
            raise Reject(f"本地 dev 锚点不存在或分支错误：{anchor}")
        if git.dirty(anchor):
            raise Reject(f"本地 dev 锚点有未提交改动：{anchor}")
        fetched = git.run(["fetch", "origin", "refs/heads/dev:refs/remotes/origin/dev"], cwd=repo, check=False)
        if fetched.returncode != 0:
            raise Reject(f"fetch origin/dev 失败：{fetched.stderr.strip()[-300:]}")
        remote_dev = git.sha(repo, "refs/remotes/origin/dev")
        local_dev = git.sha(anchor, "refs/heads/dev")
        if not remote_dev or not local_dev:
            raise Reject("找不到 origin/dev 或本地 dev 提交")
        if local_dev == remote_dev:
            say("info", f"{alias}: origin/dev 已是最新")
            return 0
        if not git.is_ancestor(repo, remote_dev, local_dev):
            raise Reject("本地 dev 与 origin/dev 已分叉；拒绝推送")
        commits = pending(repo, f"{remote_dev}..{local_dev}")
        prompt = f"{alias}：确认推送本地 dev → origin/dev，{len(commits)} 个提交：\n" + "\n".join(commits[:25])
        print(prompt)
        if dry:
            say("info", "（dry-run 不推送）")
            return 0
        task_id = required_promotion_task_id(reg, args, alias, repo, local_dev)
        if not registry.confirm_human(
                prompt, _push_expect(args),
                context=action_context(args, "git推送", task_id, prompt,
                                       repository=_repository_label_for_workbuddy(args, anchor))):
            raise Reject("未确认推送 dev，停止")
        if git.current_branch(anchor) != "dev" or git.dirty(anchor):
            raise Reject("确认期间本地 dev 锚点切换或出现未提交改动")
        if git.sha(anchor, "refs/heads/dev") != local_dev or git.sha(repo, "refs/remotes/origin/dev") != remote_dev:
            raise Reject("确认期间本地 dev 或 origin/dev 已变化，请重新执行")
        _validate_xinhua_origin(cfg, alias, repo)
        _validate_xinhua_origin(cfg, alias, anchor)
        git.run(["push", "origin", "refs/heads/dev:refs/heads/dev"], cwd=anchor)
        say("ok", f"{alias}: 已推送 origin/dev")
        record_refs(reg, alias, repo, ["dev", "fxh"])
        return 0


def cmd_promote(cfg, reg, args):
    if cfg.os == "windows" and not cfg.windows_integration:
        raise WtError("promote 默认只在 mac 集成面执行；Windows 请显式配置 worktrees.windows_integration")
    aliases = [a.strip() for a in args.repos.split(",")] if args.repos else list(cfg.repo_order)
    rc_all = 0
    for alias in aliases:
        rc = cfg.repo(alias)
        if getattr(rc, "workspace_mode", "task-worktree") == "direct":
            say("err", f"{alias} 使用 direct checkout；promote 不适用，必须由 direct-finish 按任务范围完成")
            rc_all = 1
            continue
        repo = integration_repo(cfg, alias)
        if not repo.exists():
            continue
        say("info", f"==== {alias}（{repo}）")
        with file_lock(tasks.land_lock_path(cfg, alias), wait_msg=f"{alias} 有落地正在进行，排队"):
            try:
                promote_one(cfg, reg, alias, rc, args.dry_run, args=args)
            except Reject as e:
                say("err", str(e))
                rc_all = 1
    if not args.dry_run:
        mark_promoted(cfg, reg)
        tasks.refresh_board(cfg, reg)
    return rc_all


def promote_one(cfg: WtConfig, reg: Registry, alias, rc, dry, args=None):
    if getattr(rc, "workspace_mode", "task-worktree") == "direct":
        raise Reject(f"{alias} 使用 direct checkout；promote 禁止绕过 direct-finish 的任务范围与 lease 门禁")
    personal_only = (getattr(cfg, "profile_name", None) == "xinhua" and alias in ("be", "web")
                     and cfg.integration == "fxh" and rc.trunk == "dev" and rc.push_branch == "fxh-dev")
    # 只查这个仓库真正会写的分支：push-only（如文档仓库只推 fxh）不碰主干，主干叫 master 也不该被拦。
    # promote 只读取 cfg.integration 的提交引用并写独立 anchor/远端；fxh 主工作区
    # 可以保留用户未提交改动。真正会改写 fxh 文件的 land 仍在提交前做清洁检查。
    reject_main_target(cfg, cfg.integration, rc.push_branch, *([rc.trunk] if rc.promote == "ff-trunk" else []))
    repo = integration_repo(cfg, alias)
    if personal_only:
        _validate_xinhua_origin(cfg, alias, repo)
    r = git.run(["fetch", "origin", "--prune"], cwd=repo, check=False)
    if r.returncode != 0:
        raise Reject(f"fetch origin 失败：{r.stderr.strip()[-300:]}")
    if git.current_branch(repo) != cfg.integration:
        raise Reject(f"主工作区不在 {cfg.integration}")
    found = audit_branch(cfg, reg, alias, repo, cfg.integration, strict=False)
    for b in list(rc.anchors) + list(rc.audited):
        found += audit_branch(cfg, reg, alias, repo, b, strict=True)
    probs = report_audit(found)
    if probs:
        raise Reject("；".join(probs))
    head = git.sha(repo, cfg.integration)

    if rc.promote == "ff-trunk" and not personal_only:
        trunk = rc.trunk
        ot = git.sha(repo, f"origin/{trunk}")
        if not ot:
            raise Reject(f"找不到 origin/{trunk}")
        if not git.is_ancestor(repo, ot, head):
            clean, tree, conflicts = git.merge_tree(repo, head, ot)
            if not clean:
                raise Reject(
                    f"origin/{trunk} 与 {cfg.integration} 冲突 {len(conflicts)} 个文件：{'、'.join(conflicts[:15])}。"
                    f"先 aisk task find 同步 看有没有进行中的同步任务；没有就开一个："
                    f"aisk task new sync-{trunk}-into-{cfg.integration} --repos {alias} --title \"同步 {trunk} 最新代码\" "
                    f"--goal \"把 origin/{trunk} 合入 {cfg.integration} 并解决冲突\" --accept \"门禁通过\"，"
                    f"在里面比较共同祖先、两侧提交时间和业务意图，逐文件逐块解冲突（禁止 ours/theirs 一键覆盖；钩子误拦合并时用 aisk task merge-commit），"
                    f"然后 check/ready/land，再重新 promote")
            cand = git.commit_tree(repo, tree, [head, ot], f"merge: 同步 origin/{trunk} 最新代码")
            changed = git.diff_names(repo, head, cand)
            if dry:
                say("ok", f"需要先把 origin/{trunk} 同步进 {cfg.integration}（无冲突，{len(changed)} 个文件）")
            else:
                gate = gate_prepare(cfg, alias, cand)
                log = cfg.logs_dir / "promote" / f"sync-{alias}-{stamp()}.log"
                ok, summary = gates.run_gate(cfg, alias, gate, changed, log, clean=True)
                if not ok:
                    print(gates.tail(log))
                    raise Reject(f"同步 origin/{trunk} 后门禁失败——{summary}（{log}）")
                if git.sha(repo, cfg.integration) != head:
                    raise Reject(f"门禁期间 {cfg.integration} 有变化，请重新 promote")
                if manual_merge_required(cfg):
                    prompt = sync_prompt(alias, cfg.integration, trunk, head, cand, changed, summary)
                    print(prompt)
                    if not registry.confirm_human(prompt, "落地", context=action_context(args, "git合并", required_promotion_task_id(reg, args, alias, repo, head), prompt)):
                        raise Reject("未确认同步主干候选，集成分支保持不变")
                git.run(["merge", "--ff-only", cand], cwd=repo)
                head = cand
                record_refs(reg, alias, repo, [cfg.integration])
                say("ok", f"已同步 origin/{trunk} 进 {cfg.integration}（门禁：{summary}）")

    if not dry:
        comparison_branch = rc.push_branch if personal_only else rc.trunk
        comparison = git.sha(repo, f"origin/{comparison_branch}") or head
        gate = gate_prepare(cfg, alias, head)
        log = cfg.logs_dir / "promote" / f"check-{alias}-{stamp()}.log"
        ok, summary = gates.run_gate(cfg, alias, gate, git.diff_names(repo, comparison, head), log, clean=True)
        if not ok:
            raise Reject(f"推送前门禁失败：{summary}（{log}）")
        if git.sha(repo, cfg.integration) != head:
            raise Reject("推送前集成分支引用发生变化，请重新验证")

    push_branch = rc.push_branch
    remote_push = git.sha(repo, f"origin/{push_branch}")
    pend = pending(repo, f"origin/{push_branch}..{cfg.integration}") if remote_push else pending(repo, f"-20 {cfg.integration}")
    if pend:
        text = f"{alias}：{cfg.integration} → origin/{push_branch}，{len(pend)} 个提交：\n" + "\n".join(pend[:25])
        print(text)
        if dry:
            say("info", "（dry-run 不推送）")
        else:
            requires_confirmation = push_requires_confirmation(cfg, alias, push_branch)
            confirmed = (not requires_confirmation
                         or registry.confirm_human(
                             text,
                             _push_expect(args),
                             context=action_context(
                                 args,
                                 "git推送",
                                 required_promotion_task_id(reg, args, alias, repo, head),
                                 text,
                                 repository=_repository_label_for_workbuddy(args, repo),
                             ),
                         ))
            if confirmed:
                if personal_only:
                    _validate_xinhua_origin(cfg, alias, repo)
                git.run(["push", "origin", f"{head}:refs/heads/{push_branch}"], cwd=repo)
                suffix = "（个人分支默认放行）" if not requires_confirmation else ""
                say("ok", f"已推送 origin/{push_branch}{suffix}")
            else:
                raise Reject(f"未确认推送 {push_branch}，停止")
    else:
        say("info", f"origin/{push_branch} 已是最新")

    # fxh → fxh-dev is a personal publish operation only. Local fxh → dev
    # integration and origin/dev publication have their own explicit APIs and
    # confirmation gates; this legacy compound entry point must never fall
    # through to either dev operation.
    if cfg.integration == "fxh" and rc.trunk == "dev" and rc.push_branch == "fxh-dev":
        say("info", "个人分支发布已结束；合并本地 dev 与推送 dev 请分别显式执行独立操作")
        return
    if rc.promote != "ff-trunk":
        return
    trunk = rc.trunk
    anchor = cfg.anchor_path(alias, trunk)
    if not anchor.exists() or git.current_branch(anchor) != trunk:
        raise Reject(f"{trunk} 锚点不存在或不在 {trunk} 上：{anchor}")
    if git.dirty(anchor):
        raise Reject(f"{trunk} 锚点有未提交改动：{anchor}")
    if dry:
        say("ok", f"dry-run：{trunk} 将快进 {len(pending(repo, f'origin/{trunk}..{cfg.integration}'))} 个提交并推送")
        return
    if not git.ok(["merge", "--ff-only", f"origin/{trunk}"], cwd=anchor):
        raise Reject(f"本地 {trunk} 与 origin/{trunk} 分叉，停止（锚点只允许快进）")
    ot = git.sha(repo, f"origin/{trunk}")
    if not git.is_ancestor(repo, ot, head):
        raise Reject(f"{trunk} 无法快进到 {cfg.integration}（有人刚推了 {trunk}），请重新 promote")
    pend = pending(repo, f"origin/{trunk}..{head}")
    if not pend:
        say("info", f"origin/{trunk} 已包含全部提交")
        return
    text = f"{alias}：{trunk} → origin/{trunk}，{len(pend)} 个提交（推送后可能触发部署）：\n" + "\n".join(pend[:25])
    print(text)
    if not registry.confirm_human(text, _push_expect(args), context=action_context(
            args, "git推送", required_promotion_task_id(reg, args, alias, repo, head), text,
            repository=_repository_label_for_workbuddy(args, repo))):
        raise Reject(f"未确认推送 {trunk}，停止")

    anchor_before = git.sha(anchor, "HEAD")
    if not git.ok(["merge", "--ff-only", head], cwd=anchor):
        raise Reject(f"{trunk} 无法快进到 {cfg.integration}，请重新 promote")
    if git.sha(anchor, "HEAD") != head or git.dirty(anchor):
        git.run(["reset", "--hard", anchor_before], cwd=anchor)
        raise Reject("确认期间锚点改变或快进后提交号不一致，已回退锚点并拒绝推送")
    try:
        # 必须按分支名推送：部分仓库的 pre-push 钩子拒绝 <sha>:refs/heads/<主干> 写法
        git.run(["push", "origin", trunk], cwd=anchor)
    except Exception as e:
        git.run(["reset", "--hard", anchor_before], cwd=anchor)
        raise Reject(f"推送 origin/{trunk} 失败（锚点已安全回退到 {anchor_before[:9]}）：{e}")
    record_refs(reg, alias, repo, [trunk, cfg.integration])
    say("ok", f"已推送 origin/{trunk}")
    hint = ((cfg.raw.get("repos") or {}).get(alias) or {}).get("promote_hint")
    if hint:
        say("info", str(hint))


def mark_promoted(cfg: WtConfig, reg: Registry):
    for t in reg.all(include_hub=True):
        if t["state"] != "landed":
            continue
        done = True
        for alias, r in t["repos"].items():
            rc = cfg.repo(alias)
            target = f"origin/{rc.trunk}" if rc.promote == "ff-trunk" else f"origin/{rc.push_branch}"
            ls = r.get("landed_sha") or r.get("ready_sha")  # 没有新提交的仓库没有落地合并，看它交付的提交
            repo = integration_repo(cfg, alias)
            if not ls or not git.sha(repo, target) or not git.is_ancestor(repo, ls, target):
                done = False
                break
        if done:
            t.pop("_from_hub", None)
            reg.set_state(t, "promoted", note="已进入远端主干")
            say("ok", f"{t['id']} → promoted，共享环境验证后 aisk task verify {t['id']} --pass --note …")


# ------------------------------------------------------------------ verify / revert
def cmd_verify(cfg, reg, args):
    task = reg.find_by_ref(args.task)
    if task["state"] not in ("promoted", "reverting", "verified"):
        raise WtError(f"任务状态 {task['state']}，promote 之后才能验证")
    task["verify"] = {"at": now_iso(), "pass": bool(args.passed), "note": args.note or ""}
    if args.passed:
        reg.set_state(task, "verified", note=args.note or "验证通过")
        say("ok", f"{task['id']} 验证通过，可 aisk task archive {task['id']}")
    else:
        reg.set_state(task, "reverting", note=args.note or "验证失败")
        say("warn", f"{task['id']} 验证失败：aisk task revert {task['id']} 生成回滚任务")
    tasks.refresh_board(cfg, reg)
    return 0


def cmd_revert(cfg, reg, args):
    # Revert creates a new task from the configured integration branch. Refuse
    # before reading or creating task state if that branch is main.
    reject_main_target(cfg, cfg.integration)
    old = reg.find_by_ref(args.task)
    if old["state"] not in ("landed", "promoted", "verified", "reverting"):
        raise WtError(f"任务状态 {old['state']}，没有可回滚的落地")
    targets = {a: r["landed_sha"] for a, r in old["repos"].items() if r.get("landed_sha")}
    if not targets:
        raise WtError("登记簿里没有落地合并提交，无法自动回滚")
    words = ["revert"] + [w for w in (old.get("slug") or "task").split("-")][:4]
    slug = "-".join(words)
    while len(slug) > 40 and len(words) > 2:
        words.pop()
        slug = "-".join(words)
    tool, sessions = tasks.caller(args)
    new = tasks.create_task(cfg, reg, slug=slug, title=f"回滚{old['title']}"[:30], repos=list(targets),
                            goal=f"撤销 {old['id']} {old['title']} 的落地合并",
                            accept=f"共享环境上 {old['id']} 的改动已撤销并验证", new_anyway=f"回滚 {old['id']}",
                            tool=tool, sessions=sessions, extra={"reverts": old["id"]})
    msg = f"revert: 回滚{old['title'][:40]}({old['id']})"
    if model.check_message(cfg, msg):
        msg = f"revert: 回滚任务({old['id']})"
    for alias, merge_sha in targets.items():
        path = Path(new["repos"][alias]["path"])
        git.run(["revert", "--no-commit", "-m", "1", merge_sha], cwd=path)
        git.run(["commit", "-m", msg], cwd=path)
        say("ok", f"{alias}: 已在 {new['id']} 生成回滚提交")
    if old["state"] != "reverting":
        reg.set_state(old, "reverting", note=f"回滚任务 {new['id']}")
    say("info", f"下一步：aisk task check {new['id']} → 填 HANDOFF.md → aisk task ready {new['id']} → aisk task land {new['id']} → aisk task promote")
    return 0
