"""Safety primitives for tasks that edit a repository's ordinary checkout.

The caller acquires a persistent per-repository lease, captures a clean
named-branch baseline, then validates Git-visible changes before committing.
Short OS file locks serialize lease-record updates across CLI processes. The
lease remains until its owner explicitly releases it. Validation is read-only:
conflicts are reported and never reverted, stashed, or overwritten.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import ntpath
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .worktree.registry import atomic_json, file_lock


class DirectCheckoutError(RuntimeError):
    """Base error for direct-checkout safety checks."""


class RepositoryError(DirectCheckoutError):
    """The supplied path is not a usable Git working tree."""


class RepoLeaseBusy(DirectCheckoutError):
    """Another task currently holds this repository's write lease."""


class RepoLeaseNotHeld(DirectCheckoutError):
    """The caller attempted a baseline operation without the matching lease."""


class RepoLeaseCorrupt(DirectCheckoutError):
    """A lease record is unreadable or does not match its repository key."""


class DirtyBaselineError(DirectCheckoutError):
    """A task cannot start because the direct checkout is already dirty."""

    def __init__(self, paths):
        self.paths = tuple(paths)
        detail = ", ".join(self.paths[:8]) or "无法枚举变更路径"
        if len(self.paths) > 8:
            detail += f" 等 {len(self.paths)} 项"
        super().__init__(f"direct checkout 基线必须干净；发现已有改动：{detail}")


class BaselineViolation(DirectCheckoutError):
    """A validation report contains branch, HEAD, or scope conflicts."""

    def __init__(self, report):
        self.report = report
        details = list(report.baseline_mismatches)
        if report.out_of_scope:
            details.append("越界路径: " + ", ".join(report.out_of_scope[:8]))
        super().__init__("direct checkout 基线校验失败；" + "；".join(details))


def _git_env():
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # Do not let ambient repository-routing variables redirect a safety check.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_PREFIX", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_CEILING_DIRECTORIES"):
        env.pop(key, None)
    return env


def _git(args, cwd, *, check=True):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_git_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if check and result.returncode:
        message = os.fsdecode(result.stderr).strip() or "git 命令失败"
        raise RepositoryError(message)
    return result


def _discover_repo(path):
    candidate = Path(path).expanduser()
    if not candidate.exists() or not candidate.is_dir():
        raise RepositoryError(f"仓库目录不存在：{candidate}")
    result = _git(["rev-parse", "--show-toplevel"], candidate, check=False)
    if result.returncode:
        raise RepositoryError(f"不是 Git 工作区：{candidate}")
    root = Path(os.fsdecode(result.stdout).strip()).resolve()
    common = _git(["rev-parse", "--git-common-dir"], root)
    common_dir = Path(os.fsdecode(common.stdout).strip())
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    common_dir = common_dir.resolve()
    key = hashlib.sha256(os.fsencode(str(common_dir))).hexdigest()
    return root, common_dir, key


def _branch_and_head(root):
    branch_result = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], root, check=False)
    if branch_result.returncode:
        raise RepositoryError("direct checkout 必须位于命名分支，不能使用 detached HEAD")
    branch = os.fsdecode(branch_result.stdout).strip()
    head = os.fsdecode(_git(["rev-parse", "--verify", "HEAD^{commit}"], root).stdout).strip()
    if not branch or not head:
        raise RepositoryError("无法读取当前分支或 HEAD")
    return branch, head


def _null_paths(data):
    return {os.fsdecode(raw) for raw in data.split(b"\0") if raw}


def _changed_paths(root):
    tracked = _git(["diff", "--no-renames", "--name-only", "-z", "HEAD", "--"], root)
    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"], root)
    return tuple(sorted(_null_paths(tracked.stdout) | _null_paths(untracked.stdout)))


def _normalize_allowed_paths(paths):
    if isinstance(paths, (str, bytes)):
        raise DirectCheckoutError("allowed_paths 必须是路径集合，不能是单个字符串")
    normalized = set()
    try:
        values = tuple(paths)
    except TypeError as exc:
        raise DirectCheckoutError("allowed_paths 必须可迭代") from exc
    for raw in values:
        if not isinstance(raw, str) or not raw:
            raise DirectCheckoutError("声明路径必须是非空相对路径")
        if "\\" in raw or raw.startswith("/") or ntpath.splitdrive(raw)[0]:
            raise DirectCheckoutError(f"声明路径必须是仓库内相对路径：{raw!r}")
        if any(char in raw for char in "*?[]"):
            raise DirectCheckoutError(f"声明路径不支持通配符：{raw!r}")
        parts = [part for part in raw.split("/") if part not in ("", ".")]
        if not parts or ".." in parts:
            raise DirectCheckoutError(f"声明路径不能指向仓库外或仓库根目录：{raw!r}")
        canonical = "/".join(parts)
        if canonical == ".git" or canonical.startswith(".git/"):
            raise DirectCheckoutError("任务声明路径不能包含 .git 元数据")
        normalized.add(canonical)
    if not normalized:
        raise DirectCheckoutError("至少声明一个允许修改的仓库相对路径")
    return tuple(sorted(normalized))


