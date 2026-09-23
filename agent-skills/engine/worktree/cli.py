# -*- coding: utf-8 -*-
"""`aisk task` 命令行。"""
from __future__ import annotations

import argparse
import contextlib
import sys

from .. import profile as profile_mod
from ..miniyaml import YamlSubsetError
from . import autoflow, direct_tasks, doctor, guards, hooks, integrate, legacy, publish_scheduler, tasks
from .config import WtError, load_config
from .registry import Registry, file_lock, say

USAGE = """aisk task：多 AI 并行任务工作区。说明见 agent-skills 的 WORKTREE.md 与技能 ai-worktree。

找活与认领：
  aisk task find <关键词>            aisk task status              aisk task claim <任务> --tool <工具>
  aisk task note <任务> --done … --next …                  aisk task pause <任务> --next …
开任务：
  aisk task new <英文短语> --title <中文标题> --repos backend,frontend --goal … --accept …
  aisk task adopt-branch <分支> --repo backend <英文短语> --title …（把主工作区遗留分支接成任务）
交付：
  aisk task commit <任务> -m "type: 说明" [--path 路径]  →  aisk task check <任务>  →  填 HANDOFF.md  →  aisk task ready <任务> [--repos …]  →  aisk task land <任务> [--repos …] [--dry-run]
  （前端、后端、文档各自 ready/land/promote：一个仓库被拦不影响其他仓库）
  操作者：
  aisk task promote [--repos backend,frontend] [--dry-run]    aisk task verify <任务> --pass|--fail    aisk task archive <任务>
  direct 仓库：aisk task direct-new <短语> --repo docs --scope <路径> ...；完成用 direct-finish，续做用 direct-resume
  前后端独立发布控制：personal-push、merge-dev、push-dev；push_pending 由 publish-scheduler 每分钟扫描
  aisk task init [--apply]    aisk task bind [--apply]    aisk task doctor [--fix] [--ack-refs]    aisk task sync    aisk task import-legacy
任务写法：T042、T042-coupon-claim-lock，或旧槽位号 me/0915-xxx
"""

TASK_LOCKED = {"check", "commit", "ready", "restack", "pause", "archive", "land", "merge-commit", "revert", "verify",
               "direct-resume", "direct-finish", "direct-abort", "finish"}


def add_tool(p):
    p.add_argument("--tool", choices=("claude", "codex", "antigravity", "workbuddy", "workbuddy-ai", "cursor", "human"))
    p.add_argument("--session", help="会话号（钩子与部分工具会自动识别）")


