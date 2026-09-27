"""公开发布前的确定性隐私扫描。

默认策略宁可误报，不读取或输出文件内容；只输出相对路径、规则编号和行号。

规则唯一真源是 `spec/privacy-classification.yaml` 的 `regex_rules`。工作区扫描
与 Git 历史扫描共用同一份从 spec 编译出来的规则——两处各手抄一份的写法已经漂移过
一次（历史侧过宽误报宿主路径、又漏掉 10.x/172.x 内网段），因此不再保留第二份。

已经发布、只能靠改写历史才能消除的命中，登记在 `spec/privacy-history-baseline.yaml`
（精确指纹，只作用于全历史扫描），见 `load_history_baseline` 与 `history_scan`。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath

from . import miniyaml

POLICY_RELATIVE = "spec/privacy-classification.yaml"
BASELINE_RELATIVE = "spec/privacy-history-baseline.yaml"
# 已发布面：历史基线只放行能从这里到达的提交。
PUBLISHED_REF = "refs/remotes/origin/master"
# 全历史扫描最多报出的阻断条数：够看清问题，又不至于在大面积命中时刷屏。
HISTORY_FINDING_CAP = 20
_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# 硬编码凭据只可能出现在配置文件里；文档/源码里的示例词不算泄漏。
SECRET_SUFFIXES = {".env", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
# 文档通用的占位内网地址，不代表真实拓扑，工作区与历史扫描都放过它。
# 字面量**刻意拆开写**：完整写出来会被 check_kernel_hygiene.py 的内网 IP 正则
# （`10\.\d{1,3}\.\d{1,3}\.\d{1,3}`）命中，而那条门禁要求 engine/ 零命中。
# 这是本仓既有的写法约定（重构前那处判断就是 `("10" + ".0.0.1") in line`），
# 别"顺手合并"成一个字符串。
NETWORK_PLACEHOLDER = "10" + ".0.0.1"
DEFAULT_EXCLUDES = {".git", ".aisk-runtime", ".aisk-private", "__pycache__", ".pytest_cache"}
PRIVATE_DOC_PATTERNS = ("MIGRATION_PLAN_*.md",)
PUBLIC_EXCLUDE_PATTERNS = (
    "agent-skills/DECISIONS.md",
    "agent-skills/ENVIRONMENT.md",
    "agent-skills/PLATFORMS.md",
    "agent-skills/TODO.md",
    "agent-skills/USAGE.md",
    "agent-skills/WINDOWS.md",
    "agent-skills/WORKTREE.md",
    "agent-skills/docs/reviews/*",
    "agent-skills/skills/brainstorming/references/*",
    # 这些文件定义扫描规则或迁移护栏，出现被扫描的关键词是规则本身，
    # 不是业务机密；真正的业务文件仍然照常扫描。
    "agent-skills/engine/privacy.py",
    "spec/privacy-classification.yaml",
    "bin/aisk",
)
PUBLIC_PATHS = (
    ".github",
    "bin",
    "docs",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "agent-skills/PUBLICATION_POLICY.md",
    "agent-skills/README.md",
    "agent-skills/adapters",
    "agent-skills/engine",
    "agent-skills/hooks",
    "agent-skills/requirements-tools.txt",
    "agent-skills/templates",
    "spec",
    "tools",
    "reports",
    # 整目录入面：新增技能或工具会自动进扫描面与导出面。逐个点名过一次名单没跟着
    # 迁移改，skills 只剩 `ai-worktree/SKILL.md` 一条 —— 导出包缺 8 个技能，
    # 共用同一份清单的 scan() 也读不到另外 19 个已发布文件（工作区侧假绿）。
    "agent-skills/skills",
    "agent-skills/tools",
    ".gitignore",
    "agent-skills/.gitignore",
    # tests 目录仍逐个点名：本地常放未入库的历史用例，整目录入面会把它们一起导出去。
    "agent-skills/tests/test_action_context.py",
    "agent-skills/tests/test_autoflow.py",
    "agent-skills/tests/test_autoflow_recovery.py",
    "agent-skills/tests/test_contracts.py",
    "agent-skills/tests/test_direct_checkout.py",
    "agent-skills/tests/test_direct_tasks.py",
    "agent-skills/tests/test_integrate_separated_flow.py",
    "agent-skills/tests/test_main_branch_guard.py",
    "agent-skills/tests/test_mcp_server.py",
    "agent-skills/tests/test_miniyaml.py",
    "agent-skills/tests/test_permissions.py",
    "agent-skills/tests/test_privacy.py",
    "agent-skills/tests/test_publish_driver.py",
    "agent-skills/tests/test_publish_notify.py",
    "agent-skills/tests/test_publish_pending.py",
    "agent-skills/tests/test_publish_scheduler.py",
    "agent-skills/tests/test_publish_worker.py",
    "agent-skills/tests/test_token_efficiency.py",
    "agent-skills/tests/test_token_runtime_golden.py",
    "agent-skills/tests/token_runtime_golden_tasks.json",
    "agent-skills/tests/test_worktree.py",
    "agent-skills/tests/test_worktree_quota.py",
    "agent-skills/tests/protocol_worktree.sh",
)
TEXT_SUFFIXES = {".md", ".markdown", ".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".txt", ".ini", ".cfg"}


class PrivacyPolicyError(RuntimeError):
    """规则策略缺失、损坏或不可用。

    门禁宁可失败关闭：策略读不到时必须产出一条不可读 finding 让验收变红，
    绝不退化成「没有规则所以扫不到命中」——那等于把门禁拆了。
    """


def load_policy(root: str | Path) -> list[dict]:
    """读取 spec 里声明的规则清单（原始条目，未编译）。"""
    path = Path(root).resolve() / POLICY_RELATIVE
    if not path.is_file():
        raise PrivacyPolicyError(f"缺少隐私规则策略: {path}")
    try:
        data = miniyaml.load_file(path)
    except Exception as exc:  # miniyaml 抛 YamlSubsetError，PyYAML 抛 YAMLError
        raise PrivacyPolicyError(f"隐私规则策略无法解析: {path}: {exc}") from exc
    rules = data.get("regex_rules") if isinstance(data, dict) else None
    if not isinstance(rules, list) or not rules:
        raise PrivacyPolicyError(f"隐私规则策略缺少 regex_rules: {path}")
    for item in rules:
        if not isinstance(item, dict) or not item.get("id") or not item.get("pattern"):
            raise PrivacyPolicyError(f"隐私规则条目缺少 id/pattern: {item!r}")
    return rules


def compile_rules(root: str | Path) -> tuple[tuple[str, re.Pattern, str], ...]:
    """把 spec 规则编译成 (id, 判定正则, git grep 预筛) 三元组。

    工作区扫描与历史扫描共用本函数的返回值，确保两侧判定完全一致。
    """
    compiled = []
    for item in load_policy(root):
        rule_id = str(item["id"])
        try:
            pattern = re.compile(str(item["pattern"]))
        except re.error as exc:
            raise PrivacyPolicyError(f"规则 {rule_id} 正则无法编译: {exc}") from exc
        prefilter = str(item.get("prefilter") or "").strip()
        if not prefilter:
            # 缺预筛会让历史扫描扫不到这条规则。宁可变红，也不静默放宽。
            raise PrivacyPolicyError(f"规则 {rule_id} 缺少 prefilter，历史扫描会漏检")
        compiled.append((rule_id, pattern, prefilter))
    return tuple(compiled)


def _hits(rules, relative_path: str, line: str) -> list[str]:
    """判定单行命中哪些规则；工作区与历史扫描共用这一处判定逻辑。"""
    suffix = Path(relative_path).suffix.lower()
    hits = []
    for rule_id, pattern, _prefilter in rules:
        if rule_id == "hardcoded_secret" and suffix not in SECRET_SUFFIXES:
            continue
        if rule_id == "rfc1918_ipv4" and NETWORK_PLACEHOLDER in line:
            continue
        if pattern.search(line):
            hits.append(rule_id)
    return hits


def _policy_finding(exc: Exception) -> dict:
    return {"rule": "policy-unreadable", "path": POLICY_RELATIVE, "line": 0, "detail": str(exc)}


def _baseline_finding(exc: Exception) -> dict:
    return {"rule": "history-baseline-invalid", "path": BASELINE_RELATIVE, "line": 0, "detail": str(exc)}


def _parse_fingerprint(raw, rule_ids) -> tuple[str, str, str, int]:
    """`<完整提交号>:<路径>:<规则>:<行号>` → (commit, path, rule, line)；不合格一律抛错。"""
    if not isinstance(raw, str) or raw.count(":") < 3:
        raise PrivacyPolicyError(f"历史基线条目应写成 <提交号>:<路径>:<规则>:<行号>，收到 {raw!r}")
    commit, rest = raw.split(":", 1)
    path, rule, line = rest.rsplit(":", 2)
    if not _FULL_SHA.match(commit):
        raise PrivacyPolicyError(f"历史基线条目必须写完整的小写提交号：{raw!r}")
    parts = PurePosixPath(path).parts
    if not path or path.startswith("/") or "\\" in path or ".." in parts:
        raise PrivacyPolicyError(f"历史基线条目的路径必须是仓库内相对路径：{raw!r}")
    if rule not in rule_ids:
        raise PrivacyPolicyError(f"历史基线条目引用了不存在的规则 {rule!r}：{raw!r}")
    if not line.isdigit() or int(line) < 1:
        raise PrivacyPolicyError(f"历史基线条目的行号必须是正整数：{raw!r}")
    return commit, path, rule, int(line)


def _baseline_refusal(commit: str, tips: set, published: set) -> str | None:
    """登记在基线里的命中为什么不能放行；可以放行时返回 None。"""
    if commit in tips:
        return "该提交是被扫描分支的顶端，泄漏仍在当前文件树里：先从文件树删除，不能靠历史基线放行"
    if not published:
        return f"本地没有 {PUBLISHED_REF}，无法确认该提交已经发布：先 git fetch origin 再验证"
    if commit not in published:
        return f"该提交还没有进入 {PUBLISHED_REF}：未发布的提交必须改掉，不能登记进历史基线"
    return None


def load_history_baseline(root: str | Path, rule_ids) -> dict:
    """读取已登记的历史命中：{(commit, path, rule, line): 原始条目}。

    **为什么需要它**：历史扫描的命中一旦发布，只有改写公开历史（强推）才能消除。在那之前
    门禁会永久变红——2026-09-24 起 CI 因此连单测都跑不到，红灯也就不再传递任何新信息。
    这里只登记「已经从文件树删掉、但仍留在已发布历史里」的命中，其余一律照常拦截：

    - 指纹是精确的 `提交号 + 路径 + 规则 + 行号`，提交号锁死内容，不能覆盖任何别的命中；
    - `history_scan` 永不放行被扫描分支的顶端提交。泄漏只要还在顶端的文件树里，顶端提交
      自己就会命中，而顶端提交号不可能预先写进它自己包含的这份文件；
    - 只放行已经进入 PUBLISHED_REF（origin/master）的提交。待合入的提交还能改：否则一个
      PR 在 A 提交引入泄漏、B 提交删掉并登记 A，就能带着泄漏通过 CI 的全历史扫描；
    - 工作树扫描与 `scan_since`（待发布提交的门禁）完全不读本文件。

    文件不存在即严格模式（空基线）；存在但写错时抛 PrivacyPolicyError，调用方失败关闭。
    """
    path = Path(root).resolve() / BASELINE_RELATIVE
    if not path.exists():
        return {}
    try:
        data = miniyaml.load_file(path)
    except Exception as exc:  # miniyaml 抛 YamlSubsetError，PyYAML 抛 YAMLError
        raise PrivacyPolicyError(f"历史基线无法解析: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PrivacyPolicyError(f"历史基线必须是 version: 1 的映射: {path}")
    entries = data.get("accepted_findings")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise PrivacyPolicyError(f"历史基线的 accepted_findings 必须是列表: {path}")
    baseline = {}
    for raw in entries:
        fingerprint = _parse_fingerprint(raw, set(rule_ids))
        if fingerprint in baseline:
            raise PrivacyPolicyError(f"历史基线条目重复：{raw!r}")
        baseline[fingerprint] = raw
    return baseline



def _excluded(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    relative_text = str(relative)
    if any(Path(relative_text).match(pattern) for pattern in PUBLIC_EXCLUDE_PATTERNS):
        return True
    if any(part in DEFAULT_EXCLUDES for part in relative.parts):
        return True
    return any(Path(relative).match(pattern) for pattern in PRIVATE_DOC_PATTERNS)


def _tracked_files(root: Path) -> set[Path] | None:
    """被 git 跟踪的文件绝对路径；root 不是 git 工作树时返回 None（退回整树扫描）。"""
    try:
        res = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return {root / rel for rel in res.stdout.split("\0") if rel}


def scan(root: str | Path) -> list[dict]:
    root = Path(root).resolve()
    if not root.exists():
        return [{"rule": "root-missing", "path": str(root), "line": 0}]
    try:
        rules = compile_rules(root)
    except PrivacyPolicyError as exc:
        return [_policy_finding(exc)]
    findings = []
    candidates = []
    for item in PUBLIC_PATHS:
        path = root / item
        if path.is_file():
            candidates.append(path)
        elif path.is_dir():
            candidates.extend(p for p in path.rglob("*") if p.is_file())
    tracked = _tracked_files(root)
    if tracked is not None:
        # 公开面 = 真的会被发布的文件。整目录入面之后，工作区里本地保留、从未入库的
        # 文件（例如只被 .git/info/exclude 挡住的迁移脚本）不算公开面：它们发不出去，
        # 为它们报红只会训练人忽略门禁。一旦 git add，它们立刻回到扫描范围。
        candidates = [p for p in candidates if p in tracked]
    for path in sorted(set(candidates)):
        if not path.is_file() or _excluded(path, root) or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        rel = str(path.relative_to(root))
        for number, line in enumerate(lines, 1):
            for rule_id in _hits(rules, rel, line):
                findings.append({"rule": rule_id, "path": rel, "line": number})
    return findings


def copy_public(source: str | Path, dest: str | Path) -> list[str]:
    """按 PUBLIC_PATHS 导出公开面；不复制整个 source 树。

    source 是 git 工作树时只导出被跟踪的文件，与 scan() 对「公开面」的定义一致。
    整目录拷贝会把 `.gitignore` 挡住的本地文件（agent-skills/skills、agent-skills/tools
    下的私有技能与脚本）一起带进公开候选，而它们从未经过扫描——扫描看跟踪文件、导出拷
    整个目录，两边口径不一就是一条绕过门禁的路。
    """
    import shutil

    source, dest = Path(source).resolve(), Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    tracked = _tracked_files(source)
    copied = []
    for item in PUBLIC_PATHS:
        src = source / item
        if not src.exists():
            continue
        target = dest / item
        if tracked is not None:
            files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
            files = [p for p in files if p in tracked and "__pycache__" not in p.parts and p.suffix != ".pyc"]
            for path in files:
                out = dest / path.relative_to(source)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, out)
            if files:
                copied.append(item + "/" if src.is_dir() else item)
        elif src.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            copied.append(item + "/")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied.append(item)
    return copied


def scan_git_history(root: str | Path) -> list[dict]:
    """全历史扫描中会让门禁变红的命中（已登记的历史债务不在其中），见 history_scan。"""
    return history_scan(root)["blocking"]


def history_scan(root: str | Path) -> dict:
    """扫描公开发布 ref 的全历史，避免“工作树干净但历史泄漏”。

    本地 `aisk task` 会创建只存在于运行时的任务分支；它们不是公开发布面，
    不能让本地私有任务内容污染公共仓库验收。公开仓库约定只发布 `master`
    与显式公开 tag，CI 仍以完整 clone 扫描这些公开 ref 的全部祖先。

    规则来自与 scan() 同一份 spec 编译结果。git grep 只用 spec 声明的 prefilter
    做粗筛（必须宽于判定规则），真正的判定仍由 _hits() 用编译后的 Python 正则给出。

    返回 {"blocking": 阻断命中, "accepted": 按历史基线放行的命中, "stale_baseline": 基线里
    已不再命中的条目, "complete": 是否扫完}。放行的命中照样逐条返回，调用方负责如实展示；
    阻断命中达到 HISTORY_FINDING_CAP 条时提前结束，此时 stale_baseline 无从判断，为空。
    """
    root = Path(root).resolve()
    result_shape = {"blocking": [], "accepted": [], "stale_baseline": [], "complete": True}
    if not (root / ".git").exists():
        return result_shape
    try:
        rules = compile_rules(root)
    except PrivacyPolicyError as exc:
        return {**result_shape, "blocking": [_policy_finding(exc)], "complete": False}
    try:
        baseline = load_history_baseline(root, [rule_id for rule_id, _pattern, _prefilter in rules])
    except PrivacyPolicyError as exc:
        return {**result_shape, "blocking": [_baseline_finding(exc)], "complete": False}
    prefilter = "|".join(f"({item})" for _rid, _pattern, item in rules)
    try:
        refs = []
        branch_probe = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True,
        )
        current_branch = branch_probe.stdout.strip() if branch_probe.returncode == 0 else ""
        # 公开发布验收必须扫描本地 master 的完整历史；任务工作区不是公开发布面，
        # 只扫描当前候选分支，避免另一条本机未推送的 master 历史污染 T003 门禁。
        candidates = ("refs/heads/master", "refs/remotes/origin/master") if current_branch == "master" \
            else ("HEAD",)
        for candidate in candidates:
            probe = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", candidate],
                capture_output=True, text=True,
            )
            if probe.returncode == 0:
                refs.append(candidate)
                break
        tags = subprocess.check_output(
            ["git", "-C", str(root), "for-each-ref", "--format=%(refname)", "refs/tags"],
            text=True, stderr=subprocess.DEVNULL,
        ).splitlines()
        refs.extend(tag for tag in tags if tag)
        if not refs:
            refs = ["HEAD"]
        commits = subprocess.check_output(
            ["git", "-C", str(root), "rev-list", *refs], text=True, stderr=subprocess.DEVNULL,
        ).splitlines()
        # 被扫描的顶端提交永不放行，未发布的提交也不放行：见 load_history_baseline 的说明。
        tips = set(subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", *(f"{ref}^{{commit}}" for ref in refs)],
            text=True, stderr=subprocess.DEVNULL,
        ).split())
        published = set()
        if baseline and subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", PUBLISHED_REF], capture_output=True,
        ).returncode == 0:
            published = set(subprocess.check_output(
                ["git", "-C", str(root), "rev-list", PUBLISHED_REF], text=True, stderr=subprocess.DEVNULL,
            ).split())
    except (OSError, subprocess.CalledProcessError):
        return {**result_shape, "blocking": [{"rule": "git-history-unreadable", "path": ".git", "line": 0}],
                "complete": False}
    findings, accepted, matched, refused = [], [], set(), set()
    complete = True
    for commit in commits:
        result = subprocess.run(
            # -i 让粗筛与 spec 里 (?i) 的判定保持同等宽松；粗筛多召回由 _hits 兜底。
            ["git", "-C", str(root), "grep", "-I", "-n", "-i", "-E", prefilter, commit, "--"],
            capture_output=True, text=True,
        )
        # **绝不截断 git grep 的输出行。** git grep 按路径排序输出，截断到前 N 行
        # 等价于只扫排序靠前的少数文件。2026-09-20 实测：本仓 HEAD 命中 526 行，
        # 前 40 行全部落在 README/adapters/engine（无一命中），于是 3 个含内部项目
        # 代号的公开文件整批漏检，`aisk public verify` 报出假绿。
        # 工作量由阻断命中上限（HISTORY_FINDING_CAP）约束，不靠砍输入行来省时间。
        # 已放行的历史债务不计入上限：它们在旧提交里成片出现，计入会让排在后面的新命中被挤掉。
        for line in result.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) < 3:
                continue
            # git grep 输出为 commit:path:line:text。扫描器实现、规则清单和旧目录
            # 拒绝护栏中的关键词属于可审计的安全规则本身，排除它们可避免
            # “扫描器因包含自己的规则而自报泄漏”。
            relative_path = parts[1]
            text = parts[3] if len(parts) > 3 else ""
            if any(Path(relative_path).match(pattern) for pattern in PUBLIC_EXCLUDE_PATTERNS):
                continue
            line_number = int(parts[2]) if parts[2].isdigit() else 0
            for rule_id in _hits(rules, relative_path, text):
                finding = {"rule": rule_id, "path": f"{commit[:12]}:{relative_path}", "line": line_number}
                fingerprint = (commit, relative_path, rule_id, line_number)
                if fingerprint in baseline:
                    refusal = _baseline_refusal(commit, tips, published)
                    if refusal is None:
                        matched.add(fingerprint)
                        accepted.append(finding)
                        continue
                    refused.add(fingerprint)
                    finding["detail"] = refusal
                findings.append(finding)
            if len(findings) >= HISTORY_FINDING_CAP:
                break
        if len(findings) >= HISTORY_FINDING_CAP:
            complete = False
            break
    stale = sorted(raw for fingerprint, raw in baseline.items()
                   if fingerprint not in matched and fingerprint not in refused) if complete else []
    return {"blocking": findings, "accepted": accepted, "stale_baseline": stale, "complete": complete}


_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,\d+)? @@")


def scan_since(root: str | Path, since: str) -> list[dict]:
    """只检查即将发布的提交：merge-base(since, HEAD)..HEAD 里每个提交相对第一父提交新增的行。

    单个任务的发布门禁只该为它自己引入的内容负责。全量历史扫描（scan_git_history）在
    门禁里会把别的任务早已推送、本任务改不了的历史遗留算到当前任务头上——2026-09-24
    一次历史泄露就让之后所有内核任务都无法完成。全量扫描仍由 `public verify`（不带
    --since）与 CI 负责，历史债务照样可见。

    逐提交而非净差异：区间内先加后删的行不在净差异里，但仍会随历史一起发布。
    规则与逐行判定和全量扫描共用 compile_rules / _hits；任何读不到的情况都失败关闭。

    **刻意不读历史基线**（spec/privacy-history-baseline.yaml）：基线只承认已经发布、改不
    回来的历史。待发布的提交还来得及改，登记进基线也照样拦——否则「本提交引入、下个提交
    删掉并登记」就能把泄露随历史一起推出去。
    """
    root = Path(root).resolve()
    try:
        rules = compile_rules(root)
    except PrivacyPolicyError as exc:
        return [_policy_finding(exc)]
    base = subprocess.run(["git", "-C", str(root), "merge-base", since, "HEAD"],
                          capture_output=True, text=True)
    base_sha = base.stdout.strip()
    if base.returncode != 0 or not base_sha:
        return [{"rule": "unpublished-range-unknown", "path": since, "line": 0}]
    log = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", "log", "--first-parent", "-m", "--no-color",
         "--no-renames", "--no-ext-diff", "--no-textconv", "-U0", "--src-prefix=a/", "--dst-prefix=b/",
         "--format=@@@commit %H", f"{base_sha}..HEAD"],
        capture_output=True, text=True,
    )
    if log.returncode != 0:
        return [{"rule": "git-history-unreadable", "path": ".git", "line": 0}]
    findings = []
    commit = path = None
    in_header = False
    lineno = 0
    for raw in log.stdout.splitlines():
        if raw.startswith("@@@commit "):
            commit, path, in_header = raw.split(" ", 1)[1].strip(), None, False
            continue
        if raw.startswith("diff --git "):
            path, in_header = None, True
            continue
        if raw.startswith("@@ "):
            in_header = False
            match = _HUNK.match(raw)
            lineno = int(match.group(1)) if match else 0
            continue
        if in_header:
            if raw.startswith("+++ "):
                target = raw[4:]
                if target.startswith('"') and target.endswith('"'):
                    target = target[1:-1]
                path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            continue
        if raw.startswith("+") and path is not None and commit:
            if not any(Path(path).match(pattern) for pattern in PUBLIC_EXCLUDE_PATTERNS):
                for rule_id in _hits(rules, path, raw[1:]):
                    findings.append({"rule": rule_id, "path": f"{commit[:12]}:{path}", "line": lineno})
            lineno += 1
    return findings


def verify(root: str | Path, since: str | None = None) -> tuple[bool, list[dict]]:
    report = verify_report(root, since=since)
    return report["ok"], report["findings"]


def verify_report(root: str | Path, since: str | None = None) -> dict:
    """verify 的完整结果：阻断命中之外，还给出按历史基线放行的命中与失效的基线条目。

    ok 只由 findings（阻断命中）决定；accepted_history / stale_history_baseline 必须由调用方
    如实展示——放行不等于隐藏。
    """
    report = {"ok": False, "since": since, "findings": [], "accepted_history": [], "stale_history_baseline": []}
    if since:
        findings = scan_since(root, since)
        return {**report, "ok": not findings, "findings": findings}
    findings = scan(root)
    if any(item.get("rule") == "policy-unreadable" for item in findings):
        # 策略不可读时不再继续，直接失败关闭。
        return {**report, "findings": findings}
    history = history_scan(root)
    findings = findings + history["blocking"]
    return {**report, "ok": not findings, "findings": findings,
            "accepted_history": history["accepted"], "stale_history_baseline": history["stale_baseline"]}


def write_report(root: str | Path, output: str | Path) -> int:
    report = verify_report(root)
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1