def _within_scope(path, allowed_paths):
    return any(path == allowed or path.startswith(allowed + "/") for allowed in allowed_paths)


def _utc_now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


def _runtime_root(explicit=None):
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    configured = os.environ.get("AISKHUB_RUNTIME_ROOT") or os.environ.get("AISK_HOME")
    return Path(configured or (Path.home() / ".aisk-runtime")).expanduser().resolve()


@contextmanager
def _record_guard(path, repo_name):
    context = file_lock(path, blocking=False)
    if not context.__enter__():
        context.__exit__(None, None, None)
        raise RepoLeaseBusy(f"仓库租约记录正在更新：{repo_name}")
    try:
        yield
    finally:
        context.__exit__(None, None, None)


class RepoLease:
    """Persistent exclusive writer lease for one local Git repository.

    The stable lease record is keyed by Git's common directory, so linked
    checkouts share one lease. A short OS lock protects each record operation;
    process exit does not release the lease. Only the matching owner may
    heartbeat or release it. An old heartbeat never permits age-only takeover.
    """

    def __init__(self, repo, *, owner, runtime_root=None):
        if not isinstance(owner, str) or not owner.strip() or "\n" in owner or "\r" in owner:
            raise DirectCheckoutError("lease owner 必须是非空单行标识")
        self.repo_root, self.common_dir, self.repository_key = _discover_repo(repo)
        lock_root = _runtime_root(runtime_root) / "locks" / "direct-checkout"
        self.lock_path = lock_root / f"{self.repository_key}.lock"
        self.record_path = lock_root / f"{self.repository_key}.json"
        self.owner = owner.strip()
        self._context = None
        self._held = False
        self._metadata = None

    @property
    def held(self):
        return self._held

    @property
    def metadata(self):
        return dict(self._metadata) if self._metadata else None

    def _read_record(self):
        if not self.record_path.exists():
            return None
        try:
            record = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RepoLeaseCorrupt("仓库 lease 记录不可读取；拒绝自动接管") from exc
        required = ("repository_key", "repo_root", "common_dir", "owner", "created_at", "heartbeat_at")
        if (not isinstance(record, dict) or record.get("version") != 1
                or any(not isinstance(record.get(key), str) or not record[key] for key in required)
                or record.get("repository_key") != self.repository_key
                or record.get("common_dir") != str(self.common_dir)):
            raise RepoLeaseCorrupt("仓库 lease 记录格式或仓库身份不匹配；拒绝自动接管")
        record.setdefault("heartbeat_count", 0)
        if not isinstance(record["heartbeat_count"], int) or record["heartbeat_count"] < 0:
            raise RepoLeaseCorrupt("仓库 lease heartbeat 计数无效；拒绝自动接管")
        return record

    def _assert_owner_record(self, owner):
        if not self._held:
            raise RepoLeaseNotHeld("必须先在当前 CLI 操作中获取仓库 OS 互斥锁")
        if owner != self.owner:
            raise RepoLeaseNotHeld("lease owner 不匹配")
        record = self._read_record()
        if record is None or record["owner"] != owner:
            raise RepoLeaseNotHeld("仓库 lease 已不存在或 owner 已变化")
        if record["repo_root"] != str(self.repo_root):
            raise RepoLeaseNotHeld("仓库 lease checkout 路径与当前仓库不匹配")
        return record

    def acquire(self):
        if self._held:
            return self
        context = file_lock(self.lock_path, blocking=False)
        if not context.__enter__():
            context.__exit__(None, None, None)
            raise RepoLeaseBusy(f"仓库租约记录正在更新：{self.repo_root.name}")
        self._context = context
        self._held = True
        try:
            record = self._read_record()
            if record is not None:
                if record["owner"] != self.owner:
                    raise RepoLeaseBusy(
                        f"仓库写租约由 {record['owner']} 持有（heartbeat {record['heartbeat_at']}）；"
                        "不会按超时自动接管"
                    )
                if record["repo_root"] != str(self.repo_root):
                    raise RepoLeaseCorrupt("lease owner 相同但 checkout 路径不同；拒绝接管")
                record["heartbeat_at"] = _utc_now()
                record["heartbeat_count"] += 1
            else:
                now = _utc_now()
                record = {
                    "version": 1,
                    "repository_key": self.repository_key,
                    "repo_root": str(self.repo_root),
                    "common_dir": str(self.common_dir),
                    "owner": self.owner,
                    "created_at": now,
                    "heartbeat_at": now,
                    "heartbeat_count": 0,
                }
            atomic_json(self.record_path, record)
            self._metadata = dict(record)
        except Exception:
            self.close()
            raise
        return self

    def heartbeat(self, owner):
        """Refresh heartbeat metadata only when the caller names this owner."""
        if owner != self.owner:
            raise RepoLeaseNotHeld("只有 lease owner 才能续租")
        record = self._assert_owner_record(owner)
        record["heartbeat_at"] = _utc_now()
        record["heartbeat_count"] += 1
        atomic_json(self.record_path, record)
        self._metadata = dict(record)
        return self.metadata

    def release(self, owner):
        """Remove the persistent lease after an explicit owner-matched release."""
        if owner != self.owner:
            raise RepoLeaseNotHeld("只有 lease owner 才能释放仓库写租约")
        record = self._assert_owner_record(owner)
        try:
            self.record_path.unlink()
        except OSError as exc:
            raise DirectCheckoutError(f"无法释放仓库写租约：{exc}") from exc
        self._metadata = dict(record)
        return dict(record)

    def close(self):
        """Release this CLI operation's OS mutex but retain its task lease."""
        if self._context is not None:
            context, self._context = self._context, None
            self._held = False
            context.__exit__(None, None, None)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        # The OS mutex is short-lived; explicit release(owner) removes the lease record.
        self.close()
        return False

    def _assert_matches(self, root, common_dir, key, owner):
        if not self._held:
            raise RepoLeaseNotHeld("必须先持有 direct checkout 仓库写租约")
        if self.repository_key != key or self.common_dir != common_dir:
            raise RepoLeaseNotHeld("写租约与当前仓库不匹配")
        if self.repo_root != root:
            raise RepoLeaseNotHeld("写租约与当前 checkout 路径不匹配")
        if self.owner != owner:
            raise RepoLeaseNotHeld("写租约 owner 与基线记录不匹配")
        self._assert_owner_record(owner)


