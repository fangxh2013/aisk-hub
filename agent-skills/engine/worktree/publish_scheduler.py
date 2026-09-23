# -*- coding: utf-8 -*-
"""One-shot publisher runner and macOS LaunchAgent for Xinhua push retries."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import publish_notify, publish_worker
from .config import WtError
from .registry import atomic_text, file_lock, say

LABEL = "com.aisk.xinhua-publish-worker"
INTERVAL_SECONDS = 60


def _hub_root():
    configured = os.environ.get("AISK_HUB_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[3]


def _launchd_target(cfg):
    if sys.platform != "darwin" or cfg.os != "mac":
        raise WtError("发布重试调度器仅支持 macOS LaunchAgent")
    if cfg.profile_name != "xinhua":
        raise WtError("此调度器只允许 xinhua 档案")
    root = _hub_root()
    launcher = root / "bin" / "aisk"
    if not launcher.is_file():
        raise WtError(f"找不到稳定的 aisk 入口：{launcher}")
    agent_dir = Path.home() / "Library" / "LaunchAgents"
    path = agent_dir / f"{LABEL}.plist"
    return root, launcher, agent_dir, path


def _plist(cfg, root, launcher):
    stdout = cfg.logs_dir / "publish-worker.stdout.log"
    stderr = cfg.logs_dir / "publish-worker.stderr.log"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(launcher), "--profile", "xinhua", "task", "publish-due"],
        "WorkingDirectory": str(root),
        "StartInterval": INTERVAL_SECONDS,
        "RunAtLoad": True,
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
        "ProcessType": "Background",
    }


def _owned_plist(path, launcher):
    if not path.exists():
        return False
    try:
        with path.open("rb") as stream:
            data = plistlib.load(stream)
    except Exception as error:
        raise WtError(f"已有调度文件无法解析，拒绝覆盖：{path}（{type(error).__name__}）") from error
    if not isinstance(data, dict):
        raise WtError(f"已有调度文件结构无效（顶层必须是字典）：{path}")
    argv = data.get("ProgramArguments")
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) or not arg.strip() for arg in argv)):
        raise WtError(f"已有调度文件 ProgramArguments 必须是非空字符串数组：{path}")
    if data.get("Label") != LABEL or str(Path(argv[0]).resolve()) != str(launcher.resolve()):
        raise WtError(f"LaunchAgent 路径已被其他配置占用，拒绝覆盖：{path}")
    return True


def _launchctl(*args):
    try:
        return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        raise WtError(f"launchctl {args[0]} 失败：{type(error).__name__}: {error}") from error


def _service_not_loaded(result):
    """Recognize only launchctl's explicit missing-service response."""
    detail = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return "could not find service" in detail or "service not found" in detail


def _launchctl_error(result):
    return (result.stderr or result.stdout or "launchctl 返回非零状态").strip()[-500:]


def cmd_publish_due(cfg, reg, _args=None):
    """Run due Xinhua fxh-dev retries and deliver visible, deduplicated alerts."""
    if sys.platform != "darwin" or cfg.profile_name != "xinhua" or cfg.os != "mac":
        raise WtError("publish-due 仅允许在 macOS xinhua 档案中运行")
    with file_lock(cfg.locks_dir / "publish-worker.lock", blocking=False) as acquired:
        if not acquired:
            say("info", "另一个发布重试 worker 正在运行，本次跳过")
            return 0
        report = publish_worker.run_due_publications(cfg, reg)
        for result in report.results:
            if result.attempted:
                if result.status == "published":
                    say("ok", f"{result.task_id} {result.alias}: origin/fxh-dev 已确认 {str(result.remote_sha or '')[:9]}")
                else:
                    say("warn", f"{result.task_id} {result.alias}: {result.status} — {result.error or result.skipped_reason or '发布未确认'}")
        notifications_failed = False
        event_log = cfg.state_dir / "publish-notifications.json"
        attempted_event_ids = set()
        for event in report.events:
            outcome = publish_notify.notify_publish_event(event, log_path=event_log)
            if outcome.event_id:
                attempted_event_ids.add(outcome.event_id)
            if outcome.status == "notified":
                say("warn" if event.get("type") != "resolved" else "ok",
                    f"桌面通知已发送：{event.get('task_id')} {event.get('alias')} {event.get('type')}")
            elif outcome.status == "duplicate":
                say("info", f"通知已发送过：{event.get('task_id')} {event.get('alias')} {event.get('type')}")
            elif outcome.status == "logged_only":
                notifications_failed = True
                say("warn", f"系统通知不可用，升级事件已持久记录：{event.get('task_id')} {event.get('alias')} {event.get('type')}")
            else:
                notifications_failed = True
                say("err", f"系统通知失败，升级事件仍在持久队列：{outcome.error or outcome.status}")
        # publish_pending emits each escalation only once. Retry older durable
        # notification intents on each scheduler tick so a temporary osascript
        # failure does not strand an event after its one-shot worker emission.
        for outcome in publish_notify.drain_pending_notifications(
                log_path=event_log, exclude_event_ids=attempted_event_ids):
            if outcome.status == "notified":
                say("warn", f"积压的桌面通知已发送：{outcome.event_id[:12]}")
            elif outcome.status == "duplicate":
                say("info", f"积压通知已发送过：{outcome.event_id[:12]}")
            else:
                notifications_failed = True
                say("err", f"积压升级通知仍未送达：{outcome.error or outcome.status}；事件 {outcome.event_id[:12]}")
        if not report.results:
            say("info", "没有到期的 fxh-dev 发布任务")
        return 1 if notifications_failed else 0


