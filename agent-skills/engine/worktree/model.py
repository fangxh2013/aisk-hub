# -*- coding: utf-8 -*-
"""任务模型的纯函数：命名校验、提交说明、租约状态、查重相似度、看板分组。不做 IO，便于单测。"""
from __future__ import annotations

import re

from .config import WtError

SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){1,4}$")
TASK_ID_RE = re.compile(r"^[A-Z]\d{3,}$")
GENERIC_WORDS = {"fix", "feat", "task", "work", "update", "change", "misc", "temp", "test", "new", "the", "and"}


def validate_slug(cfg, slug):
    if not SLUG_RE.match(slug or "") or len(slug) > 40:
        raise WtError("英文短语须为 2–5 个小写单词、用连字符连接、≤40 字符，例如 coupon-claim-lock")
    if cfg.ai_re.search(slug):
        raise WtError("英文短语里不要出现 AI 工具名")


def slug_from_name(name, ai_re=None):
    """把工具给的任意名字（如原生 worktree 名）转成合规英文短语。"""
    words = [w[:16] for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if w]
    if ai_re is not None:
        words = [w for w in words if not ai_re.search(w)]
    words = words[:5]
    if not words or not words[0][0].isalpha():
        words.insert(0, "task")
    if len(words) < 2:
        words.append("work")
    words = words[:5]
    while len("-".join(words)) > 40 and len(words) > 2:
        words.pop()
    return "-".join(words)


def validate_title(title):
    title = (title or "").strip()
    if not title:
        raise WtError("中文标题必填（--title），让任何人一眼看懂任务在做什么")
    if len(title) > 30:
        raise WtError(f"中文标题 {len(title)} 字，请控制在 30 字以内")
    return title


def check_message(cfg, msg):
    body = "\n".join(l for l in msg.splitlines() if not l.startswith("#")).strip()
    if not body:
        return "提交说明为空"
    if len(body.splitlines()) != 1:
        return "提交说明必须只有一行"
    if re.search(r"co-authored-by:", body, re.I):
        return "提交说明不得包含 Co-authored-by"
    hit = cfg.ai_re.search(body)
    if hit:
        return f"提交说明含 AI 工具/模型名：{hit.group(0)}"
    if cfg.ai_email_re.search(body):
        return "提交说明含 AI 邮箱"
    if not cfg.message_re.match(body):
        return f"提交说明格式应为 {cfg.message_hint}"
    if len(body) > cfg.max_msg_chars:
        return f"提交说明 {len(body)} 字，超过 {cfg.max_msg_chars} 字"
    return None


# ------------------------------------------------------------------ 租约
HELD, IDLE, CLAIMABLE = "held", "idle", "claimable"
LEASE_LABEL = {HELD: "持有中", IDLE: "空闲", CLAIMABLE: "可接手"}


def lease_state(owner, activity_ts, now_ts, cfg):
    """owner 为 None 或 {tool, session, ...}；activity_ts/now_ts 为秒级时间戳（activity 可为 None）。"""
    if not owner:
        return CLAIMABLE
    if activity_ts is None:
        return CLAIMABLE
    age_min = max(0.0, (now_ts - activity_ts) / 60.0)
    if age_min >= cfg.claimable_minutes:
        return CLAIMABLE
    if age_min >= cfg.idle_minutes:
        return IDLE
    return HELD


def owner_sessions(owner):
    ids = list((owner or {}).get("sessions") or [])
    if (owner or {}).get("session") and owner["session"] not in ids:
        ids.append(owner["session"])
    return ids


def same_actor(owner, tool, sessions=()):
    """工具和可验证的会话号都须一致；缺少身份不能按工具名续租。"""
    if not owner or not tool or owner.get("tool") != tool:
        return False
    if isinstance(sessions, str):
        sessions = [sessions]
    mine, theirs = set(sessions or ()), set(owner_sessions(owner))
    if mine and theirs:
        return bool(mine & theirs)
    return tool == "human" and not mine and not theirs


def claim_decision(owner, state, tool, sessions, takeover):
    """返回 (允许, 说明)。持有中且非本人拒绝；空闲需 takeover；可接手直接允许。"""
    if same_actor(owner, tool, sessions):
        return True, "续租"
    if state == HELD:
        return False, "任务持有中"
    if state == IDLE and not takeover:
        return False, "任务空闲但仍有执行者，接手需 --takeover --reason"
    return True, "接手" if owner else "认领"


def age_text(activity_ts, now_ts):
    if activity_ts is None:
        return "从未活动"
    minutes = int(max(0, now_ts - activity_ts) // 60)
    if minutes < 60:
        return f"{minutes} 分钟前"
    if minutes < 48 * 60:
        return f"{minutes // 60} 小时前"
    return f"{minutes // 1440} 天前"


# ------------------------------------------------------------------ 查重
def _bigrams(text):
    text = re.sub(r"\s+", "", (text or "").lower())
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) > 1 else ({text} if text else set())


def title_similarity(a, b):
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def slug_overlap(a, b):
    wa = {w for w in (a or "").split("-") if w not in GENERIC_WORDS and not w.isdigit()}
    wb = {w for w in (b or "").split("-") if w not in GENERIC_WORDS and not w.isdigit()}
    return len(wa & wb)


def duplicate_candidates(tasks, title, slug, threshold=0.5):
    hits = []
    for t in tasks:
        score = title_similarity(title, t.get("title", ""))
        common = slug_overlap(slug, t.get("slug", ""))
        if score >= threshold or common >= 2:
            hits.append((round(score, 2), common, t))
    return sorted(hits, key=lambda h: (-h[0], -h[1]))


def search_score(task, query):
    q = (query or "").strip().lower()
    if not q:
        return 1.0
    fields = " ".join(str(task.get(k) or "") for k in ("id", "name", "title", "goal", "legacy_id")).lower()
    fields += " " + " ".join(r.get("branch", "") for r in (task.get("repos") or {}).values()).lower()
    words = [w for w in re.split(r"[\s,，]+", q) if w]
    if all(w in fields for w in words):
        return 1.0
    return title_similarity(q, task.get("title", "") + (task.get("goal") or ""))


# ------------------------------------------------------------------ 看板
GROUPS = [
    ("working", "有人在做"),
    ("paused", "暂停可接手"),
    ("ready", "待落地"),
    ("landed", "已落地待上主干"),
    ("promoted", "已上主干待验证"),
    ("verified", "已验证待归档"),
    ("reverting", "回滚中"),
]


def board_group(task, lease):
    st = task.get("state")
    if st in ("ready", "queued"):
        return "ready"
    if st == "landed":
        return "landed"
    if st == "promoted":
        return "promoted"
    if st == "verified":
        return "verified"
    if st == "reverting":
        return "reverting"
    if st == "parked" or lease != HELD:
        return "paused"
    return "working"