def acquire_repo_lease(repo, *, owner, runtime_root=None):
    """Create or resume a persistent lease for this exact task owner.

    Other owners are rejected even when the stored heartbeat is old. The same
    owner can resume from another CLI process. Use the returned object as a
    context manager for one CLI operation, then explicitly call
    lease.release(owner) inside a later operation when the task is complete.
    """
    return RepoLease(repo, owner=owner, runtime_root=runtime_root).acquire()


def recover_repo_lease(repo, *, expected_owner, reason, runtime_root=None):
    """Explicitly clear one recorded owner after an operator-approved recovery.

    No heartbeat age is interpreted here. Callers must supply the exact owner
    they intend to clear and a non-empty recovery reason; normal task flows must
    use owner-matched release instead.
    """
    if not isinstance(expected_owner, str) or not expected_owner.strip():
        raise DirectCheckoutError("恢复 lease 必须指定 expected_owner")
    if not isinstance(reason, str) or not reason.strip():
        raise DirectCheckoutError("恢复 lease 必须记录 reason")
    lease = RepoLease(repo, owner=expected_owner, runtime_root=runtime_root)
    with _record_guard(lease.lock_path, lease.repo_root.name):
        record = lease._read_record()
        if record is None:
            raise RepoLeaseNotHeld("仓库没有待恢复的 lease")
        if record["owner"] != expected_owner.strip():
            raise RepoLeaseNotHeld("expected_owner 与当前 lease 不匹配")
        if record["repo_root"] != str(lease.repo_root):
            raise RepoLeaseNotHeld("lease checkout 路径与恢复目标不匹配")
        try:
            lease.record_path.unlink()
        except OSError as exc:
            raise DirectCheckoutError(f"无法恢复仓库写租约：{exc}") from exc
        return {**record, "recovery_reason": reason.strip(), "recovered_at": _utc_now()}


