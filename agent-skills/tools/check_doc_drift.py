#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档漂移门禁：文档里的状态快照必须和事实对得上。

用法：
    python3 tools/check_doc_drift.py          # 检查注册表里的全部断言
    python3 tools/check_doc_drift.py --list   # 只打印注册表，看它到底覆盖了什么
退出码：0=一致，1=有漂移或断言失锚

**为什么需要它**：文档里的数字是 AI 的输入。2026-09-17 实测：端点和技能数量曾经漂移，
入口文档没有写清 `~/.workbuddy-ai/skills` 的独立落点，导致那一端的技能没有被正确分发，
`aisk doctor` 照样报绿（它不看这个目录）。**数字过期不会报错，只会让下一个 AI
照错的办事。**

刻意不查的三类（边界必须写出来，否则绿灯又是假的）
--------------------------------------------------
1. **带日期的历史快照**。`WORKTREE.md` §10 的表头写着「证据（2026-09-16）」，
   那里的 277 项 / 94 项是当天的实测记录，改掉它才是篡改历史。当前值以
   `python3 tools/run_tests.py full` 与 `aisk doctor` 的输出为准。
2. **耗时（秒）**。机器快慢不同，机械断言只会把门禁变成噪音源，人就学会忽略它。
3. **运行时计数（单测项数、协议项数）**。要跑整套才拿得到，代价远超收益；
   而且它们是「当时的证据」，不是「现在的事实」。

CI 反事实断言：门禁的第二类覆盖（2026-09-19 补）
-----------------------------------------------
`.github/workflows` 一旦存在，仓库里就不许再出现「本仓库没有 CI」这类断言。
理由是同一类错误：文档把**不存在的约束**当事实讲给下一个 AI 听。
2026-09-19 实测：CI 已经加在 `.github/workflows/quality.yml`，而
`tools/run_tests.py` 的模块文档仍写着「本仓库**没有 CI**（无 `.github/workflows`）」——
下一个 AI 读到它，要么以为没有远端兜底而重复搭一套，要么以为本地门禁可有可无。
扫 `.py`/`.md`/`.sh`/`.yml`/`.yaml`/`.toml`；`.github/workflows` 真的不存在时，
这类说法是**事实**，不拦（否则门禁自己在说反话）。需要逐字引用反事实文本的文件
（本门禁自己的测试）在文件里写一行 `doc-drift: ci-absence-scan-off` 整份豁免；
刻意不做「整类目录豁免」，那会把 `tests/` 下未来的真实断言一起放过。

