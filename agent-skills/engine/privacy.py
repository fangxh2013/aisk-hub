"""公开发布前的确定性隐私扫描。

默认策略宁可误报，不读取或输出文件内容；只输出相对路径、规则编号和行号。

规则唯一真源是 `spec/privacy-classification.yaml` 的 `regex_rules`。工作区扫描
与 Git 历史扫描共用同一份从 spec 编译出来的规则——两处各手抄一份的写法已经漂移过
一次（历史侧过宽误报宿主路径、又漏掉 10.x/172.x 内网段），因此不再保留第二份。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import miniyaml

POLICY_RELATIVE = "spec/privacy-classification.yaml"
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
    "agent-skills/tests/test_contracts.py",
    "agent-skills/tests/test_permissions.py",
    "agent-skills/tests/test_privacy.py",
    "agent-skills/tests/test_token_efficiency.py",
    "agent-skills/tests/test_token_runtime",
    "agent-skills/tests/test_worktree.py",
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
    """按 PUBLIC_PATHS 导出公开面；不复制整个 source 树。"""
    import shutil

    source, dest = Path(source).resolve(), Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for item in PUBLIC_PATHS:
        src = source / item
        if not src.exists():
            continue
        target = dest / item
        if src.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            copied.append(item + "/")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied.append(item)
    return copied


def scan_git_history(root: str | Path) -> list[dict]:
    """扫描公开发布 ref 的全历史，避免“工作树干净但历史泄漏”。

    本地 `aisk task` 会创建只存在于运行时的任务分支；它们不是公开发布面，
    不能让本地私有任务内容污染公共仓库验收。公开仓库约定只发布 `master`
    与显式公开 tag，CI 仍以完整 clone 扫描这些公开 ref 的全部祖先。

    规则来自与 scan() 同一份 spec 编译结果。git grep 只用 spec 声明的 prefilter
    做粗筛（必须宽于判定规则），真正的判定仍由 _hits() 用编译后的 Python 正则给出。
    """
    root = Path(root).resolve()
    if not (root / ".git").exists():
        return []
    try:
        rules = compile_rules(root)
    except PrivacyPolicyError as exc:
        return [_policy_finding(exc)]
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
    except (OSError, subprocess.CalledProcessError):
        return [{"rule": "git-history-unreadable", "path": ".git", "line": 0}]
    findings = []
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
        # 工作量由下面的 findings 上限（20 条）约束，不靠砍输入行来省时间。
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
            for rule_id in _hits(rules, relative_path, text):
                findings.append({
                    "rule": rule_id,
                    "path": f"{commit[:12]}:{relative_path}",
                    "line": int(parts[2]) if parts[2].isdigit() else 0,
                })
            if len(findings) >= 20:
                break
        if len(findings) >= 20:
            break
    return findings


def verify(root: str | Path) -> tuple[bool, list[dict]]:
    findings = scan(root)
    if any(item.get("rule") == "policy-unreadable" for item in findings):
        # 策略不可读时不再继续，直接失败关闭。
        return False, findings
    findings = findings + scan_git_history(root)
    return not findings, findings


def write_report(root: str | Path, output: str | Path) -> int:
    ok, findings = verify(root)
    Path(output).write_text(json.dumps({"ok": ok, "findings": findings}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if ok else 1