@dataclass(frozen=True)
class RepoBaseline:
    repo_root: str
    common_dir: str
    repository_key: str
    branch: str
    head: str
    allowed_paths: tuple[str, ...]
    lease_owner: str
    captured_at: str
    version: int = 1

    def to_dict(self):
        return {
            "version": self.version,
            "repo_root": self.repo_root,
            "common_dir": self.common_dir,
            "repository_key": self.repository_key,
            "branch": self.branch,
            "head": self.head,
            "allowed_paths": list(self.allowed_paths),
            "lease_owner": self.lease_owner,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.get("version") != 1:
            raise DirectCheckoutError("direct checkout 基线格式不受支持")
        try:
            allowed = _normalize_allowed_paths(value["allowed_paths"])
            fields = ("repo_root", "common_dir", "repository_key", "branch", "head", "lease_owner", "captured_at")
            parts = [value[name] for name in fields]
        except (KeyError, TypeError) as exc:
            raise DirectCheckoutError("direct checkout 基线字段不完整") from exc
        if any(not isinstance(part, str) or not part for part in parts):
            raise DirectCheckoutError("direct checkout 基线包含空字段")
        return cls(*parts[:5], allowed, *parts[5:], version=1)


@dataclass(frozen=True)
class ChangeValidation:
    baseline_branch: str
    current_branch: str
    baseline_head: str
    current_head: str
    changed_paths: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    baseline_mismatches: tuple[str, ...]

    @property
    def ok(self):
        return not self.out_of_scope and not self.baseline_mismatches

    def raise_if_invalid(self):
        if not self.ok:
            raise BaselineViolation(self)
        return self


def capture_baseline(repo, allowed_paths: Iterable[str], *, lease: RepoLease) -> RepoBaseline:
    """Capture a clean branch/HEAD baseline while holding this task's lease."""
    root, common_dir, key = _discover_repo(repo)
    if not isinstance(lease, RepoLease):
        raise RepoLeaseNotHeld("capture_baseline 必须提供已获取的 RepoLease")
    lease._assert_matches(root, common_dir, key, lease.owner)
    paths = _normalize_allowed_paths(allowed_paths)
    status = _git(["status", "--porcelain=v1", "--untracked-files=all", "-z"], root)
    if status.stdout:
        raise DirtyBaselineError(_changed_paths(root))
    branch, head = _branch_and_head(root)
    return RepoBaseline(
        repo_root=str(root), common_dir=str(common_dir), repository_key=key,
        branch=branch, head=head, allowed_paths=paths, lease_owner=lease.owner,
        captured_at=_utc_now(),
    )


def validate_task_changes(repo, baseline: RepoBaseline | dict, *, lease: RepoLease) -> ChangeValidation:
    """Read current state and report baseline drift or Git-visible scope escapes.

    This function never changes files, refs, the index, or task state. It checks
    tracked staged/unstaged changes and non-ignored untracked paths. A lease
    serializes cooperating Aisk writers; changes made by an actor that bypasses
    the lease in an already-allowed file cannot be attributed by Git alone.
    """
    if isinstance(baseline, dict):
        baseline = RepoBaseline.from_dict(baseline)
    if not isinstance(baseline, RepoBaseline):
        raise DirectCheckoutError("baseline 必须是 RepoBaseline 或其字典表示")
    root, common_dir, key = _discover_repo(repo)
    if not isinstance(lease, RepoLease):
        raise RepoLeaseNotHeld("validate_task_changes 必须提供 RepoLease")
    lease._assert_matches(root, common_dir, key, baseline.lease_owner)

    mismatches = []
    if str(root) != baseline.repo_root:
        mismatches.append("checkout 路径已变化")
    if str(common_dir) != baseline.common_dir or key != baseline.repository_key:
        mismatches.append("Git common-dir 已变化")
    branch, head = _branch_and_head(root)
    if branch != baseline.branch:
        mismatches.append(f"当前分支从 {baseline.branch} 变为 {branch}")
    if head != baseline.head:
        mismatches.append(f"当前 HEAD 从 {baseline.head[:12]} 变为 {head[:12]}")

    changed = _changed_paths(root)
    outside = tuple(path for path in changed if not _within_scope(path, baseline.allowed_paths))
    return ChangeValidation(
        baseline_branch=baseline.branch,
        current_branch=branch,
        baseline_head=baseline.head,
        current_head=head,
        changed_paths=changed,
        out_of_scope=outside,
        baseline_mismatches=tuple(mismatches),
    )