一条硬约束：断言失锚是错误，不是跳过
------------------------------------
文档被改写、正则不再命中时，必须报错，不许当成「这项不适用」放过去。宁可红，
不许把不确定项洗成绿——`check_kernel_hygiene.py` 的 `DEFAULT_ROOTS` 只扫 4 个路径、
不含 `engine/` 与 `tools/`，那句「✅ 干净」比它自己的设计目标窄，就是这条没守住。
"""

import argparse
import re
import sys
from pathlib import Path

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine import link  # noqa: E402

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
NUM = r"[0-9]+|[零一二两三四五六七八九十]+"


def read_number(text):
    """把「四」和「4」都读成 4；读不出来返回 None。"""
    t = text.strip()
    if t.isdigit():
        return int(t)
    if not t or any(ch not in CN_DIGITS for ch in t):
        return None
    if "十" in t:
        head, _, tail = t.partition("十")
        return (CN_DIGITS[head] if head else 1) * 10 + (CN_DIGITS[tail] if tail else 0)
    return CN_DIGITS[t]


# ------------------------------------------------------------------ 事实来源
# 每个事实都必须能从仓库自身现场算出，不碰网络、不写任何东西、不跑测试。
def skill_count():
    return len(list((KERNEL / "skills").glob("*/SKILL.md")))


def endpoint_count():
    return len(link.TARGETS)


def default_endpoint_count():
    return len(link.DEFAULT_TOOLS)


def claude_rename_count():
    return len(link.CLAUDE_RENAMES)


FACTS = {
    "skill_count": (skill_count, "内核 skills/*/SKILL.md 计数"),
    "endpoint_count": (endpoint_count, "engine/link.py 的 TARGETS 端数"),
    "default_endpoint_count": (default_endpoint_count, "engine/link.py 的 DEFAULT_TOOLS 端数"),
    "claude_rename_count": (claude_rename_count, "engine/link.py 的 CLAUDE_RENAMES 条数"),
}

# ------------------------------------------------------------------ 断言注册表
# 表里每一行就是门禁的一个覆盖点。锚点必须只命中一处；改文档措辞就要同步改这里，
# 漏改会被「断言失锚」挡下。
#
# **只放「内核技能数/端数」这类纯仓库事实。** 各端受管技能数刻意不进注册表：
# 那是分发后的现场状态（取决于最近一次 `aisk link` 跑没跑），不是仓库事实。
# 2026-09-17 曾把它绑到 skill_count，结果加一个技能、还没跑 link 时 6 行全红——
# 假红和假绿一样坏，所以断言只绑定公共入口的内核事实，不绑定端上现场数量。
CLAIMS = [
    ("README.md", "开头：内核技能数", rf"^({NUM}) 个技能，分发到", "skill_count"),
    ("README.md", "开头：分发端数", rf"分发到 ({NUM}) 个端", "endpoint_count"),
    ("README.md", "开头：Claude 端改名数", rf"端上改名.*?({NUM}) 个技能改了名", "claude_rename_count"),
    ("docs/AI-ONBOARDING.md", "`aisk link` 默认端数", rf"分发到默认 ({NUM}) 个端", "default_endpoint_count"),
]

# 端名覆盖：TARGETS 里每个端都必须在这些文档里被点名。
# 这是 2026-09-17 那次事故的直接防线——`workbuddy-ai` 当时在文档里根本不存在。
ENDPOINT_DOCS = ["README.md", "docs/ADAPTERS.md"]


# ------------------------------------------------------------------ CI 反事实断言
# 仓库根：`.github` 在 agent-skills 的上一层，所以这里要往上走一级。
ROOT = KERNEL.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SELF = Path(__file__).resolve()
SCAN_SUFFIXES = {".py", ".md", ".sh", ".yml", ".yaml", ".toml"}
# 整文件豁免标记：夹具**必须**逐字引用反事实文本的文件（本门禁自己的测试就是），
# 在文件里写上这一行即整份跳过。刻意不做「整类目录豁免」——那会把 tests/ 下
# 未来的真实断言一起放过，绿灯又会比设计目标窄。
SCAN_OFF = "doc-drift: ci-absence-scan-off"

# 「本仓库没有 CI」这类说法。CI 一旦存在，它就从事实变成反事实。
CI_ABSENCE = re.compile(
    r"(?:本)?仓库(?:里|中|内)?(?:并|也|都)?(?:没有|无|不含|不存在)\s*"
    r"(?:CI|持续集成|`?\.github/workflows`?)"
    r"|(?:没有|无)\s*CI\s*(?:配置|流水线|门禁|兜底|守护|可用)?"
)


def workflows_present(workflows=None):
    """CI 是否存在：以 `.github/workflows` 下有没有工作流文件为准，不看文档怎么说。"""
    directory = Path(workflows) if workflows else WORKFLOWS
    if not directory.is_dir():
        return False
    return any(p.suffix in (".yml", ".yaml") for p in directory.iterdir() if p.is_file())


def check_ci_absence(root=None, workflows=None):
    """返回 [(相对 root 的路径, 行号, 命中片段)]。

    只在 `.github/workflows` 确实存在时才拦——否则「本仓库没有 CI」是事实，
    拦它等于门禁自己在说反话。跳过两种文件：反事实模式的字面量所在的自己，
    以及带 `SCAN_OFF` 标记的文件（夹具需要逐字引用反事实文本）。
    """
    if not workflows_present(workflows):
        return []
    base = Path(root) if root else KERNEL
    hits = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if "__pycache__" in path.parts or path.resolve() == SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if SCAN_OFF in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            m = CI_ABSENCE.search(line)
            if m:
                try:
                    where = str(path.relative_to(base))
                except ValueError:
                    where = str(path)
                hits.append((where, lineno, m.group(0)))
    return hits


# ------------------------------------------------------------------ 检查
def load(name):
    path = KERNEL.parent / name
    if not path.is_file():
        path = KERNEL / name
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def check_claims(texts):
    """返回 (不符, 失锚)。失锚单独一类，因为它要改的是注册表而不是文档。"""
    mismatch, lost = [], []
    for name, label, pattern, fact in CLAIMS:
        text = texts.get(name)
        if text is None:
            lost.append((name, label, pattern, "文件不存在"))
            continue
        hits = [(i, m) for i, line in enumerate(text.splitlines(), 1)
                for m in [re.search(pattern, line)] if m]
        if not hits:
            lost.append((name, label, pattern, "锚点命中 0 处"))
            continue
        if len(hits) > 1:
            lost.append((name, label, pattern, f"锚点命中 {len(hits)} 处（应只命中 1 处）"))
            continue
        lineno, m = hits[0]
        want = read_number(m.group(1))
        got = FACTS[fact][0]()
        if want is None:
            lost.append((name, label, pattern, f"读不出数字：{m.group(1)!r}"))
        elif want != got:
            mismatch.append((f"{name}:{lineno}", label, want, got, FACTS[fact][1]))
    return mismatch, lost


def check_endpoints(texts):
    """端名覆盖：TARGETS 的每个端名都要在权威文档里作为整词出现。

    整词判定用 `(?<![\\w-])` / `(?![\\w-])`，否则 `workbuddy` 会被
    `workbuddy-ai` 里的片段满足，漏掉「桌面版这一端没写」。
    """
    missing = []
    for name in ENDPOINT_DOCS:
        text = texts.get(name)
        if text is None:
            missing.append((name, "文件不存在"))
            continue
        for tool in link.TARGETS:
            if not re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", text, re.I):
                missing.append((name, tool))
    return missing


def print_registry():
    print(f"断言注册表（{len(CLAIMS)} 条数字断言 + {len(ENDPOINT_DOCS)} 处端名覆盖）\n")
    for name, label, pattern, fact in CLAIMS:
        print(f"  {name:<14} {label:<26} → {fact}")
        print(f"  {'':<14} 锚点 {pattern}")
    print(f"\n端名覆盖：{ '、'.join(ENDPOINT_DOCS) } 必须出现 TARGETS 全部端名")
    print(f"  TARGETS = {list(link.TARGETS)}")
    print(f"  DEFAULT_TOOLS = {link.DEFAULT_TOOLS}")
    state = "启用" if workflows_present() else "不启用（本仓库确实没有 CI）"
    print(f"\nCI 反事实断言：{state}")
    print(f"  以 {WORKFLOWS} 里有没有工作流文件为准；扫 {'、'.join(sorted(SCAN_SUFFIXES))}")
    print(f"  整文件豁免标记：{SCAN_OFF}（只给必须逐字引用反事实文本的夹具用）")
    print("\n刻意不查：带日期的历史快照、耗时（秒）、运行时计数（单测/协议项数）")


def main():
    ap = argparse.ArgumentParser(description="文档漂移门禁")
    ap.add_argument("--list", action="store_true", help="只打印断言注册表")
    a = ap.parse_args()
    if a.list:
        print_registry()
        return 0

    texts = {name: load(name) for name in
             {c[0] for c in CLAIMS} | set(ENDPOINT_DOCS)}
    mismatch, lost = check_claims(texts)
    missing = check_endpoints(texts)
    ci_hits = check_ci_absence()
    rc = 1 if (mismatch or lost or missing or ci_hits) else 0

    if not rc:
        files = "、".join(sorted({c[0] for c in CLAIMS} | set(ENDPOINT_DOCS)))
        print(f"✅ 文档漂移检查通过：{len(CLAIMS)} 条数字断言与事实一致，"
              f"{len(link.TARGETS)} 个端名在 {len(ENDPOINT_DOCS)} 份文档里都有点名")
        print(f"   覆盖文件：{files}")
        print(f"   CI 反事实断言：{'已启用（.github/workflows 存在）' if workflows_present() else '未启用（本仓库确实没有 CI）'}")
        print("   未纳入机械判定：带日期的历史快照、耗时、运行时计数（理由见模块文档）")
        return 0

    total = len(mismatch) + len(lost) + len(missing) + len(ci_hits)
    print(f"❌ 文档漂移：{total} 处需要处理\n")

    if mismatch:
        print(f"── 数字与事实不符（{len(mismatch)} 处）")
        for where, label, want, got, source in mismatch:
            print(f"   {where}  {label}")
            print(f"      文档说 {want}，实际 {got}（{source}）")
        print()

    if missing:
        print(f"── 端名没在文档里点名（{len(missing)} 处）")
        for name, tool in missing:
            print(f"   {name} 缺少端名 `{tool}` —— 加端时漏改文档，"
                  f"后果是那一端「一个技能都不会被分发」")
        print()

    if ci_hits:
        print(f"── CI 反事实断言（{len(ci_hits)} 处）：CI 已存在，这类说法必须改成事实")
        for where, lineno, snippet in ci_hits:
            print(f"   {where}:{lineno}  「{snippet}」")
        print()

    if lost:
        print(f"── 断言失锚：请改 tools/check_doc_drift.py 的注册表，不是改这条断言（{len(lost)} 处）")
        for name, label, pattern, why in lost:
            print(f"   {name}  {label}：{why}")
            print(f"      锚点 {pattern}")
        print()

    return 1


if __name__ == "__main__":
    sys.exit(main())
