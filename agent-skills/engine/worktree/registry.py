# -*- coding: utf-8 -*-
"""登记簿：任务记录、编号分配、事件日志、受保护分支基线、文件锁与构建信号量。

所有写入走「同目录临时文件 + fsync + replace」，并在需要互斥的地方加跨进程文件锁。
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import IS_WIN, WtConfig, WtError
from ..action_context import ActionContext, ActionContextError, audit

if IS_WIN:  # pragma: no cover - 仅 Windows
    import msvcrt
else:
    import fcntl

TASK_STATES = ("active", "parked", "ready", "queued", "rejected", "landed", "promoted", "verified",
               "reverting", "archived")
LIVE_STATES = ("active", "parked", "ready", "queued", "rejected")
WORKING_STATES = ("active", "queued", "rejected")


def now():
    return datetime.datetime.now().astimezone()


def now_iso():
    return now().isoformat(timespec="seconds")


def parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def stamp():
    return now().strftime("%Y%m%d-%H%M%S")


def say(kind, msg):
    marks = {"ok": "✓", "warn": "!", "err": "✗", "info": " "}
    sys.stdout.write(f"{marks[kind]} {msg}\n")
    sys.stdout.flush()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextlib.contextmanager
def file_lock(path, blocking=True, wait_msg=""):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    acquired = announced = False
    try:
        while True:
            try:
                if IS_WIN:  # pragma: no cover
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if not blocking:
                    break
                if wait_msg and not announced:
                    say("info", wait_msg)
                    announced = True
                time.sleep(1.0)
        yield acquired
    finally:
        if acquired:
            try:
                if IS_WIN:  # pragma: no cover
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


@contextlib.contextmanager
def semaphore(cfg: WtConfig, name, slots):
    locks = [cfg.locks_dir / f"{name}-{i}.lock" for i in range(1, max(1, int(slots)) + 1)]
    announced = False
    while True:
        for lp in locks:
            cm = file_lock(lp, blocking=False)
            if cm.__enter__():
                try:
                    yield lp.name
                finally:
                    cm.__exit__(None, None, None)
                return
            cm.__exit__(None, None, None)
        if not announced:
            say("info", f"构建配额已满（同时 ≤{slots}），排队等待")
            announced = True
        time.sleep(2.0)


def task_sort_key(task):
    tid = task.get("id") or ""
    digits = tid[1:]
    return (tid[:1], int(digits) if digits.isdigit() else 0, tid)


class Registry:
    """一个数据根一份登记簿。"""

    def __init__(self, cfg: WtConfig):
        self.cfg = cfg

    # ------------------------------------------------------------ 锁
    def lock(self, name="registry", wait_msg=""):
        return file_lock(self.cfg.locks_dir / f"{name}.lock", wait_msg=wait_msg)

    # ------------------------------------------------------------ 任务
    def task_file(self, tid):
        return self.cfg.task_state_dir / f"{tid}.json"

    def save(self, task):
        task = {k: v for k, v in task.items() if not k.startswith("_")}
        task["updated_at"] = now_iso()
        atomic_json(self.task_file(task["id"]), task)
        if self.cfg.raw.get("share_activity") and task.get("os") == self.cfg.os:
            summary = {key: task.get(key) for key in (
                "id", "name", "slug", "title", "goal", "os", "state", "owner", "updated_at", "next_step", "creating")}
            summary.update(project=self.cfg.profile_name,
                           repos={a: {"branch": r["branch"]} for a, r in task["repos"].items()})
            try:
                atomic_json(self.cfg.hub / "state" / "activity" / self.cfg.os / f"{task['id']}.json", summary)
            except OSError:
                say("warn", "本地进度已保存，但共享交换目录不可写；另一端可能看到旧摘要。恢复连接后再记一次进度。")
        return task

    def shared_activity(self):
        """只读另一端的摘要，不访问其 worktree 或把摘要当成可执行的任务记录。"""
        if not self.cfg.raw.get("share_activity"):
            return []
        other = "windows" if self.cfg.os == "mac" else "mac"
        rows = []
        try:
            files = list((self.cfg.hub / "state" / "activity" / other).glob("*.json"))
            for path in files:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if data.get("project") == self.cfg.profile_name and data.get("os") == other:
                        data.update(_from_hub=True, _summary=True)
                        rows.append(data)
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        return rows

    def load(self, tid, must=True):
        f = self.task_file(tid)
        if f.exists():
            return self._prefer_republished(json.loads(f.read_text(encoding="utf-8")))
        if self.cfg.os == "mac":
            win = self.cfg.win_state_dir / f"{tid}.json"
            if win.exists():
                data = json.loads(win.read_text(encoding="utf-8"))
                data["_from_hub"] = True
                return data
        for data in self.shared_activity():
            if data["id"] == tid:
                return data
        if must:
            raise WtError(f"登记簿里没有任务 {tid}（aisk task status 查看全部）")
        return None

    def _prefer_republished(self, data):
        """Windows 任务被 mac 拒绝后修好重新 ready：hub 里的发布记录带着新的 ready 提交，应以它为准。"""
        if self.cfg.os != "mac" or data.get("state") not in ("ready", "queued", "rejected"):
            return data
        win = self.cfg.win_state_dir / f"{data['id']}.json"
        if not win.exists():
            return data
        published = json.loads(win.read_text(encoding="utf-8"))
        before = {a: r.get("ready_sha") for a, r in data.get("repos", {}).items()}
        after = {a: r.get("ready_sha") for a, r in published.get("repos", {}).items()}
        if published.get("state") != "ready" or before == after:
            return data
        published["_from_hub"] = True
        for alias, r in published.get("repos", {}).items():
            landed = data.get("repos", {}).get(alias, {}).get("landed_sha")
            if landed:
                r["landed_sha"] = landed
        return published

    def all(self, include_archived=False, include_hub=True):
        out = {}
        if self.cfg.task_state_dir.exists():
            for f in sorted(self.cfg.task_state_dir.glob("*.json")):
                d = json.loads(f.read_text(encoding="utf-8"))
                out[d["id"]] = d
        if include_hub and self.cfg.os == "mac" and self.cfg.win_state_dir.exists():
            for f in sorted(self.cfg.win_state_dir.glob("*.json")):
                d = json.loads(f.read_text(encoding="utf-8"))
                if d["id"] not in out:
                    d["_from_hub"] = True
                    out[d["id"]] = d
        if include_hub:
            for data in self.shared_activity():
                if data["id"] not in out:
                    out[data["id"]] = data
        tasks = list(out.values())
        if not include_archived:
            tasks = [t for t in tasks if t.get("state") != "archived"]
        return sorted(tasks, key=task_sort_key)

    def find_by_ref(self, ref):
        """接受任务编号（T042）、完整名（T042-coupon-claim-lock）或旧槽位号（me/0915-xxx）。"""
        ref = ref.strip()
        head = ref.split("-", 1)[0]
        if head and head[0].isalpha() and head[1:].isdigit():
            t = self.load(head, must=False)
            if t:
                return t
        for t in self.all(include_archived=True):
            if ref in (t.get("id"), t.get("name"), t.get("legacy_id")):
                return t
        raise WtError(f"登记簿里没有任务 {ref}（aisk task status 查看全部，aisk task find 搜索）")

    def find_by_path(self, path):
        path = Path(path).resolve()
        for t in self.all(include_hub=False):
            if not t.get("dir"):
                continue
            d = Path(t["dir"]).resolve()
            if path == d or d in path.parents:
                return t
        return None

    def set_state(self, task, state, note=""):
        if state not in TASK_STATES:
            raise WtError(f"内部错误：未知状态 {state}")
        old = task.get("state")
        task["state"] = state
        task.setdefault("history", []).append({"at": now_iso(), "from": old, "to": state, "note": note[:500]})
        self.save(task)
        self.event("state", task=task["id"], frm=old, to=state, note=note[:200])

    def next_id(self):
        """编号永不复用：计数器与已登记编号取大。调用方需持有登记簿锁。"""
        prefix = self.cfg.task_prefix
        counter = self.cfg.state_dir / "counter.json"
        data = json.loads(counter.read_text(encoding="utf-8")) if counter.exists() else {}
        used = [int(t["id"][len(prefix):]) for t in self.all(include_archived=True, include_hub=False)
                if t["id"].startswith(prefix) and t["id"][len(prefix):].isdigit()]
        n = max([int(data.get(prefix, 0))] + used) + 1
        data[prefix] = n
        atomic_json(counter, data)
        return f"{prefix}{n:03d}"

    # ------------------------------------------------------------ 事件与基线
    def event(self, kind, **data):
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        rec = {"at": now_iso(), "os": self.cfg.os, "kind": kind}
        rec.update(data)
        with open(self.cfg.state_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def load_refs(self, alias):
        f = self.cfg.refs_dir / f"{alias}.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def save_refs(self, alias, data):
        atomic_json(self.cfg.refs_dir / f"{alias}.json", data)


def remove_tree(path):
    """删除目录，删不干净就报错并列出残留。有的沙箱把删除改成静默失败还返回成功，不能把"调用过删除"当成"删掉了"。"""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    except OSError:
        pass
    if path.exists() or path.is_symlink():
        left = [str(p) for _, p in zip(range(5), path.rglob("*"))] if path.is_dir() else []
        raise WtError(f"没能删除 {path}（残留示例：{'、'.join(left) or path}）；请在普通终端手工确认后重试")


def confirm_human(prompt, expect, context=None, *, action=None, task_id="", session_id="", tool=None,
                  risk_level="HIGH", repository=""):
    """人工确认：macOS 弹系统对话框（终端里的 AI 点不到），无图形会话时退回终端输入；Windows 终端输入。
    没有任何环境变量开关——测试在进程内替换本函数。"""
    if context is None:
        try:
            context = ActionContext.from_env(
                action or ({"推送": "git推送", "落地": "git合并"}.get(expect, "确认")),
                task_id=task_id, session_id=session_id, tool=tool, risk_level=risk_level,
                summary=prompt, repository=repository,
            )
        except ActionContextError:
            # 未知/缺失工具身份必须拒绝，不生成没有归属的系统弹窗。
            return False
    if not IS_WIN and Path("/usr/bin/osascript").exists():
        text = (prompt + f"\n\n确认请点「{expect}」").replace("\\", "\\\\").replace('"', '\\"')
        title = context.title.replace("\\", "\\\\").replace('"', '\\"')
        script = (f'display dialog "{text}" with title "{title}" buttons {{"取消", "{expect}"}} '
                  f'default button "取消" giving up after 600')
        try:
            r = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=660)
            if r.returncode == 0:
                accepted = f"button returned:{expect}" in r.stdout and "gave up:true" not in r.stdout
                audit(context, "accepted" if accepted else "denied")
                return accepted
            if "-128" in (r.stderr or ""):
                audit(context, "cancelled")
                return False
        except (OSError, subprocess.SubprocessError):
            pass
    if not sys.stdin.isatty():
        audit(context, "denied", detail="no-interactive-tty")
        return False
    try:
        accepted = input(f"【{context.title}】\n{prompt}\n请输入「{expect}」确认：").strip() == expect
        audit(context, "accepted" if accepted else "denied")
        return accepted
    except EOFError:
        audit(context, "cancelled")
        return False
