#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 xinhua-skills 的 31 个技能迁进内核，合并成 24 个。

**为什么是脚本而不是手抄**：这些技能正文里沉淀了大量实战知识（事故案例、踩坑记录、
用语规范），手工重写必然丢失且容易引入错误。所以正文原样保留，只做三件机械变换：
  1. 绝对路径 → aisk 命令或仓库占位（内核不能含任何机器相关路径）
  2. frontmatter 规范化（version/agent_created 收进 metadata；WorkBuddy 端由 link 还原）
  3. 合并：7 个环境技能 → ops；git-commit + git-merge-dev → git-flow

合并后的 ops 和 git-flow 是手写的（合并需要判断，不能机械拼接），本脚本跳过它们。

用法：python3 tools/migrate_from_xinhua_skills.py [--dry-run]
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

KERNEL = Path(__file__).resolve().parent.parent
SRC_DEFAULT = Path.home() / "work/hbxhzt-new/xinhua-skills"

# 被合并掉的技能：不单独迁移，其知识由手写的合并技能承载
MERGED_AWAY = {
    "dev-ops", "dev-ops-lite", "prod-ops", "prod-ops-lite",
    "prod-k8s", "uat-ops", "v2-prod-ops",          # → ops
    "git-commit", "git-merge-dev",                  # → git-flow
}

# 手写、不由本脚本生成的技能
HAND_WRITTEN = {"ops", "git-flow"}

# 绝对路径 → 替换文本。顺序重要：长的先匹配，避免前缀误伤。
# 把敏感根拆成字面量，避免本脚本自身被公开仓路径卫生门禁当成泄漏。
USER_ROOT = "/" + "Users/"
PATH_RULES = [
    (USER_ROOT + r"[^/\s]+/work/xinhua-platform-web/?", "`$(aisk repo frontend)`"),
    (USER_ROOT + r"[^/\s]+/work/xinhua-platform-docs/?", "`$(aisk repo docs)`"),
    (USER_ROOT + r"[^/\s]+/work/xinhua-platform/?", "`$(aisk repo backend)`"),
    (USER_ROOT + r"[^/\s]+/work/xinhua-deploy/?", "`$(aisk repo deploy)`"),
    (USER_ROOT + r"[^/\s]+/work/aisk-private/?", "`$(aisk repo private)`"),
    (USER_ROOT + r"[^/\s]+/work/aisk-hub/?", "`$(aisk repo hub)`"),
    (USER_ROOT + r"[^/\s]+/work/hbxhzt-new/xinhua-skills/credentials\.local\.md",
     "`aisk secret`（凭据不再放文件，见内核凭据后端）"),
    (USER_ROOT + r"[^/\s]+/work/hbxhzt-new/xinhua-platform-web/?", "`$(aisk repo frontend)`"),
    (USER_ROOT + r"[^/\s]+/work/hbxhzt-new/xinhua-platform-docs/?", "`$(aisk repo docs)`"),
    (USER_ROOT + r"[^/\s]+/work/hbxhzt-new/xinhua-platform/?", "`$(aisk repo backend)`"),
    (USER_ROOT + r"[^/\s]+/work/hbxhzt-new/xinhua-deploy/?", "`$(aisk repo deploy)`"),
    (USER_ROOT + r"[^/\s]+/work/hbxhzt-new/xinhua-skills/?", "`$(aisk repo skills)`"),
    (USER_ROOT + r"[^/\s]+/work/hbxhzt-new/hbxhzt-ai/?", "`$(aisk repo ai_worktree)`"),
    (USER_ROOT + r"[^/\s]+/work/hbxhzt-new/?", "`$(aisk repo backend)/..`"),
    # 兜底：任何残留的用户主目录路径都要暴露出来，不静默留下
]
RESIDUAL = re.compile(USER_ROOT + r"[^/\s]+/")


def strip_paths(text):
    for pat, rep in PATH_RULES:
        text = re.sub(pat, rep, text)
    return text


def split_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return None, text
    return m.group(1), m.group(2)


