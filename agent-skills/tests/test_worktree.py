#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多 AI 工作区内核回归：真实临时 git 仓库 + 假档案。不访问业务仓库、网络、用户配置或真实口令。

跑法：python3 -m unittest tests.test_worktree
"""
import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine import cli as aisk_cli  # noqa: E402
from engine import profile as profile_mod  # noqa: E402
from engine.worktree import (actor, bind, config as wt_config, doctor, gates, gitops as git, guards, hooks,  # noqa: E402
                             integrate, legacy, model, names, registry, tasks)
from engine.worktree import cli as wt_cli  # noqa: E402
from engine.worktree.config import WtError, build_config  # noqa: E402
from engine.worktree.registry import Registry  # noqa: E402

IDENTITY_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "AISK_TOOL", "AISK_SESSION",
                "CLAUDE_PID", "CLAUDE_ENV_FILE", "CLAUDE_PROJECT_DIR")


def make_profile(root, **overrides):
    wt = {
        "data_root": str(root / "data"), "integration_branch": "fxh", "protected": ["main", "dev", "fxh"],
        "lease": {"idle_minutes": 30, "claimable_minutes": 120},
        "repos": {"be": {"profile_repo": "backend", "trunk": "dev", "push_branch": "fxh-dev", "promote": "ff-trunk",
                         "anchors": ["dev", "main"], "gate": {"kind": "none"}}},
    }
    wt.update(overrides)
    return {"project": "demo", "_path": root / "demo.yaml", "repos": {"backend": str(root / "main" / "backend")},
            "worktrees": wt}


class WindowsIntegrationConfig(unittest.TestCase):
    def test_shared_integration_requires_explicit_opt_in_and_repo_mapping(self):
        with tempfile.TemporaryDirectory(prefix="wt-win-integration-") as tmp:
            root = Path(tmp).resolve()
            profile = make_profile(root, hub=str(root / "hub"), windows_integration=True)
            profile["repos"]["backend_shared"] = str(root / "shared-backend")
            profile["worktrees"]["repos"]["be"]["integration_repo"] = "backend_shared"

            cfg = build_config(profile, "windows")

            self.assertTrue(cfg.windows_integration)
            self.assertEqual(cfg.integration_repo("be"), root / "shared-backend")

    def test_windows_integration_stays_protected_by_default(self):
        with tempfile.TemporaryDirectory(prefix="wt-win-protected-") as tmp:
            root = Path(tmp).resolve()
            profile = make_profile(root, hub=str(root / "hub"))
            profile["repos"]["backend_shared"] = str(root / "shared-backend")
            profile["worktrees"]["repos"]["be"]["integration_repo"] = "backend_shared"

            cfg = build_config(profile, "windows")

            with self.assertRaisesRegex(WtError, "默认不能执行集成"):
                cfg.integration_repo("be")

            with self.assertRaisesRegex(WtError, "默认只在 mac 集成面执行"):
                integrate.cmd_land(cfg, None, type("Args", (), {"task": "W001", "dry_run": False})())
            with self.assertRaisesRegex(WtError, "默认只在 mac 集成面执行"):
                integrate.cmd_promote(cfg, None, type("Args", (), {"repos": None, "dry_run": False})())

    def test_windows_integration_requires_each_repo_mapping(self):
        with tempfile.TemporaryDirectory(prefix="wt-win-mapping-") as tmp:
            root = Path(tmp).resolve()
            profile = make_profile(root, hub=str(root / "hub"), windows_integration=True)

            with self.assertRaisesRegex(WtError, "必须声明 integration_repo"):
                build_config(profile, "windows")

            profile["worktrees"]["windows_integration"] = "false"
            with self.assertRaisesRegex(WtError, "必须是 true 或 false"):
                build_config(profile, "windows")

    def test_windows_init_creates_hub_and_gate_for_explicit_integration(self):
        with tempfile.TemporaryDirectory(prefix="wt-win-init-") as tmp:
            root = Path(tmp).resolve()
            source = root / "mac-source"
            source.mkdir()
            git.run(["init", "-q", "-b", "fxh"], cwd=source)
            (source / "README.md").write_text("baseline\n", encoding="utf-8")
            git.run(["add", "README.md"], cwd=source)
            with patch.dict(os.environ, {"GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "tester@example.test",
                                         "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "tester@example.test"}):
                git.run(["commit", "-qm", "feat: 初始化"], cwd=source)
            git.run(["branch", "dev"], cwd=source)
            git.run(["update-ref", "refs/remotes/origin/dev", "HEAD"], cwd=source)
            integration = root / "integration"
            git.run(["clone", "-q", str(source), str(integration)])
            profile = make_profile(root, hub=str(root / "hub"), windows_integration=True)
            profile["worktrees"]["repos"]["be"]["mac_source"] = str(source)
            profile["repos"]["backend_shared"] = str(integration)
            profile["worktrees"]["repos"]["be"]["integration_repo"] = "backend_shared"
            cfg = build_config(profile, "windows")

            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(doctor.init_windows(cfg, Registry(cfg), type("Args", (), {"apply": True})()), 0)
            self.assertTrue((cfg.hub / "be.git" / "HEAD").is_file())
            self.assertTrue((cfg.repo("be").path / "HEAD").is_file())
            self.assertTrue((cfg.anchor_path("be", "gate") / "README.md").is_file())
            self.assertEqual(Path(git.config_get(cfg.repo("be").path, "remote.hub.url")).resolve(),
                             (cfg.hub / "be.git").resolve())

    def test_windows_explicit_integration_lands_ready_task_after_confirmation(self):
        with tempfile.TemporaryDirectory(prefix="wt-win-land-") as tmp:
            root = Path(tmp).resolve()
            source = root / "mac-source"
            source.mkdir()
            env = {"GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "tester@example.test",
                   "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "tester@example.test"}
            with patch.dict(os.environ, env):
                git.run(["init", "-q", "-b", "fxh"], cwd=source)
                (source / "README.md").write_text("baseline\n", encoding="utf-8")
                git.run(["add", "README.md"], cwd=source)
                git.run(["commit", "-qm", "feat: 初始化"], cwd=source)
                git.run(["branch", "dev"], cwd=source)
                git.run(["update-ref", "refs/remotes/origin/dev", "HEAD"], cwd=source)
                integration = root / "integration"
                git.run(["clone", "-q", str(source), str(integration)])
                profile = make_profile(root, hub=str(root / "hub"), windows_integration=True,
                                       merge_policy={"confirm_land": True})
                profile["worktrees"]["repos"]["be"].update(mac_source=str(source), integration_repo="backend_shared")
                profile["repos"]["backend_shared"] = str(integration)
                cfg = build_config(profile, "windows")
                reg = Registry(cfg)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(doctor.init_windows(cfg, reg, type("Args", (), {"apply": True})()), 0)

                parser = wt_cli.build_parser()

                def run(command):
                    args = parser.parse_args(shlex.split(command))
                    return args.func(cfg, reg, args)

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run("new windows-native-confirm --title Windows原生确认 --repos be "
                                         "--goal native-dialog --accept land-after-confirm "
                                         "--tool codex --session session-1"), 0)
                task = reg.load("W001")
                task_repo = Path(task["repos"]["be"]["path"])
                (task_repo / "windows.txt").write_text("landed only after confirmation\n", encoding="utf-8")
                git.run(["add", "windows.txt"], cwd=task_repo)
                git.run(["commit", "-qm", "feat: Windows 集成确认"], cwd=task_repo)
                handoff = Path(task["dir"]) / "HANDOFF.md"
                handoff.write_text(handoff.read_text(encoding="utf-8").replace("（待填写）", "无"), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run("ready W001 --tool codex --session session-1"), 0)
                    with patch.object(registry, "confirm_human", return_value=True) as confirm:
                        self.assertEqual(run("land W001 --tool codex --session session-1"), 0)

                confirm.assert_called_once()
                self.assertEqual((integration / "windows.txt").read_text(encoding="utf-8"), "landed only after confirmation\n")
                self.assertEqual(reg.load("W001")["state"], "landed")


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wt-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "home").mkdir()
        env = patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"), "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "tester@example.test",
            "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "tester@example.test",
            # 必须显式钉住：profile.runtime_root() 优先读 AISK_HOME，外层（启动器、
            # 任务 worktree 的 shell）导出了它时，bind 的产物会落到真实运行根，
            # 把 lane.gitconfig 里的推送封锁改写掉。见 test_runtime_root_never_escapes_sandbox。
            "AISK_HOME": str(self.root / "aisk"),
        })
        env.start()
        self.addCleanup(env.stop)
        for key in list(os.environ):
            if key in IDENTITY_ENV or key.startswith(("CODEX_", "CODEBUDDY_")):
                os.environ.pop(key)
        pdir = patch.object(profile_mod, "PROFILE_DIR", self.root / "aisk" / "profiles")
        pdir.start()
        self.addCleanup(pdir.stop)
        self.prof = make_profile(self.root)
        self.cfg = build_config(self.prof, "mac")
        loader = patch.object(wt_config, "load_config", return_value=(self.cfg, "测试档案"))
        loader.start()
        self.addCleanup(loader.stop)
        no_ps = patch.object(actor, "ancestor_pid", return_value=None)
        no_ps.start()
        self.addCleanup(no_ps.stop)
        self.reg = Registry(self.cfg)
        self.repo = self.cfg.repo("be").path
        self.repo.mkdir(parents=True)
        git.run(["init", "-q", "-b", "fxh"], cwd=self.repo)
        (self.repo / "file.txt").write_text("baseline\n")
        git.run(["add", "."], cwd=self.repo)
        git.run(["commit", "-qm", "feat: 初始化"], cwd=self.repo)
        git.run(["branch", "dev"], cwd=self.repo)
        git.run(["branch", "main"], cwd=self.repo)
        git.worktree_add_unique(self.repo, self.cfg.anchor_path("be", "gate"), "xa-be-gate", "fxh", detach=True)
        silence = contextlib.redirect_stdout(io.StringIO())
        silence.__enter__()
        self.addCleanup(silence.__exit__, None, None, None)

    # ---------------------------------------------------------------- 工具
    def args(self, cmd):
        return wt_cli.build_parser().parse_args(shlex.split(cmd))

    def run_cmd(self, cmd):
        a = self.args(cmd)
        return a.func(self.cfg, self.reg, a)

    def new(self, slug="first-demo-task", title="第一个演示任务", extra=""):
        self.run_cmd(f"new {slug} --title {title} --repos be --goal 目标 --accept 验收 {extra}")
        return max(self.reg.all(), key=lambda t: t["id"])

    def commit(self, task, content="changed\n", name="file.txt"):
        wt = Path(task["repos"]["be"]["path"])
        (wt / name).write_text(content)
        git.run(["add", "."], cwd=wt)
        git.run(["commit", "-qm", "feat: 修改内容"], cwd=wt)
        return git.sha(wt, "HEAD")

    def fill_handoff(self, task):
        p = Path(task["dir"]) / "HANDOFF.md"
        p.write_text(p.read_text(encoding="utf-8").replace("（待填写）", "无"), encoding="utf-8")

    def ready(self, task):
        self.fill_handoff(task)
        self.run_cmd(f"check {task['id']}")
        self.run_cmd(f"ready {task['id']}")

    def age_task(self, tid, minutes):
        """把任务的心跳与进度文件时间拨回 minutes 分钟前（任务分支没有新提交、工作区干净时即为全部活动来源）。"""
        t = self.reg.load(tid)
        past = time.time() - minutes * 60
        if t.get("owner"):
            import datetime
            t["owner"]["heartbeat_at"] = datetime.datetime.fromtimestamp(past).astimezone().isoformat(timespec="seconds")
            self.reg.save(t)
        prog = Path(t["dir"]) / "PROGRESS.md"
        os.utime(prog, (past, past))

    def remote(self):
        remote = self.root / "remote.git"
        git.run(["init", "--bare", "-q", str(remote)])
        git.run(["remote", "add", "origin", str(remote)], cwd=self.repo)
        git.run(["push", "-q", "origin", "dev", "fxh:fxh-dev"], cwd=self.repo)
        git.run(["fetch", "-q", "origin"], cwd=self.repo)
        anchor = self.cfg.anchor_path("be", "dev")
        git.worktree_add_unique(self.repo, anchor, "xa-be-dev", "dev", branch="dev")
        return remote, anchor


class MergeSafety(Sandbox):
    def enable_policy(self):
        self.cfg.raw["merge_policy"] = {
            "confirm_land": True,
            "forbid_main_operations": True,
            "conflict_resolution": "common_ancestor_temporal_semantic",
        }

    def test_land_requires_human_confirmation_before_fast_forward(self):
        self.enable_policy()
        task = self.new()
        self.commit(task)
        self.ready(task)
        before = git.sha(self.repo, "fxh")
        with patch.object(registry, "confirm_human", return_value=False) as confirm:
            with self.assertRaises(integrate.Reject):
                self.run_cmd(f"land {task['id']}")
        self.assertTrue(confirm.called)
        self.assertEqual(git.sha(self.repo, "fxh"), before)
        self.assertEqual(self.reg.load(task["id"])["state"], "rejected")

    def test_land_rejects_unrelated_integration_history_with_actionable_error(self):
        task = self.new()
        self.commit(task)
        self.ready(task)
        before = git.sha(self.repo, "fxh")
        with patch.object(integrate.git, "merge_base", return_value=None):
            self.assertEqual(self.run_cmd(f"land {task['id']} --dry-run"), 1)
        self.assertEqual(git.sha(self.repo, "fxh"), before)
        self.assertEqual(self.reg.load(task["id"])["state"], "ready")

    def test_task_guard_rejects_main_and_bulk_conflict_choices(self):
        self.enable_policy()
        task = self.new()
        task_root = str(Path(task["repos"]["be"]["path"]))
        denied = (
            "git merge main",
            "git merge origin/main",
            "git rebase refs/remotes/origin/main",
            "git merge -X ours origin/dev",
            "git checkout --theirs .",
        )
        for command in denied:
            with self.subTest(command=command), self.assertRaises(guards.Deny):
                guards.check_bash(self.cfg, command, task_root, task_root)
        self.assertFalse(guards.check_bash(self.cfg, "git log origin/main", task_root, task_root))

    def test_task_commit_is_independent_from_dirty_integration_checkout(self):
        task = self.new()
        # 模拟用户正在 fxh 主工作区编辑的业务文件；任务 worktree 仍应可以独立提交、check、ready。
        (self.repo / "file.txt").write_text("fxh 上未提交的用户修改\n")
        wt = Path(task["repos"]["be"]["path"])
        (wt / "task.txt").write_text("任务分支修改\n")
        with patch.dict(os.environ, {"AISK_TOOL": "codex", "AISK_SESSION": "commit-test"}):
            self.assertEqual(self.run_cmd(
                f'commit {task["id"]} --repos be --path task.txt -m "feat: 任务分支独立提交"'), 0)
        self.assertTrue(git.dirty(self.repo), "fxh 的用户改动必须原样保留")
        self.assertEqual(git.sha(self.repo, "fxh"), task["repos"]["be"]["base_sha"])
        self.assertEqual(self.run_cmd(f"check {task['id']}"), 0)
        self.fill_handoff(task)
        self.assertEqual(self.run_cmd(f"ready {task['id']}"), 0)
        self.assertTrue(self.reg.load(task["id"])["repos"]["be"]["ready_sha"])


class LandGateNarrowing(Sandbox):
    """落地门禁只拦「主工作区脏文件与本次落地内容相交」，不再逐字节要求干净。

    2026-09-22 实测：`AGENTS.md` 有 9 行用户未提交改动，把与它毫无关系的三个仓库
    一起挡在了落地之外——上面的 `test_task_commit_is_independent_from_dirty_integration_checkout`
    已经在验证 fxh 上的脏文件不影响 commit/check/ready，这里补上此前完全没覆盖的 land 这一步。
    """

    def test_unrelated_dirty_file_does_not_block_land_and_survives(self):
        task = self.new()
        self.commit(task)
        self.ready(task)
        (self.repo / "draft.md").write_text("与本次落地无关的草稿\n")
        self.assertEqual(self.run_cmd(f"land {task['id']}"), 0)
        self.assertEqual(self.reg.load(task["id"])["state"], "landed")
        self.assertEqual((self.repo / "draft.md").read_text(), "与本次落地无关的草稿\n",
                         "无关草稿不该被落地流程碰到")

    def test_dirty_file_clashing_with_landed_content_blocks_and_names_it(self):
        task = self.new()
        self.commit(task)  # 任务分支把 file.txt 改成 "changed\n"
        self.ready(task)
        (self.repo / "file.txt").write_text("fxh 上还没提交的改动\n")
        with self.assertRaises(integrate.Reject) as ctx:
            self.run_cmd(f"land {task['id']}")
        self.assertIn("file.txt", str(ctx.exception))
        self.assertEqual((self.repo / "file.txt").read_text(), "fxh 上还没提交的改动\n",
                         "拒绝落地时不能动用户还没提交的内容")
        # 计划阶段全部仓库被拦，cmd_land 的 `if not plans: raise` 生效，状态与其他
        # 「彻底落不了地」的既有用例（见 test_land_requires_human_confirmation_before_fast_forward）一致。
        self.assertEqual(self.reg.load(task["id"])["state"], "rejected")

    def test_unrelated_untracked_file_mixed_with_dirty_still_lands(self):
        # 先把第二个文件提交进 fxh 基线（任务创建之前），这样任务分支和 fxh 从同一份内容
        # 分叉，之后单纯编辑它属于「本地未提交改动」，不会被误判成「与落地内容分叉」。
        (self.repo / "other.txt").write_text("基线内容\n")
        git.run(["add", "other.txt"], cwd=self.repo)
        git.run(["commit", "-qm", "chore: 基线补充第二个文件"], cwd=self.repo)
        task = self.new()
        self.commit(task)
        self.ready(task)
        (self.repo / "other.txt").write_text("无关的已跟踪改动\n")
        (self.repo / "untracked.md").write_text("从未加入版本库的文件\n")
        self.assertEqual(self.run_cmd(f"land {task['id']}"), 0)
        self.assertEqual((self.repo / "other.txt").read_text(), "无关的已跟踪改动\n")
        self.assertEqual((self.repo / "untracked.md").read_text(), "从未加入版本库的文件\n")

    def test_recheck_still_blocks_new_conflicting_change_after_confirmation(self):
        """§3.4 的第 1 道保险：确认弹窗和真正快进之间，`file.txt` 出现新冲突仍要拦住。

        这条钉住一个纠正：门禁通过后的复检若照抄「工作区有任何脏文件就拒绝」，会把
        plan_landing 刚刚放行的无关草稿在这里重新挡一次，等于门禁收窄白做——必须和
        plan_landing 用同一套「只拦相交」口径，TOCTOU 防护护的是「变了」不是「本来就有」。
        """
        self.enable_policy_land_confirm()
        task = self.new()
        self.commit(task)
        self.ready(task)
        before = git.sha(self.repo, "fxh")

        def confirm_and_conflict(*a, **k):
            (self.repo / "file.txt").write_text("确认期间冒出来的冲突改动\n")
            return True

        # 这个仓库在计划阶段通过了（那时还没脏），复检才被拦——不同于全部仓库计划阶段
        # 就被拒的 test_dirty_file_clashing_with_landed_content_blocks_and_names_it，
        # cmd_land 这里记 blocked 后继续收尾，返回非零而不是抛异常。
        with patch.object(registry, "confirm_human", side_effect=confirm_and_conflict):
            self.assertEqual(self.run_cmd(f"land {task['id']}"), 1)
        self.assertEqual(git.sha(self.repo, "fxh"), before, "复检失败必须原地不动，不能半途快进")
        self.assertEqual((self.repo / "file.txt").read_text(), "确认期间冒出来的冲突改动\n",
                         "拒绝时不能碰用户在确认期间写下的内容")

    def test_recheck_ignores_unrelated_change_appearing_during_confirmation(self):
        self.enable_policy_land_confirm()
        task = self.new()
        self.commit(task)
        self.ready(task)

        def confirm_and_draft(*a, **k):
            (self.repo / "draft.md").write_text("确认期间冒出来的无关草稿\n")
            return True

        with patch.object(registry, "confirm_human", side_effect=confirm_and_draft):
            self.assertEqual(self.run_cmd(f"land {task['id']}"), 0)
        self.assertEqual(self.reg.load(task["id"])["state"], "landed")
        self.assertEqual((self.repo / "draft.md").read_text(), "确认期间冒出来的无关草稿\n")

    def enable_policy_land_confirm(self):
        self.cfg.raw["merge_policy"] = {"confirm_land": True}


class RepoAutonomy(Sandbox):
    """前端、后端、文档各自 ready / land / promote：一个仓库被拦只挡它自己。

    2026-09-18 T021（后端 + 前端 + 文档）因为文档主工作区有别人的未提交改动，三个仓库一起被拒。"""

    def setUp(self):
        super().setUp()
        self.prof["repos"]["frontend"] = str(self.root / "main" / "frontend")
        self.prof["worktrees"]["repos"]["web"] = {"profile_repo": "frontend", "trunk": "dev", "push_branch": "fxh-dev",
                                                  "promote": "ff-trunk", "anchors": ["dev"], "gate": {"kind": "none"}}
        self.cfg = build_config(self.prof, "mac")
        loader = patch.object(wt_config, "load_config", return_value=(self.cfg, "测试档案"))
        loader.start()
        self.addCleanup(loader.stop)
        self.reg = Registry(self.cfg)
        self.web = self.cfg.repo("web").path
        self.web.mkdir(parents=True)
        git.run(["init", "-q", "-b", "fxh"], cwd=self.web)
        (self.web / "app.js").write_text("baseline\n")
        git.run(["add", "."], cwd=self.web)
        git.run(["commit", "-qm", "feat: 初始化"], cwd=self.web)
        git.run(["branch", "dev"], cwd=self.web)
        git.worktree_add_unique(self.web, self.cfg.anchor_path("web", "gate"), "xa-web-gate", "fxh", detach=True)

    def new_both(self):
        self.run_cmd("new both-repos-demo --title 前后端演示任务 --repos be,web --goal 目标 --accept 验收")
        return max(self.reg.all(), key=lambda t: t["id"])

    def commit_in(self, task, alias, name):
        wt = Path(task["repos"][alias]["path"])
        (wt / name).write_text("changed\n")
        git.run(["add", "."], cwd=wt)
        git.run(["commit", "-qm", "feat: 修改内容"], cwd=wt)

    def test_dirty_main_checkout_only_blocks_its_own_repo(self):
        self.cfg.raw["merge_policy"] = {"confirm_land": True}
        t = self.new_both()
        self.commit_in(t, "be", "file.txt")
        self.commit_in(t, "web", "app.js")
        self.ready(t)
        (self.web / "app.js").write_text("别人的未提交改动\n")
        be_before, web_before = git.sha(self.repo, "fxh"), git.sha(self.web, "fxh")
        with patch.object(registry, "confirm_human", return_value=True) as confirm:
            self.assertEqual(self.run_cmd(f"land {t['id']}"), 1)
        self.assertNotEqual(git.sha(self.repo, "fxh"), be_before, "后端不该被前端主工作区挡住")
        self.assertEqual(git.sha(self.web, "fxh"), web_before)
        self.assertIn("web: 本次不落地", confirm.call_args[0][0], "弹窗要写明哪个仓库这次不落地")
        task = self.reg.load(t["id"])
        self.assertEqual(task["state"], "rejected")
        self.assertIn("web", task["last_reject"]["reason"])
        git.run(["checkout", "--", "app.js"], cwd=self.web)
        with patch.object(registry, "confirm_human", return_value=True):
            self.assertEqual(self.run_cmd(f"land {t['id']}"), 0)
        self.assertNotEqual(git.sha(self.web, "fxh"), web_before)
        self.assertEqual(self.reg.load(t["id"])["state"], "landed")

    def test_land_repos_selects_only_named_repos(self):
        t = self.new_both()
        self.commit_in(t, "be", "file.txt")
        self.commit_in(t, "web", "app.js")
        self.ready(t)
        be_before, web_before = git.sha(self.repo, "fxh"), git.sha(self.web, "fxh")
        self.assertEqual(self.run_cmd(f"land {t['id']} --repos web"), 0)
        self.assertNotEqual(git.sha(self.web, "fxh"), web_before)
        self.assertEqual(git.sha(self.repo, "fxh"), be_before)
        self.assertEqual(self.reg.load(t["id"])["state"], "ready", "只落了一部分，任务还没落地完")
        self.assertEqual(self.run_cmd(f"land {t['id']}"), 0)
        self.assertNotEqual(git.sha(self.repo, "fxh"), be_before)
        self.assertEqual(self.reg.load(t["id"])["state"], "landed")
        with self.assertRaises(WtError):
            self.run_cmd(f"land {t['id']} --repos docs")

    def test_ready_failure_in_one_repo_does_not_block_others(self):
        t = self.new_both()
        self.commit_in(t, "be", "file.txt")
        self.commit_in(t, "web", "app.js")
        self.fill_handoff(t)
        self.run_cmd(f"check {t['id']}")
        be_wt = Path(t["repos"]["be"]["path"])
        (be_wt / "file.txt").write_text("还没提交\n")
        self.assertEqual(self.run_cmd(f"ready {t['id']}"), 1)
        task = self.reg.load(t["id"])
        self.assertEqual(task["state"], "ready")
        self.assertTrue(task["repos"]["web"].get("ready_sha"))
        self.assertFalse(task["repos"]["be"].get("ready_sha"))
        be_before, web_before = git.sha(self.repo, "fxh"), git.sha(self.web, "fxh")
        self.assertEqual(self.run_cmd(f"land {t['id']}"), 1)
        self.assertNotEqual(git.sha(self.web, "fxh"), web_before, "前端不该被后端的交付问题挡住")
        self.assertEqual(git.sha(self.repo, "fxh"), be_before)
        git.run(["checkout", "--", "file.txt"], cwd=be_wt)
        self.assertEqual(self.run_cmd(f"ready {t['id']} --repos be"), 0)
        self.assertEqual(self.run_cmd(f"land {t['id']}"), 0)
        self.assertEqual(self.reg.load(t["id"])["state"], "landed")

    def test_repo_without_commits_is_not_blocked_by_dirty_main_checkout(self):
        t = self.new_both()
        self.commit_in(t, "be", "file.txt")
        self.ready(t)
        (self.web / "app.js").write_text("别人的未提交改动\n")
        self.assertEqual(self.run_cmd(f"land {t['id']}"), 0)
        task = self.reg.load(t["id"])
        self.assertEqual(task["state"], "landed")
        self.assertFalse(task["repos"]["web"].get("landed_sha"), "没有新提交的仓库不该登记落地合并（revert 会撤错）")

    def test_push_only_repo_is_not_rejected_for_master_trunk(self):
        self.cfg.raw["merge_policy"] = {"confirm_land": True, "forbid_main_operations": True}
        rc = self.cfg.repo("be")
        rc.trunk, rc.promote = "master", "push-only"
        remote, _anchor = self.remote()
        t = self.new()
        self.commit(t)
        self.ready(t)
        with patch.object(registry, "confirm_human", return_value=True):
            self.run_cmd(f"land {t['id']}")
            self.assertEqual(self.run_cmd("promote --repos be"), 0)
        self.assertEqual(git.sha(remote, "fxh-dev"), git.sha(self.repo, "fxh"))
        self.assertEqual(self.reg.load(t["id"])["state"], "promoted")
        rc.promote = "ff-trunk"
        with self.assertRaises(integrate.Reject):
            integrate.promote_one(self.cfg, self.reg, "be", rc, True)


# ==================================================================== 旧引擎回归（移植）
class LifecycleRegression(Sandbox):
    def test_handoff_can_complete_without_editing_instructions(self):
        t = self.new()
        self.commit(t)
        self.ready(t)
        self.assertEqual(self.reg.load(t["id"])["state"], "ready")

    def test_pause_releases_quota_and_claim_enforces_it(self):
        self.cfg.quota_active = 1
        a = self.new()
        self.run_cmd(f"pause {a['id']}")
        self.new("second-sample-job", "完全不同的另一件事")
        with self.assertRaises(WtError):
            self.run_cmd(f"claim {a['id']} --tool codex --session test-session")

    def test_invalid_refs_rejected(self):
        self.new()
        for ref in ["../T001", "T001/../x", "/tmp/T001", "", "cx/../../escape"]:
            with self.subTest(ref=ref), self.assertRaises(WtError):
                self.reg.find_by_ref(ref)

    def test_dirty_changed_during_check_is_not_certified(self):
        t = self.new()
        self.commit(t)
        self.cfg.repo("be").gate = {"kind": "command", "argv": ["true"]}

        def mutation(*a, **k):
            self.commit(t, "raced\n")
            return True, "passed old content"

        with patch.object(gates, "run_gate", side_effect=mutation):
            self.assertEqual(self.run_cmd(f"check {t['id']}"), 1)
        self.assertFalse(self.reg.load(t["id"])["check"]["results"]["be"]["ok"])

    def test_land_conflict_preserves_integration(self):
        a = self.new()
        b = self.new("second-sample-job", "完全不同的另一件事")
        self.commit(a, "first\n")
        self.commit(b, "second\n")
        self.ready(a)
        self.ready(b)
        self.run_cmd(f"land {a['id']}")
        before = git.sha(self.repo, "HEAD")
        with self.assertRaises(WtError):
            self.run_cmd(f"land {b['id']}")
        self.assertEqual(git.sha(self.repo, "HEAD"), before)
        self.assertEqual(self.reg.load(b["id"])["state"], "rejected")

    def test_gate_failure_never_lands(self):
        t = self.new()
        self.commit(t)
        self.ready(t)
        before = git.sha(self.repo, "HEAD")
        with patch.object(gates, "run_gate", return_value=(False, "expected failure")), self.assertRaises(WtError):
            self.run_cmd(f"land {t['id']}")
        self.assertEqual(git.sha(self.repo, "HEAD"), before)

    def test_archive_preserves_untracked_and_tracked_changes(self):
        t = self.new()
        self.commit(t)
        wt = Path(t["repos"]["be"]["path"])
        (wt / "new.bin").write_bytes(bytes(range(256)))
        (wt / "file.txt").write_text("unsaved\n")
        self.run_cmd(f"archive {t['id']} --abandon")
        self.assertFalse(wt.exists())
        refs = git.out(["for-each-ref", "--format=%(refname)", "refs/aisk/salvage"], cwd=self.repo).splitlines()
        self.assertEqual(len(refs), 1)
        self.assertEqual(git.out(["show", refs[0] + ":file.txt"], cwd=self.repo), "unsaved")
        self.assertTrue(git.sha(self.repo, f"refs/aisk/archive/{t['name']}/be"))

    def test_message_rules(self):
        for msg in ["feat: 无工具名\n第二行", "feat: " + "长" * 140, "feat: Codex 修改", "任意说明",
                    "feat: 普通说明\nCo-authored-by: Person <p@example.test>"]:
            with self.subTest(msg=msg):
                self.assertIsNotNone(model.check_message(self.cfg, msg))
        for msg in ["fix: 修复回链校验", "fix(order): 修复回链校验", "docs(库存管理): 补充字段说明"]:
            self.assertIsNone(model.check_message(self.cfg, msg))

    def test_no_state_regression_after_land(self):
        t = self.new()
        self.commit(t)
        self.ready(t)
        self.run_cmd(f"land {t['id']}")
        with self.assertRaises(WtError):
            self.run_cmd(f"pause {t['id']}")

    def test_dry_run_does_not_change_registry(self):
        t = self.new()
        self.commit(t)
        self.ready(t)
        before = self.reg.task_file(t["id"]).read_bytes()
        self.run_cmd(f"land {t['id']} --dry-run")
        self.assertEqual(self.reg.task_file(t["id"]).read_bytes(), before)

    def test_link_crosswire_blocks_check(self):
        a, b = self.new(), self.new("second-sample-job", "完全不同的另一件事")
        aw, bw = Path(a["repos"]["be"]["path"]), Path(b["repos"]["be"]["path"])
        (aw / ".git").write_bytes((bw / ".git").read_bytes())
        with self.assertRaises(WtError):
            self.run_cmd(f"check {a['id']}")

    def test_untracked_directory_lists_files(self):
        t = self.new()
        wt = Path(t["repos"]["be"]["path"])
        (wt / "module").mkdir()
        (wt / "module" / "file.txt").write_text("new")
        self.assertIn("module/file.txt", git.dirty_paths(wt))

    def test_task_worktree_has_push_protection(self):
        lane = bind.generated_dir() / "lane.gitconfig"
        lane.parent.mkdir(parents=True)
        lane.write_text(bind.lane_gitconfig_text(self.cfg))
        (self.root / "gitconfig").write_text(bind.include_block(self.cfg))
        t = self.new()
        wt = Path(t["repos"]["be"]["path"])
        self.assertEqual(git.config_get(wt, names.GIT_MARK_KEY), "true")
        self.assertIn("aisk-push-disabled", git.config_get(wt, "remote.origin.pushurl"))
        self.assertIsNone(git.config_get(self.repo, names.GIT_MARK_KEY))


class SafetyRegression(Sandbox):
    def test_branch_delivery_policy_allows_personal_and_protects_dev(self):
        self.assertFalse(integrate.push_requires_confirmation(self.cfg, "be", "fxh"))
        self.assertFalse(integrate.push_requires_confirmation(self.cfg, "be", "fxh-dev"))
        self.assertTrue(integrate.push_requires_confirmation(self.cfg, "be", "dev"))
        self.assertTrue(integrate.push_requires_confirmation(self.cfg, "be", "main"))
        self.cfg.raw["merge_policy"] = {"confirm_personal_push": True}
        self.assertTrue(integrate.push_requires_confirmation(self.cfg, "be", "fxh-dev"))

    def test_restore_recovers_abandoned_untracked_files(self):
        t = self.new()
        wt = Path(t["repos"]["be"]["path"])
        (wt / "new.txt").write_text("keep me")
        self.run_cmd(f"archive {t['id']} --abandon")
        self.run_cmd(f"restore {t['id']} --slug restored-demo-task")
        new = max(self.reg.all(), key=lambda x: x["id"])
        self.assertEqual(new["restored_from"], t["id"])
        self.assertEqual((Path(new["repos"]["be"]["path"]) / "new.txt").read_text(), "keep me")

    def test_npm_refreshes_on_lockfile_change(self):
        wt = self.root / "web"
        wt.mkdir()
        (wt / "package.json").write_text("{}")
        lock = wt / "package-lock.json"
        lock.write_text("{}")
        calls = []

        def install(cmd, cwd, env, log):
            calls.append(cmd)
            (wt / "node_modules").mkdir(exist_ok=True)
            return 0

        with patch.object(gates, "run_logged", side_effect=install):
            gates.ensure_node_modules(wt, self.root / "npm.log")
            gates.ensure_node_modules(wt, self.root / "npm.log")
            lock.write_text('{"version": 2}')
            gates.ensure_node_modules(wt, self.root / "npm.log")
        self.assertEqual(len(calls), 2)

    def test_node_modules_symlink_rejected(self):
        wt = self.root / "web"
        wt.mkdir()
        (wt / "node_modules").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(WtError):
            gates.ensure_node_modules(wt, self.root / "npm.log")

    def test_failed_npm_not_cached(self):
        wt = self.root / "web"
        wt.mkdir()
        with patch.object(gates, "run_logged", return_value=1), self.assertRaises(WtError):
            gates.ensure_node_modules(wt, self.root / "npm.log")
        self.assertFalse((wt / "node_modules/.aisk-deps.json").exists())

    def test_precheck_runs_for_matching_paths(self):
        self.cfg.repo("be").gate = {"kind": "maven-modules", "precheck": "scripts/check-migrations.sh",
                                    "precheck_when": ["scripts/database/**"]}
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/check-migrations.sh").write_text("exit 1")
        ok, summary = gates.run_gate(self.cfg, "be", self.repo,
                                     ["scripts/database/flyway/migrations/order/V1__test.sql"], self.root / "sql.log")
        self.assertFalse(ok)
        self.assertIn("预检", summary)

    def test_maven_scope_adds_dependents_for_shared_modules(self):
        for mod in ("common", "order"):
            (self.repo / mod).mkdir()
            (self.repo / mod / "pom.xml").write_text("<project/>")
        (self.repo / "pom.xml").write_text("<project/>")
        self.cfg.repo("be").gate = {"kind": "maven-modules", "also_dependents": ["common/**"]}
        seen = {}

        def fake_run(cmd, cwd, env, log):
            seen["cmd"] = cmd
            return 0

        with patch.object(gates, "run_logged", side_effect=fake_run), patch.object(gates, "java_home_for", return_value=""), \
                patch.object(gates.shutil, "which", return_value="/usr/bin/java"):
            ok, summary = gates.run_gate(self.cfg, "be", self.repo, ["common/src/A.java"], self.root / "mvn.log")
        self.assertTrue(ok)
        self.assertIn("-amd", seen["cmd"])
        self.assertIn("common", seen["cmd"][seen["cmd"].index("-pl") + 1])

    def test_empty_handoff_rejected(self):
        t = self.new()
        (Path(t["dir"]) / "HANDOFF.md").write_text("# done\n")
        self.assertTrue(bind.handoff_incomplete(t["dir"]))

    def test_init_failure_writes_nothing(self):
        git.run(["switch", "-q", "dev"], cwd=self.repo)
        before = (self.repo / ".git/config").read_bytes()
        with self.assertRaises(WtError):
            self.run_cmd("init --apply")
        self.assertEqual((self.repo / ".git/config").read_bytes(), before)
        self.assertFalse(self.cfg.hub.exists())

    def test_stale_worktree_quarantine_preserves_directory(self):
        stale = self.root / "gate"
        stale.mkdir()
        (stale / ".git").write_text("gitdir: missing-admin\n")
        (stale / "unknown.txt").write_text("preserve\n")
        kept = doctor.quarantine_stale_worktree(stale)
        self.assertFalse(stale.exists())
        self.assertEqual((kept / "unknown.txt").read_text(), "preserve\n")
        self.assertTrue(kept.name.startswith("gate.stale-"))

    def test_missing_salvage_source_rejected(self):
        with self.assertRaises(WtError):
            tasks.salvage_dir(self.cfg, self.reg, self.root / "absent", "be", "fxh", "missing")
        self.assertEqual(git.out(["for-each-ref", "--format=%(refname)", "refs/aisk/salvage"], cwd=self.repo), "")

    def test_source_audit_detects_update_ref(self):
        t = self.new()
        tip = self.commit(t)
        integrate.record_refs(self.reg, "be", self.repo, ["fxh"])
        git.run(["update-ref", "refs/heads/fxh", tip], cwd=self.repo)
        self.assertTrue(integrate.audit_branch(self.cfg, self.reg, "be", self.repo, "fxh", False))

    def test_gate_never_discards_unknown_work(self):
        gate = self.cfg.anchor_path("be", "gate")
        (gate / "unexpected.txt").write_text("must retain")
        with self.assertRaises(WtError):
            integrate.gate_prepare(self.cfg, "be", git.sha(self.repo, "fxh"))
        self.assertTrue((gate / "unexpected.txt").exists())

    def test_duplicate_repos_rejected_before_creation(self):
        with self.assertRaises(WtError):
            self.run_cmd("new first-demo-task --title 演示 --repos be,be --goal g --accept a")
        self.assertFalse(git.branch_exists(self.repo, "ai/T001-first-demo-task"))
        self.assertEqual(self.reg.all(), [])

    def test_partial_worktree_creation_is_rolled_back(self):
        old = git.run

        def failure(args, **kwargs):
            if args[:2] == ["worktree", "move"]:
                raise WtError("injected move failure")
            return old(args, **kwargs)

        with patch.object(git, "run", side_effect=failure), self.assertRaises(WtError):
            self.new()
        self.assertFalse(git.branch_exists(self.repo, "ai/T001-first-demo-task"))
        self.assertFalse(any((w.get("branch") or "").startswith("ai/") for w in git.worktree_list(self.repo)))
        self.assertEqual(self.reg.all(include_archived=True), [])

    def test_promote_pushes_only_tested_head(self):
        remote, _anchor = self.remote()
        t = self.new()
        self.commit(t)
        self.ready(t)
        self.run_cmd(f"land {t['id']}")
        tip = git.sha(self.repo, "fxh")
        (self.repo / "file.txt").write_text("fxh 主工作区仍有用户未提交改动\n")
        with patch.object(registry, "confirm_human", return_value=True) as confirm:
            self.assertEqual(self.run_cmd("promote --repos be"), 0)
        # fxh-dev 是个人推送面，默认放行；只有最终写入 dev 才弹一次确认框。
        self.assertEqual(confirm.call_count, 1)
        self.assertTrue(git.dirty(self.repo), "promote 不得吞掉 fxh 主工作区的用户改动")
        self.assertEqual(git.sha(remote, "dev"), tip)
        self.assertEqual(self.reg.load(t["id"])["state"], "promoted")

    def test_promote_passes_real_pre_push_hook(self):
        hook_src = os.environ.get("AISK_TEST_PRE_PUSH_HOOK")
        if not hook_src or not Path(hook_src).exists():
            self.skipTest("未设置 AISK_TEST_PRE_PUSH_HOOK（指向业务仓库真实 pre-push 钩子时才验证）")
        remote, anchor = self.remote()
        hooks_dir = self.root / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "pre-push").write_bytes(Path(hook_src).read_bytes())
        (hooks_dir / "pre-push").chmod(0o755)
        git.run(["config", "core.hooksPath", str(hooks_dir)], cwd=self.repo)
        t = self.new()
        self.commit(t)
        self.ready(t)
        self.run_cmd(f"land {t['id']}")
        tip = git.sha(self.repo, "fxh")
        with patch.object(registry, "confirm_human", return_value=True):
            self.assertEqual(self.run_cmd("promote --repos be"), 0)
        self.assertEqual(git.sha(remote, "dev"), tip)
        self.assertEqual(git.sha(anchor, "HEAD"), tip)
        self.assertEqual(self.reg.load_refs("be").get("dev"), tip)

    def test_promote_push_failure_rolls_back_anchor(self):
        _remote, anchor = self.remote()
        dev_before = git.sha(anchor, "HEAD")
        t = self.new()
        self.commit(t)
        self.ready(t)
        self.run_cmd(f"land {t['id']}")
        tip = git.sha(self.repo, "fxh")
        orig = git.run

        def failing(cmd, *a, **kw):
            if cmd[:3] == ["push", "origin", "dev"]:
                raise WtError("injected push network failure")
            return orig(cmd, *a, **kw)

        with patch.object(registry, "confirm_human", return_value=True), patch.object(git, "run", side_effect=failing):
            self.assertEqual(self.run_cmd("promote --repos be"), 1)
        self.assertEqual(git.sha(anchor, "HEAD"), dev_before)
        self.assertNotEqual(self.reg.load_refs("be").get("dev"), tip)

    def test_scope_guard_resolves_symlink(self):
        t = self.new()
        sd = Path(t["dir"])
        (sd / "outside").symlink_to(self.repo, target_is_directory=True)
        with patch.object(guards, "writable_roots", return_value=[str(sd)]), self.assertRaises(guards.Deny):
            guards.check_edit(self.cfg, str(sd / "outside/file.txt"), str(sd), sd)


class HookRegression(Sandbox):
    def test_native_create_and_remove_preserves_work(self):
        path = hooks.worktree_create({"hook_event_name": "WorktreeCreate", "cwd": str(self.repo),
                                      "name": "feature-demo", "session_id": "s1"}, cfg=self.cfg)
        wt = Path(path)
        (wt / "pending.txt").write_text("in progress")
        hooks.worktree_remove({"hook_event_name": "WorktreeRemove", "worktree_path": path, "session_id": "s1"})
        self.assertTrue((wt / "pending.txt").exists())
        t = self.reg.all()[0]
        self.assertEqual(t["state"], "parked")
        self.assertIsNone(t["owner"])

    def test_hook_refuses_unrelated_repository(self):
        with self.assertRaises(WtError):
            hooks.worktree_create({"cwd": str(self.root), "name": "demo"}, cfg=self.cfg)


# ==================================================================== 任务模型（新）
class TaskModel(Sandbox):
    def test_ids_are_never_reused(self):
        a = self.new()
        self.run_cmd(f"archive {a['id']} --abandon")
        b = self.new("another-demo-task", "另一件事情")
        self.assertEqual((a["id"], b["id"]), ("T001", "T002"))
        self.assertEqual(b["name"], "T002-another-demo-task")
        self.assertEqual(b["repos"]["be"]["branch"], "ai/T002-another-demo-task")

    def test_slug_and_title_are_validated(self):
        for slug in ["single", "Upper-case", "has_underscore-x", "claude-helper", "a-b-c-d-e-f"]:
            with self.subTest(slug=slug), self.assertRaises(WtError):
                model.validate_slug(self.cfg, slug)
        with self.assertRaises(WtError):
            model.validate_title("")
        with self.assertRaises(WtError):
            self.run_cmd("new coupon-claim-lock --title 没有目标 --repos be")

    def test_claim_rules_follow_lease(self):
        t = self.new()
        tid = t["id"]
        self.run_cmd(f"claim {tid} --tool codex --session A")
        self.run_cmd(f"claim {tid} --tool codex --session A")  # 同会话续租
        with self.assertRaises(WtError):
            self.run_cmd(f"claim {tid} --tool codex --session B")  # 同工具另一个会话
        with self.assertRaises(WtError):
            self.run_cmd(f"claim {tid} --tool claude --session C")
        self.age_task(tid, 45)
        with self.assertRaises(WtError):
            self.run_cmd(f"claim {tid} --tool claude --session C")  # 空闲：需要 --takeover
        with self.assertRaises(WtError):
            self.run_cmd(f"claim {tid} --tool claude --session C --takeover")  # 还要理由
        self.run_cmd(f"claim {tid} --tool claude --session C --takeover --reason 对方下线")
        self.assertEqual(self.reg.load(tid)["owner"]["tool"], "claude")
        self.age_task(tid, 180)
        self.run_cmd(f"claim {tid} --tool workbuddy --session test-session")  # 可接手：直接认领
        self.assertEqual(self.reg.load(tid)["owner"]["tool"], "workbuddy")
        progress = (Path(t["dir"]) / "PROGRESS.md").read_text(encoding="utf-8")
        self.assertIn("接手（原执行者 codex，空闲）", progress)
        self.assertIn("原因：对方下线", progress)

    def test_lease_thresholds_come_from_profile(self):
        cfg = build_config(make_profile(self.root, lease={"idle_minutes": 5, "claimable_minutes": 10}), "mac")
        self.assertEqual((cfg.idle_minutes, cfg.claimable_minutes), (5, 10))
        owner = {"tool": "codex"}
        now = time.time()
        self.assertEqual(model.lease_state(owner, now - 60, now, cfg), model.HELD)
        self.assertEqual(model.lease_state(owner, now - 6 * 60, now, cfg), model.IDLE)
        self.assertEqual(model.lease_state(owner, now - 11 * 60, now, cfg), model.CLAIMABLE)
        with self.assertRaises(WtError):
            build_config(make_profile(self.root, lease={"idle_minutes": 10, "claimable_minutes": 5}), "mac")

    def test_duplicate_task_is_blocked_until_reason_given(self):
        self.new("coupon-claim-lock", "优惠券领取加锁")
        with self.assertRaises(WtError) as ctx:
            self.new("coupon-claim-limit", "优惠券领取加锁防重复")
        self.assertIn("T001", str(ctx.exception))
        t = self.new("coupon-claim-limit", "优惠券领取加锁防重复", extra="--new-anyway 拆成两个独立改动")
        self.assertEqual(t["new_anyway"], "拆成两个独立改动")

    def test_find_by_chinese_keyword_and_legacy_board(self):
        t = self.new("coupon-claim-lock", "优惠券领取加锁")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_cmd("find 优惠券"), 0)
        self.assertIn(t["id"], out.getvalue())
        self.assertEqual(self.run_cmd("find 完全无关的词"), 1)

    def test_board_groups_by_who_can_take_it(self):
        a = self.new()
        self.run_cmd(f"claim {a['id']} --tool codex --session A")
        b = self.new("second-sample-job", "完全不同的另一件事")
        self.run_cmd(f"pause {b['id']} --next 继续写测试")
        text = self.cfg.board_file.read_text(encoding="utf-8")
        working = text.split("## 有人在做")[1].split("##")[0]
        paused = text.split("## 暂停可接手")[1].split("##")[0]
        self.assertIn(a["id"], working)
        self.assertIn(b["id"], paused)
        self.assertIn("继续写测试", paused)
        self.assertIn("## 已验证待归档", text)

    def test_pause_snapshots_without_commit_and_keeps_worktree(self):
        t = self.new()
        self.run_cmd(f"claim {t['id']} --tool codex --session A")
        wt = Path(t["repos"]["be"]["path"])
        head = git.sha(wt, "HEAD")
        (wt / "file.txt").write_text("half done\n")
        (wt / "draft.md").write_text("notes\n")
        self.run_cmd(f"pause {t['id']} --tool codex --session A --next 补单测")
        after = self.reg.load(t["id"])
        self.assertEqual(git.sha(wt, "HEAD"), head)
        self.assertEqual((wt / "file.txt").read_text(), "half done\n")
        ref = f"refs/aisk/wip/{t['name']}/be"
        self.assertEqual(git.out(["show", f"{ref}:draft.md"], cwd=self.repo), "notes")
        self.assertIsNone(after["owner"])
        self.assertEqual(after["state"], "parked")
        self.assertEqual(tasks.last_next(after), "补单测")

    def test_foreign_ai_cannot_pause_ready_or_archive_held_task(self):
        t = self.new()
        self.run_cmd(f"claim {t['id']} --tool codex --session A")
        for cmd in (f"pause {t['id']} --tool claude --session C", f"release {t['id']} --tool claude --session C",
                    f"archive {t['id']} --abandon --tool claude --session C"):
            with self.subTest(cmd=cmd), self.assertRaises(WtError):
                self.run_cmd(cmd)
        self.run_cmd(f"release {t['id']}")  # 操作者（识别不到 AI 工具）可以交还
        self.assertIsNone(self.reg.load(t["id"])["owner"])

    def test_adopt_branch_counts_source_commits_and_keeps_source(self):
        holder = self.root / "holder"
        git.run(["worktree", "add", "-q", "-b", "feature-left", str(holder), "fxh"], cwd=self.repo)
        (holder / "file.txt").write_text("left behind\n")
        git.run(["commit", "-qam", "feat: 遗留改动"], cwd=holder)
        git.run(["worktree", "remove", str(holder)], cwd=self.repo)
        source_tip = git.sha(self.repo, "feature-left")
        self.run_cmd("adopt-branch feature-left left-behind-work --repo be --title 接手遗留改动 --goal 目标 --accept 验收")
        t = self.reg.all()[0]
        wt = Path(t["repos"]["be"]["path"])
        self.assertEqual(git.sha(wt, "HEAD"), source_tip)
        self.assertEqual(t["repos"]["be"]["base_sha"], git.sha(self.repo, "fxh"))
        self.ready(t)
        self.assertEqual(self.reg.load(t["id"])["state"], "ready")
        self.assertEqual(git.sha(self.repo, "feature-left"), source_tip)
        with self.assertRaises(WtError):
            self.run_cmd("adopt-branch feature-left again-left-work --repo be --title 再接一次")

    def test_adopt_branch_refuses_checked_out_or_protected_branch(self):
        holder = self.root / "holder"
        git.run(["worktree", "add", "-q", "-b", "held-branch", str(holder), "fxh"], cwd=self.repo)
        with self.assertRaises(WtError):
            self.run_cmd("adopt-branch held-branch held-branch-work --repo be --title 被占用的分支")
        with self.assertRaises(WtError):
            self.run_cmd("adopt-branch dev dev-branch-work --repo be --title 受保护分支")

    def test_ready_requires_goal_and_acceptance(self):
        self.run_cmd("new draft-demo-task --title 草稿任务 --repos be --draft")
        t = self.reg.all()[0]
        self.commit(t)
        self.fill_handoff(t)
        with self.assertRaises(WtError):
            self.run_cmd(f"ready {t['id']}")

    def test_verify_revert_and_archive_flow(self):
        t = self.new()
        self.commit(t)
        self.ready(t)
        self.run_cmd(f"land {t['id']}")
        landed = self.reg.load(t["id"])
        self.assertIsNone(landed["owner"])
        self.run_cmd(f"revert {t['id']}")
        rv = max(self.reg.all(), key=lambda x: x["id"])
        self.assertEqual(rv["reverts"], t["id"])
        self.assertEqual((Path(rv["repos"]["be"]["path"]) / "file.txt").read_text(), "baseline\n")
        self.assertEqual(self.reg.load(t["id"])["state"], "reverting")
        self.run_cmd(f"verify {t['id']} --pass --note 回滚后复验")
        self.run_cmd(f"archive {t['id']}")
        self.assertEqual(self.reg.load(t["id"])["state"], "archived")

    def test_hub_republished_ready_wins(self):
        self.cfg.win_state_dir.mkdir(parents=True)
        local = {"id": "W001", "state": "rejected", "os": "windows", "repos": {"be": {"ready_sha": "a", "landed_sha": "x"}}}
        registry.atomic_json(self.reg.task_file("W001"), local)
        published = {"id": "W001", "state": "ready", "os": "windows", "repos": {"be": {"ready_sha": "b"}}}
        registry.atomic_json(self.cfg.win_state_dir / "W001.json", published)
        got = self.reg.load("W001")
        self.assertEqual(got["repos"]["be"]["ready_sha"], "b")
        self.assertEqual(got["repos"]["be"]["landed_sha"], "x")
        self.assertTrue(got["_from_hub"])

    def test_import_legacy_keeps_paths_and_honours_excludes(self):
        legacy_root = self.root / "old"
        self.cfg.legacy_root = legacy_root
        self.cfg.legacy_exclude = ["*seckill*"]
        slot_dir = legacy_root / "lanes" / "me" / "0914-demo-stock"
        git.worktree_add_unique(self.repo, slot_dir / "be", "xw-me-0914-demo-stock-be", "fxh",
                                branch="ai/me/0914-demo-stock")
        (legacy_root / "state" / "slots").mkdir(parents=True)
        base = git.sha(self.repo, "fxh")
        for sid, task in (("me/0914-demo-stock", "0914-demo-stock"), ("me/0915-seckill", "0915-seckill")):
            rec = {"id": sid, "lane": "me", "task": task, "title": "代发库存口径", "os": "mac",
                   "dir": str(legacy_root / "lanes" / sid), "slot_no": 1,
                   "ports": {"base": 20110, "web": 20110, "gateway": 20111, "services_from": 20112, "services_to": 20119},
                   "repos": {"be": {"branch": f"ai/{sid}", "path": str(legacy_root / "lanes" / sid / "be"),
                                    "admin": f"xw-me-{task}-be", "base_ref": "fxh", "base_sha": base,
                                    "ready_sha": None, "landed_sha": None}},
                   "state": "active", "created_at": "2026-09-14T10:00:00+08:00", "history": []}
            (legacy_root / "state" / "slots" / f"me--{task}.json").write_text(json.dumps(rec, ensure_ascii=False))
        self.run_cmd("import-legacy")
        self.assertEqual(self.reg.all(), [])
        self.run_cmd("import-legacy --apply")
        imported = self.reg.all()
        self.assertEqual(len(imported), 1)
        t = imported[0]
        self.assertEqual(t["legacy_id"], "me/0914-demo-stock")
        self.assertEqual(t["repos"]["be"]["branch"], "ai/me/0914-demo-stock")
        self.assertEqual(t["dir"], str(slot_dir))
        self.assertEqual(t["port_block"], 11)
        self.assertEqual(self.reg.find_by_ref("me/0914-demo-stock")["id"], t["id"])
        tasks.require_local(self.cfg, t)
        self.run_cmd(f"claim me/0914-demo-stock --tool codex --session test-session")
        self.assertIn("旧编号 me/0914-demo-stock", self.cfg.board_file.read_text(encoding="utf-8"))
        self.assertTrue((legacy_root / "state" / "slots" / "me--0915-seckill.json").exists())
        self.run_cmd("import-legacy --apply")
        self.assertEqual(len(self.reg.all()), 1)


class GuardsAndHooks(Sandbox):
    def edit_payload(self, task, session, name="file.txt"):
        return {"tool_name": "Write", "cwd": task["dir"], "session_id": session,
                "tool_input": {"file_path": str(Path(task["repos"]["be"]["path"]) / name)}}

    def test_bash_guard_blocks_dangerous_git_and_operator_commands(self):
        t = self.new()
        sd = Path(t["dir"])
        for cmd in ["git push origin HEAD", "git stash", "git checkout dev", "git -C /tmp/other commit -m x",
                    "git fetch origin", "git config user.name x", "xw promote", "aisk wt bind --apply", "aisk task bind --apply",
                    "aisk --profile demo task promote", "aisk task --profile demo init --apply",
                    f"rm -rf {self.repo}/file.txt", f"echo x > {self.cfg.state_dir}/tasks/T001.json"]:
            with self.subTest(cmd=cmd), self.assertRaises(guards.Deny):
                guards.check_bash(self.cfg, cmd, str(sd), sd)
        for cmd in ["git status", "git log --oneline -3", "git config --get user.name", "xw note T001 --next x",
                    "ls ../"]:
            with self.subTest(cmd=cmd):
                guards.check_bash(self.cfg, cmd, str(sd), sd)

    def test_lease_guard_autoclaims_unowned_and_blocks_other_actor(self):
        t = self.new()
        self.assertIsNone(guards.evaluate(self.edit_payload(t, "s1"), "claude"))
        owner = self.reg.load(t["id"])["owner"]
        self.assertEqual((owner["tool"], owner["sessions"]), ("claude", ["s1"]))
        self.assertIsNone(guards.evaluate(self.edit_payload(t, "s1"), "claude"))
        reason = guards.evaluate(self.edit_payload(t, "s2"), "claude")
        self.assertIn("持有中", reason)
        bash = {"tool_name": "Bash", "cwd": t["dir"], "session_id": "other",
                "tool_input": {"command": "git commit -qm 'feat: 抢改'"}}
        self.assertIn("持有中", guards.evaluate(bash, "codex"))
        reads = {"tool_name": "Bash", "cwd": t["dir"], "tool_input": {"command": "git diff && cat file.txt 2>/dev/null"}}
        self.assertIsNone(guards.evaluate(reads, "codex"))

    def test_guard_output_protocol(self):
        t = self.new()
        payload = {"tool_name": "Bash", "cwd": t["dir"], "tool_input": {"command": "git push"}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(guards.main("codex", io.StringIO(json.dumps(payload))), 0)
        decision = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        outside = {"tool_name": "Bash", "cwd": str(self.root), "tool_input": {"command": "git push"}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            guards.main("codex", io.StringIO(json.dumps(outside)))
        self.assertEqual(out.getvalue(), "")

    def outside_payload(self, command="git push origin master", cwd=None):
        return {"tool_name": "Bash", "cwd": str(cwd or self.root), "session_id": "wb-s1",
                "tool_input": {"command": command}}

    def test_workbuddy_outside_public_push_requires_confirmation(self):
        payload = self.outside_payload()
        with patch.object(guards, "outside_origin_url", return_value="git@github.com:example/demo.git"), \
                patch.object(guards.registry, "confirm_human", return_value=False) as confirm:
            reason = guards.evaluate(payload, "workbuddy")
        self.assertIn("未获操作者确认", reason)
        confirm.assert_called_once()
        self.assertEqual(confirm.call_args.args[1], "确认推送")
        self.assertEqual(confirm.call_args.kwargs["tool"], "workbuddy")
        self.assertEqual(confirm.call_args.kwargs["action"], "git推送")
        self.assertEqual(confirm.call_args.kwargs["repository"], self.root.name)
        self.assertIn("普通 fast-forward 推送", confirm.call_args.args[0])
        self.assertIn("远端旧基线：未获取", confirm.call_args.args[0])

        with patch.object(guards, "outside_origin_url", return_value="git@github.com:example/demo.git"), \
                patch.object(guards.registry, "confirm_human", return_value=True) as confirm:
            self.assertIsNone(guards.evaluate(payload, "workbuddy-ai"))
        self.assertEqual(confirm.call_args.kwargs["tool"], "workbuddy-ai")

    def test_workbuddy_outside_push_reads_remote_from_the_directory_in_the_command(self):
        """载荷 cwd 是会话工作区根（不是仓库）时，远端必须按命令里 cd / git -C 指向的目录判定。

        WorkBuddy 的 PreToolUse 载荷 cwd 恒为会话工作区根，若只用它读 origin 就读不到，
        公开远端判不出来，任务外 push 会被静默放行。"""
        repo = self.root / "repo"
        workspace = self.root / "workspace"
        repo.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)

        def origin(cwd):
            return "https://github.com/example/demo.git" if Path(cwd) == repo else ""

        with patch.object(guards, "outside_origin_url", side_effect=origin), \
                patch.object(guards.registry, "confirm_human", return_value=False) as confirm:
            for command in (f"cd {repo} && git push origin master",
                            f"git -C {repo} push origin master",
                            f'sh -c "cd {repo} && git push origin master"'):
                confirm.reset_mock()
                payload = {"tool_name": "Bash", "cwd": str(workspace), "session_id": "wb-s1",
                           "tool_input": {"command": command}}
                reason = guards.evaluate(payload, "workbuddy")
                self.assertIn("未获操作者确认", reason, command)
                confirm.assert_called_once()
                self.assertEqual(confirm.call_args.kwargs["repository"], "repo", command)
                self.assertEqual(confirm.call_args.args[1], "确认推送")

    def test_workbuddy_outside_private_push_and_read_only_commands_do_not_prompt(self):
        # 个人集成分支（fxh/fxh-dev）是日常开发闭环；是否公开远端不改变放行策略。
        payload = self.outside_payload("git push origin fxh-dev")
        with patch.object(guards, "outside_origin_url", return_value="file:///tmp/demo.git"), \
                patch.object(guards.registry, "confirm_human") as confirm:
            self.assertIsNone(guards.evaluate(payload, "workbuddy"))
            self.assertIsNone(guards.evaluate(self.outside_payload("git status"), "workbuddy"))
        confirm.assert_not_called()

        # 其它端的任务外行为保持原样：这次 WorkBuddy 闸门不能改变 Codex。
        with patch.object(guards.registry, "confirm_human") as confirm:
            self.assertIsNone(guards.evaluate(payload, "codex"))
        confirm.assert_not_called()

    def test_protected_push_requires_confirmation_for_every_supported_tool(self):
        payload = self.outside_payload("git push origin dev")
        for tool in ("codex", "claude", "antigravity", "workbuddy", "workbuddy-ai", "cursor"):
            with self.subTest(tool=tool), patch.object(guards.registry, "confirm_human", return_value=False) as confirm:
                reason = guards.evaluate(payload, tool)
            self.assertIn("未获操作者确认", reason)
            confirm.assert_called_once()
            self.assertEqual(confirm.call_args.kwargs["tool"], tool)
            self.assertEqual(confirm.call_args.kwargs["action"], "git推送")
            self.assertEqual(confirm.call_args.args[1], "确认推送")

    def test_merge_protected_branch_requires_tool_named_confirmation(self):
        payload = self.outside_payload("git merge dev")
        with patch.object(guards.registry, "confirm_human", return_value=False) as confirm:
            reason = guards.evaluate(payload, "claude")
        self.assertIn("未获操作者确认", reason)
        self.assertEqual(confirm.call_args.kwargs["tool"], "claude")
        self.assertEqual(confirm.call_args.kwargs["action"], "git合并")
        self.assertEqual(confirm.call_args.args[1], "确认合并")

        with patch.object(guards.registry, "confirm_human") as confirm:
            self.assertIsNone(guards.evaluate(self.outside_payload("git merge feature/demo"), "claude"))
        confirm.assert_not_called()

    def test_workbuddy_outside_custom_forbidden_command_prompts(self):
        self.cfg.forbidden_commands = {r"danger-token": "配置声明的高危命令"}
        payload = self.outside_payload("echo danger-token")
        with patch.object(guards.registry, "confirm_human", return_value=False) as confirm:
            reason = guards.evaluate(payload, "workbuddy")
        self.assertIn("配置声明的高危命令", reason)
        self.assertEqual(confirm.call_args.args[1], "确认")
        self.assertEqual(confirm.call_args.kwargs["action"], "高危动作")

    def test_session_hooks_claim_heartbeat_and_release(self):
        t = self.new()
        start = json.loads(hooks.run_claude({"hook_event_name": "SessionStart", "cwd": t["dir"], "session_id": "s9"}))
        self.assertIn("已为本会话认领", start["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.reg.load(t["id"])["owner"]["sessions"], ["s9"])
        other = json.loads(hooks.run_claude({"hook_event_name": "SessionStart", "cwd": t["dir"], "session_id": "s10"}))
        self.assertIn("只读", other["hookSpecificOutput"]["additionalContext"])
        self.age_task(t["id"], 20)
        hooks.run_claude({"hook_event_name": "Stop", "cwd": t["dir"], "session_id": "s9"})
        beat = registry.parse_iso(self.reg.load(t["id"])["owner"]["heartbeat_at"]).timestamp()
        self.assertLess(time.time() - beat, 60)
        hooks.run_claude({"hook_event_name": "SessionEnd", "cwd": t["dir"], "session_id": "s10", "reason": "exit"})
        self.assertIsNotNone(self.reg.load(t["id"])["owner"])  # 别的会话结束不影响持有者
        hooks.run_claude({"hook_event_name": "SessionEnd", "cwd": t["dir"], "session_id": "s9", "reason": "exit"})
        self.assertIsNone(self.reg.load(t["id"])["owner"])
        self.assertIn("会话结束", (Path(t["dir"]) / "PROGRESS.md").read_text(encoding="utf-8"))

    def test_actor_sessions_from_env_and_payload(self):
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "abc"}
        self.assertEqual(actor.detect_tool(env), "claude")
        self.assertEqual(actor.session_ids("claude", payload={"session_id": "abc"}, env=env), ["abc"])
        self.assertTrue(model.same_actor({"tool": "claude", "sessions": ["abc"]}, "claude", ["abc", "x"]))
        self.assertFalse(model.same_actor({"tool": "claude", "sessions": ["abc"]}, "claude", ["def"]))
        self.assertFalse(model.same_actor({"tool": "claude", "sessions": ["abc"]}, "codex", ["abc"]))
        for env in (
            {"CODEBUDDY_APP": "workbuddy-ai"},
            {"WORKBUDDY_DATA_FOLDER_NAME": ".workbuddy-ai"},
            {"WORKBUDDY_CONFIG_DIR": "/tmp/.workbuddy-ai"},
        ):
            env["CODEBUDDY_SHELL"] = "1"
            self.assertEqual(actor.detect_tool(env), "workbuddy-ai")
        self.assertEqual(actor.detect_tool({"CODEBUDDY_SHELL": "1"}), "workbuddy")
        self.assertEqual(model.slug_from_name("Bright Sparrow", self.cfg.ai_re), "bright-sparrow")
        self.assertEqual(model.slug_from_name("claude", self.cfg.ai_re), "task-work")
        self.assertEqual(model.slug_from_name("42"), "task-42")

    def test_workbuddy_hook_tool_is_resolved_at_runtime(self):
        """WorkBuddy 桌面版与 AI 版共读同一份项目级 settings，一份文件只能写一个工具名。
        所以 bind 传哨兵 `auto`，由 guard / hook 在运行期解析——写死任一个都会让
        另一端的会话事件与守卫以错误名义记账。本用例同时钉住其他端不受影响。"""
        ai_env = {"CODEBUDDY_SHELL": "1", "WORKBUDDY_CONFIG_DIR": "/tmp/.workbuddy-ai"}
        desk_env = {"CODEBUDDY_SHELL": "1", "WORKBUDDY_CONFIG_DIR": "/tmp/.workbuddy"}
        self.assertEqual(actor.resolve_tool(actor.AUTO_TOOL, env=ai_env), "workbuddy-ai")
        self.assertEqual(actor.resolve_tool(actor.AUTO_TOOL, env=desk_env), "workbuddy")
        # 解析不出端时回落桌面版 = 改造前写死 `workbuddy` 的行为，最坏不回退
        self.assertEqual(actor.resolve_tool(actor.AUTO_TOOL, env={}), "workbuddy")
        # 其余端原样透传：codex / antigravity / claude / cursor / human 行为不变
        for tool in ("claude", "codex", "antigravity", "cursor", "human"):
            self.assertEqual(actor.resolve_tool(tool, env={}), tool)
        # 生成物：WorkBuddy 两端写哨兵，codex 仍写死自己
        task_dir = self.root / "T001"
        for settings in (bind.codebuddy_settings(self.cfg, task_dir),
                         bind.workbuddy_ai_settings(self.cfg, task_dir)):
            self.assertIn(f"--tool {actor.AUTO_TOOL}", json.dumps(settings, ensure_ascii=False))
        self.assertIn("--tool codex", json.dumps(bind.codex_hooks(self.cfg, task_dir), ensure_ascii=False))


class BindAndCli(Sandbox):
    # 2026-09-23：Windows 弹窗后端已从 PowerShell WinForms 换成原生 TaskDialog
    # （ctypes 调 comctl32），旧的 WinForms 用例测的机制已经从 registry.confirm_human
    # 里删掉（合入 Windows 原生确认弹窗支持时确认过：新代码路径下旧分支永不可达，
    # 是死代码）。等价覆盖在 test_action_context.py 的
    # test_windows_task_dialog_names_the_action_and_defaults_to_cancel 里，走的是
    # 真实生效的 ctypes 路径，不需要在这里重复一份测已删代码的用例。

    def test_windows_publish_is_confirmed_and_never_force_pushes(self):
        self.prof["worktrees"]["hub"] = str(self.root / "hub")
        cfg = build_config(self.prof, "windows")
        task = {"id": "W009", "repos": {"be": {"branch": "tasks/W009-demo", "ready_sha": "a" * 40}}}
        with patch.object(tasks, "_hub_remote_url", return_value="file:///central/hub.git"), \
             patch.object(tasks.git, "sha", return_value="a" * 40), \
             patch.object(tasks.git, "out", side_effect=["", "a" * 40 + "\trefs/heads/tasks/W009-demo"]), \
             patch.object(tasks.git, "run") as run, \
             patch.object(registry, "confirm_human", return_value=True) as confirm, \
             patch.object(tasks, "atomic_json"):
            tasks.publish_to_hub(cfg, task, tool="workbuddy", sessions=["s-009"])
        pushed = run.call_args.args[0]
        self.assertEqual(pushed[0], "push")
        self.assertNotIn("--force", pushed)
        self.assertEqual(confirm.call_args.kwargs["context"].title, "git推送-workbuddy | W009 | be")

    def test_bind_plan_is_idempotent_and_replaces_legacy_blocks(self):
        home = self.root / "home"
        (home / ".claude").mkdir()
        (home / ".claude" / "CLAUDE.md").write_text(
            "# 用户规则\n\n<!-- legacy-worktrees:rules begin -->\n旧规则\n<!-- legacy-worktrees:rules end -->\n")
        (self.root / "gitconfig").write_text(
            "[user]\n\tname = x\n# >>> legacy-worktrees (xw) >>>\n[includeIf \"gitdir:/x/\"]\n\tpath = /x\n"
            "# <<< legacy-worktrees (xw) <<<\n")
        written = bind.apply_plan(bind.plan(self.cfg))
        self.assertTrue(written)
        self.assertEqual([it for it in bind.plan(self.cfg) if it[2] != it[3]], [])
        rules = (home / ".claude" / "CLAUDE.md").read_text()
        self.assertNotIn("legacy-worktrees:rules", rules)
        self.assertIn(bind.MD_BEGIN, rules)
        gcfg = (self.root / "gitconfig").read_text()
        self.assertNotIn("legacy-worktrees (xw)", gcfg)
        self.assertTrue(gcfg.rstrip().endswith(bind.GIT_END))
        local = json.loads((self.repo / ".claude" / "settings.local.json").read_text())
        self.assertIn("WorktreeCreate", local["hooks"])

    def test_snapshot_excludes_every_generated_tool_dir(self):
        """任务目录里的生成目录必须全部被快照排除。

        `bind.write_task_files()` 会往任务目录写 `.claude/`、`.codex/`、`.codebuddy/`、
        `.workbuddy-ai/`、`.agents/`、`.aisk/` 等生成物，而 `snapshot_worktree()` 把
        `SNAPSHOT_EXCLUDES` 当作 `core.excludesFile`。常规 `pause` 的快照根是
        `td/<alias>`，任务目录不在其中；但 `salvage --dir <任务目录>` 与旧 v1 布局
        会让它成为快照根——那时这些含内核绝对路径的生成物就会被提交进
        refs/aisk/wip。所以这里按**实际生成的目录**反查，而不是维护第二份写死清单。
        桌面版 `.workbuddy/` 排了、AI 版 `.workbuddy-ai/` 曾漏掉；`.aisk/` 也曾漏掉。
        """
        t = self.new()
        td = Path(t["dir"])
        generated = sorted(p.name for p in td.iterdir() if p.is_dir() and p.name.startswith("."))
        # 先确认 bind 真的写了生成目录，否则下面的循环会「零项通过」变成假绿
        for expected in (".aisk", ".codebuddy", ".claude", ".codex", ".agents", ".workbuddy-ai"):
            self.assertIn(expected, generated)
        for name in generated:
            with self.subTest(dir=name):
                self.assertTrue(name in tasks.SNAPSHOT_EXCLUDES or f"{name}/" in tasks.SNAPSHOT_EXCLUDES,
                                f"{name} 未被 SNAPSHOT_EXCLUDES 覆盖，会被快照进 refs/aisk/wip")

    def test_apply_plan_writes_through_symlink_and_backs_up(self):
        real = self.root / "dotfiles" / "CLAUDE.md"
        real.parent.mkdir()
        real.write_text("old\n")
        link = self.root / "home" / "linked.md"
        link.symlink_to(real)
        bind.apply_plan([("测试项", link, "old\n", "new\n")])
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "new\n")
        backups = list((profile_mod.PROFILE_DIR.parent / "backups" / "worktree").rglob("*linked.md"))
        self.assertEqual(len(backups), 1)

    def test_config_requires_section_and_windows_hub(self):
        with self.assertRaises(WtError):
            build_config({"project": "x", "repos": {}})
        with self.assertRaises(WtError):
            build_config(make_profile(self.root), "windows")
        win = build_config(make_profile(self.root, hub="//Mac/Home/work/demo/hub"), "windows")
        self.assertEqual(win.task_prefix, "W")
        self.assertEqual(win.repo("be").path, self.root / "data" / "repos" / "be.git")

    def test_aisk_routes_task_subcommands(self):
        pdir = profile_mod.PROFILE_DIR
        pdir.mkdir(parents=True)
        (pdir / "demo.yaml").write_text(
            "project: demo\n"
            "repos:\n"
            f"  backend: {self.repo}\n"
            "worktrees:\n"
            f"  data_root: {self.root / 'data'}\n"
            "  integration_branch: fxh\n"
            "  protected:\n    - main\n    - dev\n    - fxh\n"
            "  lease: {idle_minutes: 15, claimable_minutes: 60}\n"
            "  repos:\n"
            "    be:\n"
            "      profile_repo: backend\n      trunk: dev\n      push_branch: fxh-dev\n      promote: ff-trunk\n"
            "      anchors:\n        - dev\n      gate: {kind: none}\n", encoding="utf-8")
        self.assertEqual(aisk_cli.main(["--profile", "demo", "task", "status"]), 0)
        board = self.cfg.board_file.read_text(encoding="utf-8")
        self.assertIn("无动静 15 分钟显示空闲、60 分钟可接手", board)
        self.assertEqual(aisk_cli.main(["--profile", "demo", "task", "claim", "T404", "--tool", "codex", "--session", "s"]), 1)

    def test_repo_lookup_uses_slot_profile_mapping_not_hardcoded_aliases(self):
        t = self.new()
        slot = json.loads((Path(t["dir"]) / names.TASK_META).read_text())
        wt = t["repos"]["be"]["path"]
        self.assertEqual(slot["profile_repos"], {"backend": wt})
        found = aisk_cli._slot_repo_path("backend", None, Path(t["dir"]), slot["repos"], slot["profile_repos"])
        self.assertEqual(found, str(Path(wt).resolve()))
        self.assertIsNone(aisk_cli._slot_repo_path("frontend", None, Path(t["dir"]), {"web": wt}, {}))

    def test_old_slot_meta_maps_profile_keys_through_profile_declaration(self):
        slot = self.root / "old-slot"
        (slot / "be" / ".git").mkdir(parents=True)
        meta = {"id": "me/0914-demo", "repos": {"be": str(slot / "be")}}
        mapping = aisk_cli._slot_profile_repos(self.prof, meta)
        self.assertEqual(mapping, {"backend": str(slot / "be")})
        self.assertEqual(aisk_cli._slot_repo_path("backend", None, slot, meta["repos"], mapping),
                         str((slot / "be").resolve()))
        self.assertEqual(aisk_cli._slot_profile_repos({"project": "x"}, meta), {})

    def test_doctor_reports_and_continues(self):
        (self.cfg.data_root / ".git").mkdir(parents=True)
        (self.cfg.tasks_dir / "hand-made").mkdir(parents=True)
        self.new()
        self.assertEqual(self.run_cmd("doctor"), 1)
        board = self.cfg.board_file.read_text(encoding="utf-8")
        self.assertIn("未登记目录", board)
        self.assertIn("数据根是 git 仓库", board)

    def test_profile_matches_legacy_config_key_by_key(self):
        prof = make_profile(self.root, push_block_prefix="http://git.example.test/", commit={
            "pattern": legacy.LEGACY_MESSAGE_PATTERN}, sensitive_paths=["pom.xml"], maven_args="-Dx=1",
            quotas={"active": 8, "builds": 2}, guard_protected=["~/old-ai"])
        prof["worktrees"]["repos"]["be"].update(hooks_required=True, gate={"kind": "maven-modules", "jdk": 17})
        cfg = build_config(prof, "mac")
        old = {"integration_branch": "fxh", "protected_branches": ["fxh", "dev", "main"],
               "gitea_prefix": "http://git.example.test/", "max_msg_chars": 140,
               "ai_name_regex": wt_config.DEFAULT_AI_NAMES, "ai_email_regex": wt_config.DEFAULT_AI_EMAILS,
               "roots": {"mac": {"java17_home_cmd": ["/usr/libexec/java_home", "-v", "17"]}},
               "repos": {"be": {"dir": "backend", "push_branch": "fxh-dev", "trunk": "dev", "anchors": ["dev", "main"],
                                "promote": "ff-trunk", "gate": "maven", "hooks_required": True}},
               "repo_order": ["be"], "quotas": {"mac": {"active_slots": 8, "maven": 2}},
               "ports": {"base": 20000, "per_slot": 10}, "sensitive_paths": ["pom.xml"],
               "maven_env": {"MAVEN_ARGS": "-Dx=1"}, "legacy_dirs": ["~/old-ai"]}
        self.assertEqual(legacy.compare_config(cfg, old), [])
        literal = build_config(dict(prof, worktrees=dict(prof["worktrees"], commit={
            "pattern": "^[a-z]+(?:\\([^)]+\\))?: .*[\u4e00-\u9fff]"})), "mac")
        self.assertEqual(legacy.compare_config(literal, old), [])  # 档案解析器把 \uXXXX 转成字面字符后仍视为一致
        old["repos"]["be"]["anchors"] = ["dev", "main", "master"]
        old["legacy_dirs"].append("~/older-ai")
        diffs = legacy.compare_config(cfg, old)
        self.assertEqual(len(diffs), 2)
        self.assertTrue(any("anchors" in d for d in diffs))

    def test_extra_guards_resolve_placeholders_and_skip_missing_files(self):
        hook = self.repo / ".claude" / "hooks" / "prod-db-guard.py"
        hook.parent.mkdir(parents=True)
        hook.write_text("pass\n")
        self.cfg.extra_guards = ["{python} {repo:be}/.claude/hooks/prod-db-guard.py",
                                 "{python} {repo:be}/.claude/hooks/absent.py"]
        cmds = bind.extra_guard_commands(self.cfg)
        self.assertEqual(len(cmds), 1)
        self.assertIn(str(hook), cmds[0])
        self.assertIn(bind.stable_python(), cmds[0])
        t = self.new()
        settings = json.loads((Path(t["dir"]) / ".claude" / "settings.json").read_text())
        bash = settings["hooks"]["PreToolUse"][0]["hooks"]
        self.assertEqual(len(bash), 2)
        self.assertIn("prod-db-guard.py", bash[0]["command"])

    def test_worktree_sources_pass_hygiene(self):
        sys.path.insert(0, str(KERNEL / "tools"))
        import check_kernel_hygiene as hygiene
        for root in hygiene.DEFAULT_ROOTS[1:]:
            with self.subTest(root=root.name):
                self.assertEqual(hygiene.scan(root), [])


if __name__ == "__main__":
    unittest.main()