def cmd_scheduler(cfg, _reg, args):
    action = args.scheduler_action
    root, launcher, agent_dir, plist_path = _launchd_target(cfg)
    label_target = f"gui/{os.getuid()}/{LABEL}"
    if action == "status":
        if not plist_path.exists():
            say("warn", f"发布重试调度器未安装：{plist_path}")
            return 1
        _owned_plist(plist_path, launcher)
        status = _launchctl("print", label_target)
        if status.returncode == 0:
            say("ok", f"发布重试调度器已运行；每 {INTERVAL_SECONDS} 秒检查一次 fxh-dev 待发布任务")
            return 0
        if _service_not_loaded(status):
            say("warn", f"LaunchAgent 文件存在但当前未加载：{plist_path}")
            return 1
        raise WtError(f"无法确认 LaunchAgent 加载状态，保留现有配置：{_launchctl_error(status)}")

    if action == "install":
        agent_dir.mkdir(parents=True, exist_ok=True)
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        _owned_plist(plist_path, launcher)
        xml = plistlib.dumps(_plist(cfg, root, launcher), fmt=plistlib.FMT_XML).decode("utf-8")
        loaded = _launchctl("print", label_target)
        if loaded.returncode == 0:
            unloaded = _launchctl("bootout", label_target)
            if unloaded.returncode != 0:
                raise WtError(f"旧 LaunchAgent 无法安全卸载：{unloaded.stderr.strip()[-300:]}")
        elif not _service_not_loaded(loaded):
            raise WtError(f"无法确认旧 LaunchAgent 状态，未覆盖配置：{_launchctl_error(loaded)}")
        atomic_text(plist_path, xml)
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
        if result.returncode != 0:
            raise WtError(f"LaunchAgent 安装失败：{result.stderr.strip()[-500:]}")
        verified = _launchctl("print", label_target)
        if verified.returncode != 0:
            raise WtError("LaunchAgent 文件已写入，但 launchctl 未确认服务加载")
        say("ok", f"发布重试调度器已安装：{plist_path}；每 {INTERVAL_SECONDS} 秒检查，失败升级通过 macOS 通知提示")
        return 0

    if action == "remove":
        if not plist_path.exists():
            say("info", "发布重试调度器未安装")
            return 0
        _owned_plist(plist_path, launcher)
        loaded = _launchctl("print", label_target)
        if loaded.returncode == 0:
            result = _launchctl("bootout", label_target)
            if result.returncode != 0:
                raise WtError(f"LaunchAgent 卸载失败：{result.stderr.strip()[-300:]}")
        elif not _service_not_loaded(loaded):
            raise WtError(f"无法确认 LaunchAgent 是否已卸载，保留配置文件：{_launchctl_error(loaded)}")
        plist_path.unlink()
        say("ok", "发布重试调度器已卸载；历史发布状态与通知记录保留")
        return 0
    raise WtError(f"不支持的调度器操作：{action}")