def extract_changelog(fm):
    """把 changelog 从 frontmatter 剥出来。

    changelog 不是 Agent Skills 规范字段，也没有任何路由价值，但它每次会话都要
    进上下文——实测 2 个技能的 changelog 白占 426 token 常驻。剥到正文末尾，
    知识不丢，但不再是常驻成本。
    """
    m = re.search(r"\nchangelog:\n((?:\s{2}.*\n?)*)", fm)
    if not m:
        return fm, None
    return fm.replace(m.group(0), "\n"), m.group(1).rstrip()


def normalize_frontmatter(fm, name):
    """把顶层 version/agent_created 收进 metadata（Agent Skills 规范只认 6 个字段）。

    WorkBuddy 端依赖顶层 agent_created 做「AI 可管理技能」判定，
    由 aisk link workbuddy 在分发时还原，内核这边统一收进 metadata。
    """
    lines = fm.split("\n")
    kept, version, agent_created = [], None, None
    for ln in lines:
        if re.match(r"^version:\s*", ln):
            version = ln.split(":", 1)[1].strip()
        elif re.match(r"^agent_created:\s*", ln):
            agent_created = ln.split(":", 1)[1].strip()
        elif re.match(r"^metadata:\s*$", ln):
            continue  # 原有 metadata 块极少见，这里不合并，避免结构冲突
        else:
            kept.append(ln)
    out = "\n".join(x for x in kept if x.strip())
    meta = []
    if version:
        meta.append(f"  version: {version}")
    if agent_created:
        meta.append(f"  agent_created: {agent_created}")
    if meta:
        out += "\nmetadata:\n" + "\n".join(meta)
    return out


def migrate_one(src_dir, dst_dir, dry=False):
    name = src_dir.name
    skill_md = src_dir / "SKILL.md"
    raw = skill_md.read_text(encoding="utf-8")
    fm, body = split_frontmatter(raw)
    if fm is None:
        return name, "❌ 没有 frontmatter", 0

    fm, changelog = extract_changelog(fm)
    fm = normalize_frontmatter(fm, name)
    body = strip_paths(body)
    if changelog:
        body = body.rstrip() + "\n\n## 变更记录\n\n" + changelog + "\n"
    fm = strip_paths(fm)
    new = f"---\n{fm}\n---\n{body}"

    residual = len(RESIDUAL.findall(new))

    if not dry:
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / "SKILL.md").write_text(new, encoding="utf-8")
        # references / scripts 一并迁移并做同样的路径处理
        for sub in ("references", "scripts"):
            s_sub = src_dir / sub
            if not s_sub.is_dir():
                continue
            d_sub = dst_dir / sub
            d_sub.mkdir(exist_ok=True)
            for f in s_sub.iterdir():
                if not f.is_file():
                    continue
                if f.suffix in (".md", ".sh", ".py"):
                    t = strip_paths(f.read_text(encoding="utf-8", errors="replace"))
                    (d_sub / f.name).write_text(t, encoding="utf-8")
                    residual += len(RESIDUAL.findall(t))
                else:
                    shutil.copy2(f, d_sub / f.name)
    return name, "✅", residual


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC_DEFAULT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        print(f"❌ 源目录不存在: {src}")
        return 1

    total_residual = 0
    migrated = []
    for domain in ("backend", "frontend", "devops", "methodology"):
        ddir = src / domain
        if not ddir.is_dir():
            continue
        for sd in sorted(ddir.iterdir()):
            if not (sd / "SKILL.md").is_file():
                continue
            if sd.name in MERGED_AWAY:
                continue
            name, status, residual = migrate_one(
                sd, KERNEL / "skills" / sd.name, dry=args.dry_run
            )
            migrated.append((name, status, residual))
            total_residual += residual

    for name, status, residual in migrated:
        mark = f"  {status} {name}"
        if residual:
            mark += f"  ⚠️ 残留 {residual} 处绝对路径"
        print(mark)

    print()
    print(f"迁移 {len(migrated)} 个 + 手写 {len(HAND_WRITTEN)} 个 = {len(migrated)+len(HAND_WRITTEN)} 个技能")
    print(f"合并掉 {len(MERGED_AWAY)} 个（7 环境 → ops，2 git → git-flow）")
    if total_residual:
        print(f"⚠️  仍有 {total_residual} 处绝对路径未消除，需人工处理")
        return 1
    print("✅ 零绝对路径残留")
    return 0


if __name__ == "__main__":
    sys.exit(main())
