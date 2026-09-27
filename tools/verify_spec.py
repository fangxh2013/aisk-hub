#!/usr/bin/env python3
"""Executable offline verifier for aisk-hub's 99.9+ contracts."""
from __future__ import annotations

import copy
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "agent-skills"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from engine import contracts, mcp_contract, miniyaml, privacy, token_efficiency  # noqa: E402
from engine.coordination_store import CoordinationError, CoordinationStore  # noqa: E402


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def input_digest(root):
    root = Path(root)
    digest = hashlib.sha256()
    for base in ("spec", "agent-skills/engine", "agent-skills/skills", "agent-skills/tests", "tools"):
        path = root / base
        if not path.exists():
            continue
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        for item in sorted(files):
            if "__pycache__" in item.parts or item.suffix == ".pyc":
                continue
            digest.update(item.relative_to(root).as_posix().encode() + b"\0")
            digest.update(item.read_bytes() + b"\0")
    return digest.hexdigest()


def tree_digest(path):
    path = Path(path)
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    for item in sorted(files):
        if "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        digest.update(item.relative_to(path).as_posix().encode() + b"\0")
        digest.update(item.read_bytes() + b"\0")
    return digest.hexdigest()


def git(root, *args):
    try:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_check(check_id, fn, command="python3 tools/verify_spec.py"):
    started = time.monotonic()
    try:
        details = fn()
        passed, exit_code = True, 0
    except Exception as exc:  # noqa: BLE001
        details, passed, exit_code = f"{type(exc).__name__}: {exc}", False, 1
    return {
        "check_id": check_id,
        "command": command,
        "exit_code": exit_code,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "stdout_digest": "",
        "stderr_digest": "",
        "passed": passed,
        "details": str(details),
    }


def run_process_check(check_id, command, root, timeout=60):
    started = time.monotonic()
    try:
        proc = subprocess.run(command, cwd=str(root), env={**__import__("os").environ, "PYTHONPATH": "agent-skills"},
                              capture_output=True, text=True, timeout=timeout)
        passed = proc.returncode == 0
        details = (proc.stdout or proc.stderr or "").strip()[-1000:] or "completed"
        exit_code = proc.returncode
        stdout_digest = hashlib.sha256((proc.stdout or "").encode()).hexdigest()
        stderr_digest = hashlib.sha256((proc.stderr or "").encode()).hexdigest()
    except subprocess.TimeoutExpired as exc:
        passed, details, exit_code = False, f"timeout after {timeout}s", 124
        stdout_digest = hashlib.sha256((exc.stdout or "").encode() if isinstance(exc.stdout, str) else b"").hexdigest()
        stderr_digest = hashlib.sha256((exc.stderr or "").encode() if isinstance(exc.stderr, str) else b"").hexdigest()
    return {
        "check_id": check_id,
        "command": " ".join(command),
        "exit_code": exit_code,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "stdout_digest": stdout_digest,
        "stderr_digest": stderr_digest,
        "passed": passed,
        "details": details,
    }


