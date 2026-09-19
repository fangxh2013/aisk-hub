"""高风险动作的工具归属、弹窗标题与审计协议。

这个模块只记录动作元数据，不记录凭据、提示词全文或业务数据。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

TOOLS = ("codex", "claude", "antigravity", "workbuddy", "workbuddy-ai", "cursor", "human")


class ActionContextError(ValueError):
    pass


def normalize_tool(tool: str | None) -> str | None:
    if tool is None:
        return None
    value = str(tool).strip().lower()
    aliases = {"workbuddy_ai": "workbuddy-ai", "workbuddyai": "workbuddy-ai"}
    return aliases.get(value, value)


@dataclass(frozen=True)
class ActionContext:
    tool: str
    action: str
    task_id: str = ""
    session_id: str = ""
    risk_level: str = "HIGH"
    summary: str = ""
    repository: str = ""

    def __post_init__(self):
        tool = normalize_tool(self.tool)
        if tool not in TOOLS:
            raise ActionContextError(f"未识别的 AI 工具归属：{self.tool!r}；必须显式使用 --tool 或 AISK_TOOL")
        object.__setattr__(self, "tool", tool)

    @property
    def title(self) -> str:
        task = self.task_id or "系统"
        if self.repository:
            # WorkBuddy 任务外 hook 没有固定任务根；仓库名必须进入标题，避免只看见一个
            # 无法审计的「系统」弹窗。没有 repository 时保留旧格式，Codex/其它端零变化。
            return f"{self.action}-{self.tool} | {task} | {self.repository}"
        return f"{self.action}-{self.tool}｜{task}"

    @classmethod
    def from_env(cls, action: str, *, task_id: str = "", session_id: str = "",
                 tool: str | None = None, risk_level: str = "HIGH", summary: str = "",
                 repository: str = ""):
        selected = normalize_tool(tool or os.environ.get("AISK_TOOL"))
        if selected is None:
            # 延迟导入，避免 actor 与本模块形成导入环。
            from .worktree.actor import detect_tool
            selected = normalize_tool(detect_tool())
        if selected is None:
            selected = "human" if os.isatty(0) else None
        if selected is None:
            raise ActionContextError("无法确定执行工具归属；高风险动作必须传 --tool 或设置 AISK_TOOL")
        sid = session_id or os.environ.get("AISK_SESSION", "")
        return cls(selected, action, task_id, sid, risk_level, summary[:240], repository)


def audit_path() -> Path:
    root = os.environ.get("AISKHUB_RUNTIME_ROOT") or os.environ.get("AISK_HOME")
    return Path(root or (Path.home() / ".aisk-runtime")) / "logs" / "dialog_audit.jsonl"


def audit(context: ActionContext, result: str, *, detail: str = "") -> None:
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": context.tool,
        "action": context.action,
        "title": context.title,
        "task_id": context.task_id,
        "session_id": context.session_id,
        "risk_level": context.risk_level,
        "result": result,
    }
    if context.repository:
        record["repository"] = context.repository
    if detail:
        record["detail"] = detail[:240]
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