def build_parser():
    ap = argparse.ArgumentParser(prog="aisk task", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="显式指定档案")
    sub = ap.add_subparsers(dest="cmd")

    def cmd(name, fn, help_text, task=False, tool=False):
        p = sub.add_parser(name, help=help_text)
        if task:
            p.add_argument("task")
        if tool:
            add_tool(p)
        p.set_defaults(func=fn)
        return p

    p = cmd("init", doctor.cmd_init, "建锚点、门禁区与 hub（默认预览）")
    p.add_argument("--apply", action="store_true")
    p = cmd("bind", doctor.cmd_bind, "比对/写入机器级配置（includeIf、规则块、Codex 配置档、仓库本地钩子）")
    p.add_argument("--apply", action="store_true")
    p = cmd("doctor", doctor.cmd_doctor, "巡检（只报告不中断）")
    p.add_argument("--fix", action="store_true", help="恢复档案 pinned_git_config 声明的仓库配置；清理目录已不存在的失效 worktree 登记")
    p.add_argument("--ack-refs", action="store_true", help="人工核对后重新登记受保护分支位置")
    p = cmd("import-legacy", legacy.cmd_import_legacy, "导入旧引擎槽位为任务（默认预览）")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rewrite-files", action="store_true", help="按新模板重写任务须知（保留 TASK/PROGRESS/HANDOFF）")
    p.add_argument("--exclude", action="append", default=[], help="排除的旧槽位号，支持通配（可多次）")

    p = cmd("new", tasks.cmd_new, "开任务", tool=True)
    p.add_argument("slug", help="英文短语，2–5 个小写单词，如 coupon-claim-lock")
    p.add_argument("--title", required=True, help="中文标题（≤30 字）")
    p.add_argument("--repos")
    p.add_argument("--goal", default="")
    p.add_argument("--accept", default="")
    p.add_argument("--draft", action="store_true", help="目标与验收稍后在 TASK.md 补（ready 前必须补齐）")
    p.add_argument("--base", help="叠放在另一个任务之上")
    p.add_argument("--scope", action="append", help="声明改动范围（可多次）")
    p.add_argument("--from-ref", action="append", help="<仓库别名>=<引用>，从指定提交开始（抢救/恢复用）")
    p.add_argument("--new-anyway", default="", help="与已有任务相似但确需另开时写理由")
    p.add_argument("--print-path", action="store_true")

    p = cmd("direct-new", direct_tasks.cmd_new, "在配置为 direct 的普通检出上取得单写者租约并登记任务", tool=True)
    p.add_argument("slug", help="英文任务短语")
    p.add_argument("--title", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--accept", required=True)
    p.add_argument("--scope", action="append", required=True, help="允许改动的仓库相对路径，可重复")
    p = cmd("direct-resume", direct_tasks.cmd_resume, "续做 direct checkout 任务并核对基线与仓库租约", task=True, tool=True)
    p.add_argument("--takeover", action="store_true", help="接手空闲 direct 任务")
    p.add_argument("--reason", default="")
    p = cmd("direct-finish", direct_tasks.cmd_finish, "校验 direct checkout 范围、提交门禁并按精确策略发布", task=True, tool=True)
    p.add_argument("-m", "--message", required=True)
    cmd("direct-abort", direct_tasks.cmd_abort, "仅在干净基线下放弃 direct checkout 任务", task=True)

    p = cmd("find", tasks.cmd_find, "按关键词找任务（开新任务前必做）")
    p.add_argument("query", nargs="*")
    p.add_argument("--all", action="store_true", help="含已归档")
    cmd("context", tasks.cmd_context, "读取单个任务目标、验收与最近进度（接力时优先使用）", task=True)
    p = cmd("claim", tasks.cmd_claim, "认领任务", task=True, tool=True)
    p.add_argument("--takeover", action="store_true", help="接手空闲任务")
    p.add_argument("--reason", default="")
    p = cmd("resume", tasks.cmd_resume, "续做（等同 claim）", task=True, tool=True)
    p = cmd("release", tasks.cmd_release, "交还认领", task=True, tool=True)
    p.add_argument("--note", default="")
    cmd("heartbeat", tasks.cmd_heartbeat, "续心跳", task=True, tool=True)
    p = cmd("add-repo", tasks.cmd_add_repo, "给进行中的任务补挂一个仓库（开工后才发现要跨仓改动）", task=True, tool=True)
    p.add_argument("alias", help="档案 worktrees.repos 里的仓库别名")
    p = cmd("note", tasks.cmd_note, "记进度", task=True, tool=True)
    p.add_argument("text", nargs="*")
    p.add_argument("--done")
    p.add_argument("--next")
    p.add_argument("--blocked")
    p = cmd("pause", tasks.cmd_pause, "暂停并交还（未提交改动自动快照，工作区保留）", task=True, tool=True)
    p.add_argument("--next", default="")
    cmd("harvest", tasks.cmd_harvest, "把任务目录里新增的 WorkBuddy 记忆收割回档案声明的仓库", task=True)

    for name in ("status", "board"):
        p = cmd(name, tasks.cmd_status, "看板（按谁能接分组）")
        p.add_argument("--json", action="store_true")
        p.add_argument("--all", action="store_true")
        p.add_argument("--brief", action="store_true", help="仅输出登记摘要，不扫描仓库（适合快速查看）")
    p = sub.add_parser("open", help="打印在某工具里打开任务的方式")
    p.add_argument("task")
    # 与 add_tool() 的取值域保持一致：漏掉 workbuddy-ai 会让 AI 端无法用 open 拿到指引，
    # 而 claim/note/check 等子命令都能带 --tool workbuddy-ai。
    p.add_argument("--tool", choices=("claude", "codex", "antigravity", "workbuddy", "workbuddy-ai", "cursor", "human"))
    p.set_defaults(func=tasks.cmd_open)
    cmd("env", tasks.cmd_env, "打印任务环境变量文件路径", task=True)
    cmd("overlap", tasks.cmd_overlap, "进行中任务的改动重叠预警")
    cmd("check", tasks.cmd_check, "自检构建", task=True, tool=True)
    p = cmd("commit", tasks.cmd_commit, "只提交当前任务分支，不检查或修改 fxh，不推送远端", task=True, tool=True)
    p.add_argument("--path", dest="paths", action="append", default=[], help="任务仓库内的相对路径（可重复）；不写时必须使用 --all")
    p.add_argument("--repos", help="只提交这些仓库（逗号分隔）；省略=全部")
    p.add_argument("--all", action="store_true", help="提交当前任务仓库的全部改动")
    p.add_argument("-m", "--message", required=True, help="符合仓库规则的提交说明")
    p = cmd("ready", tasks.cmd_ready, "交付就绪（登记 ready 提交；各仓库各自判定）", task=True, tool=True)
    p.add_argument("--repos", help="只交付这些仓库（逗号分隔）；省略=全部，没过的仓库不拦其他仓库")
    p = cmd("finish", autoflow.cmd_finish, "前后端单仓自动提交、门禁、落地 fxh、发布 fxh-dev 并回收 worktree", task=True, tool=True)
    p.add_argument("--repo", required=True, help="只能指定本任务的一个代码仓库")
    p.add_argument("--path", dest="paths", action="append", default=[], help="本次明确提交的仓库内路径，可重复")
    p.add_argument("-m", "--message", required=True)
    cmd("restack", tasks.cmd_restack, "rebase 到最新基线", task=True, tool=True)
    p = cmd("land", integrate.cmd_land, "落地到本地集成分支（门禁验证确切的合并提交；各仓库各自判定）", task=True, tool=True)
    p.add_argument("--repos", help="只落这些仓库（逗号分隔）；省略=全部，被拦的仓库不拦其他仓库")
    p.add_argument("--dry-run", action="store_true")
    p = cmd("promote", integrate.cmd_promote, "集成分支 → 推送分支 → 主干（推送前操作者确认）", tool=True)
    p.add_argument("--repos")
    p.add_argument("--task-id", dest="task_id", default="", help="可选：为推送弹窗补充任务号")
    p.add_argument("--dry-run", action="store_true")
    for name, fn, label in (
        ("personal-push", _personal_push, "只推 fxh → fxh-dev，不执行 dev 操作"),
        ("merge-dev", _merge_dev, "用户命令触发：确认后只快进本地 dev"),
        ("push-dev", _push_dev, "用户命令触发：确认后只推本地 dev 到 origin/dev"),
    ):
        p = cmd(name, fn, label)
        p.add_argument("--repo", required=True, choices=("be", "web"))
        p.add_argument("--task-id", default="", help="将确认记录绑定到任务号")
        p.add_argument("--dry-run", action="store_true")

    cmd("publish-due", publish_scheduler.cmd_publish_due,
        "运行一次到期的 fxh-dev 重试和升级通知（LaunchAgent 调用）")
    p = cmd("publish-scheduler", publish_scheduler.cmd_scheduler, "安装/检查/卸载自动发布重试调度器")
    p.add_argument("scheduler_action", choices=("install", "status", "remove"))
    cmd("sync", integrate.cmd_sync, "加锁抓取远端与 hub")
    p = cmd("verify", integrate.cmd_verify, "登记共享环境验证结论", task=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pass", dest="passed", action="store_true")
    g.add_argument("--fail", dest="passed", action="store_false")
    p.add_argument("--note", default="")
    cmd("revert", integrate.cmd_revert, "为已落地任务生成回滚任务", task=True, tool=True)
    p = cmd("gc", tasks.cmd_gc, "回收任务目录：归档过期任务、清构建产物（默认预览）")
    p.add_argument("--apply", action="store_true", help="真正执行")
    p.add_argument("--build-artifacts", action="store_true", help="同时清 node_modules/target 这类构建产物")
    p.add_argument("--include-live", action="store_true", help="进行中的任务也清构建产物（会打断正在跑的构建）")
    p = cmd("archive", tasks.cmd_archive, "归档（分支存 refs/aisk/archive）", task=True, tool=True)
    p.add_argument("--force", action="store_true", help="已落地/已上主干但不做验证")
    p.add_argument("--abandon", action="store_true", help="放弃：未提交改动先自动快照")
    p = cmd("restore", tasks.cmd_restore, "从归档恢复为新任务", task=True, tool=True)
    p.add_argument("--slug")
    p.add_argument("--title")
    p = cmd("salvage", tasks.cmd_salvage, "把任意目录快照成 refs/aisk/salvage/… 提交（不写该目录）")
    p.add_argument("dir")
    p.add_argument("--repo", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--date")
    p = cmd("merge-commit", tasks.cmd_merge_commit, "仓库钩子在 worktree 里误判合并时按规则补落合并提交", task=True)
    p.add_argument("--repo")
    p.add_argument("-m", "--message")
    p = cmd("adopt", tasks.cmd_adopt, "收编工具原生建出的 worktree（会话已结束）")
    p.add_argument("path")
    p.add_argument("slug")
    p.add_argument("--repo", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--goal", default="")
    p.add_argument("--accept", default="")
    p = cmd("adopt-branch", tasks.cmd_adopt_branch, "把现有分支接手成任务（原分支保留）", tool=True)
    p.add_argument("branch")
    p.add_argument("slug")
    p.add_argument("--repo", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--goal", default="")
    p.add_argument("--accept", default="")
    p.add_argument("--new-anyway", default="")

    p = sub.add_parser("guard", help="PreToolUse 守卫（钩子调用，stdin 为 JSON）")
    p.add_argument("--tool", required=True)
    p.add_argument("--task-root", help="该钩子所属的任务目录；工具上报的 cwd 指到任务外时用它兜底")
    p = sub.add_parser("hook", help="工具钩子适配（钩子调用，stdin 为 JSON）")
    p.add_argument("tool")
    p.add_argument("--event", help="钩子输入里不带事件名的工具（Antigravity）由这里指定")
    return ap


def _personal_push(cfg, reg, args):
    return integrate.publish_personal_branch(cfg, reg, args.repo, args=args, dry=args.dry_run)


def _merge_dev(cfg, reg, args):
    return integrate.merge_integration_to_local_dev(cfg, reg, args.repo, args=args, dry=args.dry_run)


def _push_dev(cfg, reg, args):
    return integrate.push_local_dev(cfg, reg, args.repo, args=args, dry=args.dry_run)


def main(argv=None, profile=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1
    if args.cmd == "guard":
        return guards.main(args.tool, task_root=args.task_root)
    if args.cmd == "hook":
        return hooks.main(args.tool, event=args.event)
    try:
        cfg, _why = load_config(args.profile or profile)
        reg = Registry(cfg)
        with contextlib.ExitStack() as stack:
            if args.cmd in TASK_LOCKED:
                tid = reg.find_by_ref(args.task)["id"]
                stack.enter_context(file_lock(cfg.locks_dir / f"task-{tid}.lock", wait_msg=f"{tid} 有别的操作进行中，等待"))
            rc = args.func(cfg, reg, args)
        return rc if isinstance(rc, int) else 0
    except WtError as e:
        say("err", str(e))
        return 1
    except (profile_mod.ProfileError, YamlSubsetError) as e:
        say("err", str(e))
        return 2
    except KeyboardInterrupt:
        say("err", "已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