def load_manifest_file(path):
    path = Path(path)
    if not path.is_file():
        raise contracts.ContractError(f"真实 Overlay 文件不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.suffix.lower() == ".json" else miniyaml.load_file(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise contracts.ContractError(f"真实 Overlay 文件无法解析: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise contracts.ContractError("真实 Overlay 顶层必须是对象")
    return data


def load_lock_file(path):
    path = Path(path)
    if not path.is_file():
        raise contracts.ContractError(f"Overlay lock 文件不存在: {path}")
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = miniyaml.load_file(path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise contracts.ContractError(f"Overlay lock 文件无法解析: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise contracts.ContractError("Overlay lock 顶层必须是对象")
    return data


def validate_overlay_lock(manifest, lock=None, *, embedded=False, require_commit=True):
    """Validate the one supported real-v2 arrangement: an external lock file."""
    contracts.validate_overlay(manifest)
    digest = str(manifest.get("public_kernel", {}).get("content_digest", ""))
    if digest == "<placeholder>" or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise contracts.ContractError("真实 Overlay 禁止使用 <placeholder>，必须有 sha256 content_digest")
    if embedded:
        raise contracts.ContractError("真实 Overlay 只支持 external lock，不支持嵌入式 lock")
    if not isinstance(manifest.get("manifest_id"), str) or not manifest["manifest_id"].strip():
        raise contracts.ContractError("真实 Overlay 必须提供 manifest_id")
    if not isinstance(lock, dict):
        raise contracts.ContractError("真实 Overlay 必须提供 --overlay-lock 外置 lock")
    version = lock.get("lock_version", lock.get("lockfile_version"))
    if version != 1:
        raise contracts.ContractError("Overlay lock_version 必须为 1")
    if lock.get("manifest_id") != manifest["manifest_id"]:
        raise contracts.ContractError("Overlay lock.manifest_id 与 manifest_id 不一致")
    lock_digest = lock.get("overlay_digest")
    if not isinstance(lock_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", lock_digest):
        raise contracts.ContractError("Overlay lock 必须提供 sha256 overlay_digest")
    expected = contracts.overlay_digest(manifest)
    if lock_digest != expected:
        raise contracts.ContractError("Overlay lock.overlay_digest 与 manifest 内容不一致")
    kernel_digest = str(lock.get("kernel_content_digest", ""))
    if not re.fullmatch(r"[a-f0-9]{64}", kernel_digest):
        raise contracts.ContractError("Overlay lock 必须提供 sha256 kernel_content_digest")
    if kernel_digest != digest:
        raise contracts.ContractError("Overlay lock.kernel_content_digest 与 manifest.public_kernel.content_digest 不一致")
    commit = str(lock.get("kernel_commit", ""))
    if require_commit and not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise contracts.ContractError("真实 Overlay lock 必须绑定 40 位 kernel_commit")
    if not isinstance(lock.get("generated_at"), str) or not lock["generated_at"].strip():
        raise contracts.ContractError("Overlay lock 必须提供 generated_at")
    return {"manifest_digest": expected, "kernel_commit": commit}


def kernel_archive_digest(root, commit):
    """`git archive` 出 <commit> 的内容摘要；取不到（提交不在本仓库）返回 None。"""
    try:
        proc = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", "--prefix=", commit],
                              capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


def validate_kernel_pin(root, lock, *, strict):
    """lock 声明的内核 commit 与摘要必须对得上本仓库，否则这枚 pin 只是装饰。

    只校验 40 位格式时，一份钉着上个月提交的 lock 也能拿满分——而真正会被分发的
    是工作区里的这份内核。这里落到内容摘要，并如实报告 pin 与 HEAD 的漂移。
    """
    commit = str(lock.get("kernel_commit", ""))
    recorded = str(lock.get("kernel_content_digest", ""))
    actual = kernel_archive_digest(root, commit)
    if actual is None:
        raise contracts.ContractError(
            f"本仓库取不到 lock 钉住的内核提交 {commit[:12]}；没核对过就不能分发")
    if actual != recorded:
        raise contracts.ContractError(
            f"lock.kernel_content_digest 与 {commit[:12]} 的实际归档摘要不符（实际 {actual[:12]}）")
    head = git(root, "rev-parse", "HEAD") or ""
    pin = {"kernel_commit": commit, "head": head,
           "status": "matched" if head == commit else "drifted"}
    if pin["status"] == "drifted" and strict:
        raise contracts.ContractError(
            f"要分发的内核是 {head[:12]}，Overlay lock 钉的是 {commit[:12]}；"
            "请对当前内核重算 kernel_commit 与 kernel_content_digest 后再分发")
    return pin


def validate_real_overlay(manifest_path, lock_path=None, kernel_root=None, *, strict=False, pin_out=None):
    manifest = load_manifest_file(manifest_path)
    if not lock_path:
        raise contracts.ContractError("真实 Overlay 校验必须显式提供 --overlay-lock")
    lock = load_lock_file(lock_path)
    result = validate_overlay_lock(manifest, lock)
    pin = {"status": "unchecked"}
    if kernel_root is not None:
        pin = validate_kernel_pin(kernel_root, lock, strict=strict)
    if pin_out is not None:
        pin_out.clear()
        pin_out.update(pin)
    note = {
        "matched": "内核 pin 与工作区 HEAD 一致",
        "drifted": f"内核 pin 已漂移：HEAD {pin.get('head', '')[:12]} ≠ pin {pin.get('kernel_commit', '')[:12]}",
        "unchecked": "未核对内核 pin",
    }[pin["status"]]
    return f"真实 Overlay v2 通过，manifest_digest={result['manifest_digest']}；{note}"


def validate_skill_map_contract(root):
    data = contracts.load_yaml(root, "skill-migration-map.yaml")
    skills = data.get("skills")
    contract = data.get("migration_contract") or {}
    if not isinstance(skills, dict) or len(skills) != int(contract.get("canonical_count", 0)):
        raise contracts.ContractError("canonical 技能数量不是 9")
    legacy = []
    for target, entry in skills.items():
        legacy.extend(entry.get("legacy_sources", []))
        for alias in entry.get("legacy_sources", []):
            if contracts.resolve_skill_alias(root, alias) != target:
                raise contracts.ContractError(f"legacy alias 未能解析: {alias} -> {target}")
    expected = int(contract.get("legacy_alias_count", 0))
    if len(legacy) != expected or len(set(legacy)) != expected:
        raise contracts.ContractError(f"legacy alias 数量不符合 26: {len(legacy)}")
    for alias in contract.get("compatibility_aliases", []):
        contracts.resolve_skill_alias(root, alias)
    return f"{len(skills)} 个 canonical、{len(legacy)} 个 legacy alias、兼容别名解析通过"


def validate_adapter_contract(data):
    contracts.validate_adapter(data)
    actors = data.get("actors", {})
    required = set((data.get("contract") or {}).get("supported_tools", []))
    for tool in required:
        entry = actors.get(tool)
        if not isinstance(entry, dict):
            raise contracts.ContractError(f"缺少适配器: {tool}")
        for field in ("install_target", "session_sources", "dialog", "degradation"):
            if field not in entry:
                raise contracts.ContractError(f"adapter.{tool} 缺少 {field}")
        dialog = entry["dialog"]
        if dialog.get("title_format") != "{action}-{tool}｜{task_id}":
            raise contracts.ContractError(f"adapter.{tool} 弹窗标题契约不一致")
        if dialog.get("confirmation") != "fail_closed":
            raise contracts.ContractError(f"adapter.{tool} 必须 fail-closed")
        if not entry["install_target"].get("project_hooks"):
            raise contracts.ContractError(f"adapter.{tool} 缺少安装目标")
        if not entry["session_sources"]:
            raise contracts.ContractError(f"adapter.{tool} 缺少会话来源")
    protocol = data.get("dialog_protocol", {})
    if protocol.get("title_format") != "{action}-{tool}｜{task_id}":
        raise contracts.ContractError("公共弹窗标题必须为动作-工具｜任务号")
    if protocol.get("platform_backends") != {"macos": "osascript", "windows": "taskdialog_comctl32"}:
        # 与 engine/worktree/registry.py 的 confirm_human 对齐：Windows 用 ctypes 调 comctl32 的 TaskDialog。
        raise contracts.ContractError("公共弹窗必须声明 macOS osascript 与 Windows TaskDialog（comctl32）后端")
    return f"{len(actors)} 个 actor；四工具安装目标、会话、标题和降级策略通过"


def repo_state(root):
    """验收开始时的提交号与工作树状态。

    必须在写任何报告之前取一次：报告目录默认就在仓库里，边写边查会把本次运行自己刚写出的
    报告算成「工作树有改动」，干净的检出也会被记成 worktree_dirty。
    """
    return {"git_commit": git(root, "rev-parse", "HEAD"), "worktree_dirty": bool(git(root, "status", "--porcelain"))}


def make_report(report_id, report_type, checks, root, command="python3 tools/verify_spec.py", *,
                validation_scope="offline_contract", real_file_checked=False, git_state=None):
    state = git_state or repo_state(root)
    report = {
        "report_id": report_id,
        "report_type": report_type,
        "run_id": str(uuid.uuid4()),
        "git_commit": state["git_commit"],
        "worktree_dirty": state["worktree_dirty"],
        "command": command,
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "duration_ms": round(sum(float(c["duration_ms"]) for c in checks), 3),
        "offline_validated": True,
        "passed": all(c["passed"] for c in checks),
        "score": 100.0 if all(c["passed"] for c in checks) else 0.0,
        "checks": checks,
        "input_digest": input_digest(root),
        "spec_digest": tree_digest(Path(root) / "spec"),
        "content_digest": "",
        "validation_scope": validation_scope,
        "real_file_checked": real_file_checked,
    }
    content = json.dumps({k: v for k, v in report.items() if k != "content_digest"}, ensure_ascii=False, sort_keys=True).encode()
    report["content_digest"] = hashlib.sha256(content).hexdigest()
    return report


def skill_checks(root):
    def aliases():
        return validate_skill_map_contract(root)
    return [run_check("legacy_skill_coverage_and_alias", aliases)]


def overlay_checks(root):
    def valid_and_rejects():
        sample = {
            "version": 2, "type": "aisk-private-overlay",
            "public_kernel": {"repository": "local/aisk-hub", "required_version": ">=2.0.0", "content_digest": "<placeholder>"},
            "profiles": ["profiles/example.yaml"], "project_rules": ["rules/example.md"],
            "custom_skills": ["skills/private-skill"],
            "skill_slots": {"backend-engineering": {"module_prefix": "example-modules"}},
        }
        contracts.validate_overlay(sample)
        bad = copy.deepcopy(sample)
        bad["skill_slots"]["malicious_slot"] = {"exec": "sh -c evil"}
        try:
            contracts.validate_overlay(bad)
        except contracts.ContractError:
            locked = copy.deepcopy(sample)
            locked["manifest_id"] = "example-overlay"
            locked["public_kernel"]["content_digest"] = "a" * 64
            external_lock = {
                "lock_version": 1,
                "manifest_id": "example-overlay",
                "overlay_digest": contracts.overlay_digest(locked),
                "kernel_commit": "b" * 40,
                "kernel_content_digest": "a" * 64,
                "generated_at": "2026-01-01T00:00:00Z",
            }
            validate_overlay_lock(locked, external_lock)
            mismatched = dict(external_lock, kernel_content_digest="c" * 64)
            try:
                validate_overlay_lock(locked, mismatched)
            except contracts.ContractError:
                return "兼容 Overlay 通过；未知插槽、可执行注入、错误 lock 与对不上的内核摘要被拒绝"
            raise AssertionError("内核摘要对不上的 lock 未被拒绝")
        raise AssertionError("非法 Overlay 未拒绝")

    def placeholder_is_not_real():
        with tempfile.TemporaryDirectory(prefix="aisk-overlay-") as tmp:
            path = Path(tmp) / "OVERLAY_MANIFEST.json"
            path.write_text(json.dumps({
                "version": 2,
                "type": "aisk-private-overlay",
                "public_kernel": {
                    "repository": "local/aisk-hub",
                    "required_version": ">=2.0.0",
                    "content_digest": "<placeholder>",
                },
                "skill_slots": {},
            }), encoding="utf-8")
            try:
                validate_real_overlay(path)
            except contracts.ContractError:
                return "placeholder 仅可用于合成样例，真实 Overlay 检查会拒绝"
        raise AssertionError("placeholder 被当成真实 Overlay")

    return [run_check("overlay_schema_and_injection_guard", valid_and_rejects),
            run_check("placeholder_not_real_manifest", placeholder_is_not_real)]


def adapter_checks(root):
    def adapter():
        return validate_adapter_contract(contracts.load_yaml(root, "adapter-capabilities.yaml"))

    def registry_parity():
        """能力矩阵必须与代码里的工具表、权限处理器四方对齐。

        `validate_adapter` 只断言 spec 自洽（actor 集合、必填字段、标题含 {tool}/{task_id}），
        它**从不校验能力值的真假**，也看不见「spec 声明了某一端、代码却不认」这种漂移。
        这一条补上那半个面：spec / actor.py / action_context.py / permissions.py 必须说同一批端。
        """
        from engine import permissions
        from engine.action_context import TOOLS as DIALOG_TOOLS
        from engine.worktree.actor import TOOLS as ACTOR_TOOLS

        spec_actors = set(contracts.load_yaml(root, "adapter-capabilities.yaml")["actors"])
        if spec_actors != set(contracts.ACTORS):
            raise AssertionError(f"spec actor 与契约常量不一致: {sorted(spec_actors ^ set(contracts.ACTORS))}")
        for name, table in (("worktree/actor.py", set(ACTOR_TOOLS)),
                            ("action_context.py", set(DIALOG_TOOLS))):
            if table != spec_actors:
                raise AssertionError(f"spec 与 {name} 的工具表不一致: {sorted(table ^ spec_actors)}")

        # 除 human（操作者，不是可配置的 AI 端）外，每个 actor 都必须在权限层登记。
        # 登记成 unsupported 也算登记——它的意义是让 `aisk permit` 如实报「已知但未支持」，
        # 而不是把这一端凭空消失成「未知端」被误读为已覆盖。
        missing = (spec_actors - {"human"}) - set(permissions.HANDLERS)
        if missing:
            raise AssertionError(f"权限层缺少处理器登记: {sorted(missing)}")

        # WorkBuddy 桌面版与 AI 版跑同一个宿主引擎、共用同一份 user 作用域配置，
        # 所以能力结论必须逐项一致，只允许 runtime_type 区分壳。任何一项漂移都意味着
        # 「两端结论相同」这个前提在某一处已经不成立，必须显式暴露。
        actors = contracts.load_yaml(root, "adapter-capabilities.yaml")["actors"]
        desk, ai = actors["workbuddy"], actors["workbuddy-ai"]
        if desk["capabilities"] != ai["capabilities"]:
            raise AssertionError(f"WorkBuddy 两端能力矩阵漂移: {desk['capabilities']} vs {ai['capabilities']}")
        if desk["trust_level"] != ai["trust_level"]:
            raise AssertionError(f"WorkBuddy 两端信任级别漂移: {desk['trust_level']} vs {ai['trust_level']}")
        if desk["runtime_type"] == ai["runtime_type"]:
            raise AssertionError("WorkBuddy 两端 runtime_type 应当区分壳（desktop_agent / desktop_agent_sub）")
        return "spec / actor / action_context / permissions 四方工具表一致，WorkBuddy 两端结论对齐"

    return [run_check("adapter_capabilities_and_dialog", adapter),
            run_check("adapter_registry_parity", registry_parity)]


def coordination_checks(root):
    def state_store():
        with tempfile.TemporaryDirectory(prefix="aisk-state-") as tmp:
            store = CoordinationStore(Path(tmp) / "state.db")
            actor = {"tool": "codex", "session_id": "s-1", "model": "test"}
            store.create("T900", actor, {"title": "contract", "repo_alias": "backend", "target_branch": "master"})
            claimed = store.claim("T900", actor, lease_ttl=60, payload={"worktree_path": tmp})
            token = claimed["fencing_token"]
            try:
                store.heartbeat("T900", {"tool": "claude", "session_id": "s-2"}, token)
            except CoordinationError:
                pass
            else:
                raise AssertionError("错误 actor 被允许续租")
            try:
                store.heartbeat("T900", actor, token - 1)
            except CoordinationError:
                pass
            else:
                raise AssertionError("旧 fencing token 被允许写入")
            if len(store.pending_events()) != 2:
                raise AssertionError("状态与 outbox 未同事务提交")
            events = Path(tmp) / "events.jsonl"
            if store.export_jsonl(events) != 2 or len(events.read_text(encoding="utf-8").splitlines()) != 2:
                raise AssertionError("outbox 导出失败")
            store.close()
        return "SQLite CAS、actor 隔离、stale fencing 拒绝、事务 Outbox 导出通过"
    return [run_check("coordination_cas_fencing_outbox", state_store),
            run_check("coordination_schema_consistency", lambda: contracts.validate_coordination_specs(root))]


def mcp_checks(root):
    def responses():
        snapshot = mcp_contract.response(ok=True, source="local-manifest", data={"service": "demo"}, availability="offline_snapshot")
        contracts.validate_mcp_response(snapshot)
        unavailable = json.loads(mcp_contract.offline_database_unavailable())
        mcp_contract.validate_no_fake_database_result(unavailable)
        try:
            mcp_contract.validate_no_fake_database_result({"source": "database-broker", "data_availability": "offline_snapshot", "ok": True, "data": {"rows": 1}})
        except contracts.ContractError:
            return "快照事实可用；离线数据库结果伪造被拒绝"
        raise AssertionError("离线数据库结果未拒绝")
    return [run_check("offline_mcp_response_semantics", responses),
            run_check("mcp_spec_states", lambda: contracts.validate_mcp_response({"ok": False, "source": "x", "data_availability": "offline_unavailable", "as_of": now_iso(), "is_live": False, "redacted": True, "data": None}))]


def privacy_checks(root):
    def scan():
        report = privacy.verify_report(root)
        if not report["ok"]:
            raise AssertionError(report["findings"][:10])
        detail = "公开 allowlist 与当前可达 Git 历史扫描通过"
        accepted, stale = report["accepted_history"], report["stale_history_baseline"]
        if accepted:
            # 放行不等于隐藏：已登记的历史债务条数写进证据，任何人都能看到它仍在已发布历史里。
            detail += f"；已登记历史债务 {len(accepted)} 条按 {privacy.BASELINE_RELATIVE} 放行（仅全历史扫描）"
        if stale:
            detail += f"；基线中 {len(stale)} 条已不再命中，可删除"
        return detail
    return [run_check("public_privacy_verify", scan)]


def syntax_checks(root):
    def syntax():
        for path in sorted((Path(root) / "spec").glob("*.json")):
            json.loads(path.read_text(encoding="utf-8"))
        return "所有 JSON Schema 可解析"
    return [run_check("spec_json_syntax", syntax)]


def regression_checks(root):
    return [
        run_process_check("contract_unit_tests", ["python3", "-m", "unittest", "discover", "-s", "agent-skills/tests", "-p", "test_contracts.py"], root),
        run_process_check("action_context_regression", ["python3", "-m", "unittest", "discover", "-s", "agent-skills/tests", "-p", "test_action_context.py"], root),
        run_process_check("token_efficiency_regression", ["python3", "-m", "unittest", "discover", "-s", "agent-skills/tests", "-p", "test_token_efficiency.py"], root),
    ]


def token_efficiency_checks(root):
    def audit():
        result = token_efficiency.audit(root)
        contracts.validate_token_audit(result)
        if not result["passed"]:
            raise AssertionError(result)
        return ("常驻技能、单技能任务上下文和重复指令比例均在预算内；"
                "安全/隐私/验证规则保留声明存在")
    return [run_check("token_context_budget_and_dedup", audit)]


def rollback_probe():
    with tempfile.TemporaryDirectory(prefix="aisk-rollback-") as tmp:
        store = CoordinationStore(Path(tmp) / "state.db")
        actor = {"tool": "human", "session_id": "rollback"}
        store.create("T901", actor, {"title": "rollback", "repo_alias": "backend", "target_branch": "master"})
        before = store.snapshot("T901")
        try:
            store.db.execute("BEGIN IMMEDIATE")
            store.db.execute("UPDATE tasks SET status='Broken' WHERE task_id='T901'")
            raise RuntimeError("forced rollback")
        except RuntimeError:
            store.db.execute("ROLLBACK")
        after = store.snapshot("T901")
        store.close()
        if before["status"] != after["status"] or before["version"] != after["version"]:
            raise AssertionError("事务回滚后状态发生变化")
    return "强制异常后的 SQLite ROLLBACK 保持基线一致"


def runtime_probe_env(args):
    """让探针与实际 aisk 启动器使用同一份 private/profile 解析结果。

    verifier 在独立 task worktree 中运行时，wrapper 的相邻目录不一定有
    aisk-private；显式传入 private manifest 就是无歧义的配置来源，不能再让
    探针回落到一个不存在的 profiles 目录。
    """
    env = os.environ.copy()
    if args.private_manifest:
        manifest = Path(args.private_manifest).expanduser().resolve()
        private_root = manifest.parent
        if manifest.name != "OVERLAY_MANIFEST.yaml":
            private_root = manifest.parent
        profile_dir = private_root / "profiles"
        if profile_dir.is_dir():
            env["AISK_PRIVATE_ROOT"] = str(private_root)
            env["AISKHUB_PRIVATE_ROOT"] = str(private_root)
            env["AISK_PROFILE_DIR"] = str(profile_dir)
            env["AISKHUB_PROFILE_DIR"] = str(profile_dir)
    env.setdefault("AISKHUB_RUNTIME_ROOT", str(Path.home() / ".aisk-runtime" / "hub"))
    env.setdefault("AISK_HOME", str(Path.home() / ".aisk"))
    return env


def run_local_smoke_probes(root, env=None):
    """本机冒烟探针：用真实启动器读本机档案、登记簿与部署仓，确认内核在这台机器上能跑。

    **它们不是真实客户端联调**：没有任何 AI 客户端加载技能，也没有弹出系统对话框
    （dry-run、别名路由与标题字符串比对，README 明确写着不能冒充实机通过）。所以通过
    只记为 local_smoke，永远不产生 real_runtime_score；失败仍让本次验收失败关闭。
    """
    probes = []

    def probe_link():
        res = subprocess.run([str(root / "bin" / "aisk"), "link", "--dry-run"], cwd=root, env=env,
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"aisk link --dry-run 失败: {res.stderr}")
        for tool in ("claude", "codex", "antigravity", "workbuddy", "workbuddy-ai"):
            if f"[dry-run] {tool}:" not in res.stdout:
                raise RuntimeError(f"缺少适配端分发输出: {tool}")
        return "默认五端分发 dry-run 输出完整（未写入任何端，未验证客户端加载）"
    probes.append(run_check("smoke_link_dry_run", probe_link, command="aisk link --dry-run"))

    def probe_board():
        # task 子命令必须显式绑定 aisk-hub profile；--brief 只读登记簿摘要，不扫描仓库。
        # 不断言某个具体任务号：任务归档后探针就会无故变红（曾经写死过 T003）。
        res = subprocess.run([str(root / "bin" / "aisk"), "--profile", "aisk-hub", "task", "status", "--brief"],
                             cwd=root, env=env, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"aisk task status 失败: {res.stderr}")
        rows = [line for line in res.stdout.splitlines() if line.strip()]
        malformed = [line for line in rows if line.count(" | ") < 5]
        if malformed:
            raise RuntimeError(f"任务摘要格式异常: {malformed[:3]}")
        return f"任务登记簿可读（aisk-hub 档案，{len(rows)} 条摘要）"
    probes.append(run_check("smoke_task_registry", probe_board, command="aisk --profile aisk-hub task status --brief"))

    def probe_fact():
        res = subprocess.run([str(root / "bin" / "aisk"), "--profile", "xinhua", "fact", "--env", "dev", "service", "goods"],
                             cwd=root, env=env, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"aisk fact 失败: {res.stderr}")
        if "service=goods" not in res.stdout or "nacos_dataid=" not in res.stdout:
            raise RuntimeError("未能正确提取部署清单事实")
        return "本机部署仓清单的服务事实解析通过（只读本地文件，未连接集群或配置中心）"
    probes.append(run_check("smoke_fact_discovery", probe_fact, command="aisk --profile xinhua fact --env dev service goods"))

    def probe_aliases():
        for legacy, target in (("dev-code", "backend-engineering"),
                               ("dev-web-code", "web-engineering"),
                               ("ops", "ops-workbench"),
                               ("database-design", "db-workbench")):
            resolved = contracts.resolve_skill_alias(root, legacy)
            if resolved != target:
                raise RuntimeError(f"别名解析异常: {legacy} -> {resolved} != {target}")
        return "旧技能逻辑别名路由到 canonical 领域技能"
    probes.append(run_check("smoke_alias_resolution", probe_aliases, command="aisk skill resolve <alias>"))

    def probe_dialog():
        from engine.worktree import integrate
        from types import SimpleNamespace
        args = SimpleNamespace(tool="workbuddy", session="s-1")
        ctx = integrate.action_context(args, "git推送", "T001", "摘要", repository="aisk-hub")
        if ctx.title != "git推送-workbuddy | T001 | aisk-hub":
            raise RuntimeError(f"WorkBuddy 弹窗标题不符合契约: {ctx.title}")
        if integrate._push_expect(args) != "确认推送":
            raise RuntimeError("WorkBuddy 确认动作不符合契约")
        return "WorkBuddy 确认框标题与确认动作的字符串契约通过（未弹出真实对话框）"
    probes.append(run_check("smoke_dialog_title_contract", probe_dialog, command="engine.action_context.ActionContext(...)"))

    return probes


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Verify aisk-hub offline contracts and optional real Overlay files")
    parser.add_argument("root", nargs="?", default=str(ROOT), help="public repository root")
    parser.add_argument("--private-manifest", help="private Overlay v2 manifest to validate; never copied or rewritten")
    parser.add_argument("--overlay-lock", help="external Overlay lock file paired with --private-manifest")
    parser.add_argument("--report-dir", help="where evidence reports are written (default: <root>/reports)")
    parser.add_argument("--no-report-files", action="store_true", help="print summary without writing evidence files")
    parser.add_argument("--require-real", action="store_true", help="return non-zero if no real manifest is supplied")
    parser.add_argument("--probe-runtime", action="store_true",
                        help="run local smoke probes with the real launcher and local profiles; a failure fails the run, "
                             "but passing never sets real_runtime_score (no AI client or real dialog is exercised)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = Path(args.root).resolve()
    state = repo_state(root)
    report_dir = Path(args.report_dir).expanduser() if args.report_dir else root / "reports"
    if not report_dir.is_absolute():
        report_dir = root / report_dir
    if not args.no_report_files:
        report_dir.mkdir(parents=True, exist_ok=True)

    def write_report(report, report_id):
        if args.no_report_files:
            return None
        path = report_dir / f"{report_id}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    groups = [
        ("1_skill_migration_report", "SkillMigrationValidation", skill_checks(root)),
        ("2_overlay_validation_report", "OverlaySchemaValidation", overlay_checks(root)),
        ("3_adapter_contract_report", "AdapterContractValidation", adapter_checks(root)),
        ("4_coordination_state_report", "CoordinationStateValidation", coordination_checks(root)),
        ("5_offline_mcp_safety_report", "McpOfflineSafetyValidation", mcp_checks(root)),
        ("6_privacy_scan_report", "PrivacyScanValidation", privacy_checks(root)),
        ("7_sandbox_regression_report", "SandboxContractRegression", syntax_checks(root) + regression_checks(root)),
        ("9_token_efficiency_report", "TokenEfficiencyValidation", token_efficiency_checks(root)),
    ]
    matrix = contracts.load_yaml(root, "acceptance-matrix.yaml")
    weights = {item["id"]: item["weight"] for item in matrix["reports"]}
    reports, total, all_passed = [], 0.0, True
    for report_id, report_type, checks in groups:
        report = make_report(report_id, report_type, checks, root, git_state=state)
        contracts.validate_report(report)
        path = write_report(report, report_id)
        weight = float(weights.get(report_id, 0))
        total += report["score"] * weight / 100.0
        all_passed = all_passed and report["passed"]
        reports.append({"id": report_id, "passed": report["passed"], "score": report["score"], "weight": weight,
                        "file": str(path.relative_to(root)) if path and path.is_relative_to(root) else str(path) if path else None})

    rollback = make_report("8_rollback_drill_report", "RollbackDrillValidation",
                           [run_check("sqlite_transaction_rollback", rollback_probe)], root, git_state=state)
    contracts.validate_report(rollback)
    rollback_path = write_report(rollback, "8_rollback_drill_report")
    weight = float(weights.get("8_rollback_drill_report", 0))
    total += rollback["score"] * weight / 100.0
    all_passed = all_passed and rollback["passed"]
    reports.append({"id": "8_rollback_drill_report", "passed": rollback["passed"], "score": rollback["score"], "weight": weight,
                    "file": str(rollback_path.relative_to(root)) if rollback_path and rollback_path.is_relative_to(root) else str(rollback_path) if rollback_path else None})

    real_file = {
        "status": "offline-only",
        "score": None,
        "certified": False,
        "manifest_supplied": bool(args.private_manifest),
        "lock_supplied": bool(args.overlay_lock),
        "details": "未提供真实 private manifest；本次仅完成离线契约验证。",
    }
    if args.private_manifest:
        kernel_pin = {}
        real_check = run_check(
            "real_private_overlay_manifest",
            lambda: validate_real_overlay(args.private_manifest, args.overlay_lock, root,
                                          strict=args.require_real, pin_out=kernel_pin),
            command="python3 tools/verify_spec.py --private-manifest <private-path>",
        )
        real_file["kernel_pin"] = kernel_pin or {"status": "unchecked"}
        real_file.update({
            "status": "file-checked",
            "score": 100.0 if real_check["passed"] else 0.0,
            "certified": real_check["passed"],
            "details": real_check["details"],
            "check": real_check,
        })
    elif args.overlay_lock:
        real_file.update({
            "score": 0.0,
            "details": "提供了 --overlay-lock 但缺少 --private-manifest，未执行真实文件验收。",
        })

    # 本工具不触达任何 AI 客户端，也不弹真实对话框，所以永远给不出 real_runtime_score。
    # 真实联调证据只能来自客户端实机记录（README「验证口径」）；这里如实保持 offline-only。
    real_runtime = {
        "status": "offline-only",
        "score": None,
        "certified": False,
        "details": "本工具不执行真实客户端联调；real_runtime_score 需要客户端实机证据，本次未提供。",
    }
    local_smoke = {"status": "not-run", "probes": [],
                   "details": "未请求本机冒烟探针（--probe-runtime）。"}
    if args.probe_runtime:
        smoke_probes = run_local_smoke_probes(root, runtime_probe_env(args))
        smoke_passed = all(p["passed"] for p in smoke_probes)
        local_smoke = {
            "status": "passed" if smoke_passed else "failed",
            "probes": smoke_probes,
            "details": ("本机冒烟探针全部通过（真实启动器 + 本机档案）；不构成真实客户端联调证据"
                        if smoke_passed else "部分本机冒烟探针失败"),
        }

    # 分数只来自离线契约。显式要求的冒烟探针失败时让本次验收失败关闭，但通过不加分、
    # 也不升级成 real_runtime——以前正是在这里把 dry-run 与字符串比对写成了「真实联调通过」。
    final_score = total
    offline_certified = bool(total >= float(matrix.get("pass_threshold", 99.9)) and all_passed)
    final_certified = bool(offline_certified and (real_file["certified"] if args.require_real else True)
                           and (local_smoke["status"] == "passed" if args.probe_runtime else True))

    # 结论只能说跑过的那几层。以前这里无条件写「真实运行时均通过」，而同一份 JSON 里
    # runtime_status 还是 offline-only —— 这正是三分数分离要防的事。
    certified_layers = ["offline_contract"] if offline_certified else []
    if real_file["certified"]:
        certified_layers.append("real_file")
    if local_smoke["status"] == "passed":
        certified_layers.append("local_smoke")
    note = ["离线契约" + ("通过 99.9+ 门禁" if offline_certified else "未通过门禁")]
    note.append({
        "offline-only": "真实文件未校验（未提供 manifest/lock）",
        "file-checked": "真实文件校验通过" if real_file["certified"] else "真实文件校验未通过",
    }.get(real_file["status"], f"真实文件：{real_file['status']}"))
    note.append({
        "not-run": "未运行本机冒烟探针",
        "passed": "本机冒烟探针通过",
        "failed": "本机冒烟探针未通过",
    }[local_smoke["status"]])
    note.append("真实运行时未联调，本结论不代表实机通过")
    certification_note = "；".join(note) + "。"

    summary = {
        "evaluation_title": "aisk 99.9+ executable contract verification",
        "evaluated_at": now_iso(),
        "git_commit": state["git_commit"],
        "worktree_dirty": state["worktree_dirty"],
        "offline_mode": True,
        "final_score": round(final_score, 2),
        "offline_contract_score": round(total, 2),
        "offline_contract_certified": offline_certified,
        "real_file_score": real_file["score"],
        "real_runtime_score": real_runtime["score"],
        "real_runtime_certified": real_runtime["certified"],
        "runtime_status": real_runtime["status"],
        "real_file_validation": real_file,
        "real_runtime_validation": real_runtime,
        "local_smoke_validation": local_smoke,
        "all_reports_passed": all_passed,
        "is_99_plus_certified": final_certified,
        "certified_layers": certified_layers,
        "certification_note": certification_note,
        "reports": reports,
    }
    if not args.no_report_files:
        (report_dir / "0_acceptance_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.require_real and not args.private_manifest:
        return 2
    return 0 if summary["is_99_plus_certified"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
