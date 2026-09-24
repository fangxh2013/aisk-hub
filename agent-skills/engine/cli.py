# -*- coding: utf-8 -*-
"""aisk 命令行入口。

输出面向「被 AI 读取」而不是「给人看漂亮」：短、结构化、无装饰，
每个命令只回答被问的那一件事，不附带背景说明——正文里每多一行都是常驻 token。
"""

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

from . import backup, contracts, dberrors, facts, launch, link, permissions, privacy, profile, schema, secrets, token_efficiency
from .brokers import db as db_broker
from .brokers import nacos as nacos_broker
from .miniyaml import YamlSubsetError, load_file
from .skill_router import SkillRouter, SkillRouterError
from .token.planner import Planner


def _out(s=""):
    # Windows 终端默认可能是 GBK，显式走 utf-8 避免中文炸掉
    sys.stdout.write(str(s) + "\n")


def _resolve(args):
    prof, why = profile.resolve(getattr(args, "profile", None))
    return prof, why


def cmd_profile(args):
    prof, why = _resolve(args)
    _out(f"profile: {prof['project']}")
    _out(f"来源: {why}")
    _out(f"文件: {prof['_path']}")
    envs = prof.get("envs") or {}
    _out(f"环境: {', '.join(envs) if envs else '(无)'}")
    return 0


def cmd_env(args):
    prof, _ = _resolve(args)
    name, env = profile.get_env(prof, args.env)
    for line in facts.env_summary(prof, name, env):
        _out(line)
    if not args.brief:
        mdir = profile.manifest_dir(prof, name)
        if mdir:
            svcs = facts.list_services(mdir)
            _out(f"services({len(svcs)}): {' '.join(svcs)}" if svcs else "services: (manifest 目录为空)")
        else:
            _out("services: (该环境未声明 manifests 目录)")
    return 0


def _table_query_fn(prof, env_name, env):
    """给 schema 模块注入一个只读查询函数。凭据在这里取，schema 模块不碰。"""
    db = env.get("db") or {}
    missing = [k for k in ("host", "user", "secret") if not db.get(k)]
    if missing:
        raise profile.ProfileError(f"envs.{env_name}.db 缺少 {', '.join(missing)}")
    pwd = secrets.get(db["secret"])
    if not pwd:
        raise profile.ProfileError(
            f"取不到凭据 {db['secret']}——先跑 aisk secret set {db['secret']}")

    def run(sql):
        return db_broker.query(
            sql, host=db.get("host") or db.get("vip"), port=db.get("port", 3306),
            user=db["user"], password=pwd, mysql_bin=db.get("mysql_bin"),
            max_rows=5000, redact=False)   # 元数据查询，无隐私可脱

    return run, db


def cmd_fact_table(args):
    """表 → 库定位。查库前先问这一句，就不会再猜错库。"""
    prof, _ = _resolve(args)
    name, env = profile.get_env(prof, args.env)
    db = env.get("db") or {}
    prefix = db.get("prefix")
    if not prefix:
        _out(f"❌ envs.{name}.db 没有声明 prefix（库名前缀），无法枚举本项目的库")
        return 2

    tables = None if args.refresh else schema.load_cache(prof["project"], name)
    if tables is None:
        try:
            run, _ = _table_query_fn(prof, name, env)
            tables = schema.fetch(run, prefix)
        except (profile.ProfileError, db_broker.BrokerError) as e:
            _out(f"❌ {e}")
            return 1
        schema.save_cache(prof["project"], name, tables)

    if not tables:
        _out(f"(env={name} 没有匹配 {prefix}* 的库)")
        return 1

    if not args.name:
        _out(f"env={name} 共 {len(tables)} 张表，分布：")
        for sch, n in schema.schema_summary(tables):
            _out(f"  {n:>4}  {sch}")
        _out("\n用法: aisk fact table <表名|模式> --env " + name)
        return 0

    hits, sugg = schema.find(tables, args.name)
    if hits:
        for t in hits[:args.limit]:
            rows = f"  ~{t['rows']} 行" if t.get("rows") else ""
            _out(f"{t['schema']}.{t['table']}{rows}")
        if len(hits) > args.limit:
            _out(f"… 另有 {len(hits)-args.limit} 张，加 --limit 看更多")
        return 0

    _out(f"❌ env={name} 找不到表 {args.name}")
    if sugg:
        _out("\n可能想找的是：")
        for t in sugg:
            _out(f"  {t['schema']}.{t['table']}")
    else:
        _out("  （也没有相近的表名。确认表名，或用通配： aisk fact table '%order%')")
    return 1


def cmd_fact_columns(args):
    """查表字段。带中文注释——那是 AI 判断字段语义最强的线索。"""
    prof, _ = _resolve(args)
    name, env = profile.get_env(prof, args.env)
    if not args.name:
        _out("用法: aisk fact columns <表名> [--env X] [--like 关键词]")
        return 2

    target = args.name
    schema_name = None
    if "." in target:
        schema_name, target = target.split(".", 1)

    try:
        run, _db = _table_query_fn(prof, name, env)
    except (profile.ProfileError, db_broker.BrokerError) as e:
        _out(f"❌ {e}")
        return 1

    # 没给库名就先定位——用户不该被迫记住表在哪个库
    if not schema_name:
        tables = schema.load_cache(prof["project"], name)
        if tables is None:
            prefix = (env.get("db") or {}).get("prefix")
            if prefix:
                tables = schema.fetch(run, prefix)
                schema.save_cache(prof["project"], name, tables)
        hits, sugg = schema.find(tables or [], target)
        if len(hits) == 1:
            schema_name = hits[0]["schema"]
            target = hits[0]["table"]
        elif len(hits) > 1:
            _out(f"表名 {target} 在多个库里都有，指明是哪个：")
            for t in hits[:8]:
                _out(f"  {t['schema']}.{t['table']}")
            return 1
        else:
            _out(f"❌ 找不到表 {target}")
            for t in sugg[:8]:
                _out(f"  可能是 {t['schema']}.{t['table']}")
            return 1

    cols = schema.fetch_columns(run, schema_name, target)
    _out(schema.format_columns(schema_name, target, cols, args.like))
    return 0


def cmd_fact_related(args):
    """查一张表能跟哪些表关联。跨表排查前问这一句，省得猜 JOIN 条件。"""
    prof, _ = _resolve(args)
    name, env = profile.get_env(prof, args.env)
    if not args.name:
        _out("用法: aisk fact related <表名> [--env X]")
        return 2

    target, schema_name = args.name, None
    if "." in target:
        schema_name, target = target.split(".", 1)

    try:
        run, _db = _table_query_fn(prof, name, env)
    except (profile.ProfileError, db_broker.BrokerError) as e:
        _out(f"❌ {e}")
        return 1

    if not schema_name:
        tables = schema.load_cache(prof["project"], name)
        if tables is None:
            prefix = (env.get("db") or {}).get("prefix")
            tables = schema.fetch(run, prefix) if prefix else []
            if tables:
                schema.save_cache(prof["project"], name, tables)
        hits, sugg = schema.find(tables or [], target)
        if len(hits) == 1:
            schema_name, target = hits[0]["schema"], hits[0]["table"]
        else:
            _out(f"❌ 无法唯一定位表 {target}")
            for t in (hits or sugg)[:8]:
                _out(f"  {t['schema']}.{t['table']}")
            return 1

    cols = schema.fetch_columns(run, schema_name, target)
    rel = schema.find_related(run, schema_name, target, cols)
    _out(schema.format_related(schema_name, target, rel, cols))
    return 0


def cmd_fact(args):
    if args.kind == "related":
        return cmd_fact_related(args)
    if args.kind == "columns":
        return cmd_fact_columns(args)
    if args.kind == "table":
        return cmd_fact_table(args)

    prof, _ = _resolve(args)
    name, _env = profile.get_env(prof, args.env)
    mdir = profile.manifest_dir(prof, name)

    if args.kind == "services":
        svcs = facts.list_services(mdir)
        if not svcs:
            _out(f"(env={name} 未声明 manifests 目录或目录为空)")
            return 1
        _out(" ".join(svcs))
        return 0

    if not args.name:
        _out("用法: aisk fact service <名字> [--env dev]", )
        return 2

    f = facts.service_facts(
        mdir,
        args.name,
        jdir=profile.env_path(prof, name, "jenkins_jobs"),
        cdir=profile.env_path(prof, name, "nacos_configs"),
    )
    if not f:
        svcs = facts.list_services(mdir)
        _out(f"env={name} 找不到服务 {args.name}")
        if svcs:
            _out(f"可用: {' '.join(svcs)}")
        return 1

    if args.kind == "entry":
        _out(f"{f['service']} type={f['service_type']} nodePort={f['node_port']} ports={','.join(f['ports']) or '-'}")
        return 0

    _out(f"service={f['service']} ns={f['namespace']} deployment={f['deployment']}")
    _out(f"image={f['image']}")
    if f["gray_version"]:
        _out(f"gray_version={f['gray_version']}")
    _out(f"entry: type={f['service_type']} nodePort={f['node_port']} ports={','.join(f['ports']) or '-'}")
    if f["jenkins_job"]:
        _out(f"jenkins_job={f['jenkins_job']}")
    if f["nacos_dataid"]:
        _out(f"nacos_dataid={f['nacos_dataid']}")
    _out(f"manifest={f['manifest']}")
    return 0


def _slot_repo_path(name, main_path, slot_dir, slot_repos, profile_repos=None):
    # 1. slot.json 中已登记的精确映射：档案仓库键（profile_repos）或任务内仓库别名（repos）
    for mapping in (profile_repos or {}, slot_repos):
        if name in mapping:
            p = Path(mapping[name])
            if p.is_dir() and (p / ".git").exists():
                return str(p.resolve())
    # 2. 槽位根目录下的同名目录 (be, web, docs 等)
    direct = slot_dir / name
    if direct.is_dir() and (direct / ".git").exists():
        return str(direct.resolve())
    # 3. profile 中该仓库主目录名在槽位中的映射
    if main_path:
        dir_name = profile._expand(main_path).name
        if dir_name in slot_repos:
            p = Path(slot_repos[dir_name])
            if p.is_dir() and (p / ".git").exists():
                return str(p.resolve())
        dir_cand = slot_dir / dir_name
        if dir_cand.is_dir() and (dir_cand / ".git").exists():
            return str(dir_cand.resolve())
    return None


def _slot_profile_repos(prof, slot_meta):
    """档案仓库键 → 槽位内路径。新任务的 slot.json 自带 profile_repos；
    旧槽位没有，就按档案 worktrees.repos.<别名>.profile_repo 从槽位别名推导（不写死任何别名）。"""
    meta = slot_meta or {}
    mapping = dict(meta.get("profile_repos") or {})
    slot_repos = meta.get("repos") or {}
    declared = ((prof.get("worktrees") or {}).get("repos") or {}) if isinstance(prof.get("worktrees"), dict) else {}
    for alias, decl in declared.items():
        key = str((decl or {}).get("profile_repo", alias)) if isinstance(decl, dict) else alias
        if alias in slot_repos and key not in mapping:
            mapping[key] = slot_repos[alias]
    return mapping


def cmd_repo(args):
    """打印仓库路径。技能正文用它取代硬编码绝对路径。"""
    prof, _ = _resolve(args)
    repos = prof.get("repos") or {}

    # 检查是否处于 xw 隔离槽位内
    slot_dir, slot_meta = profile.find_slot_meta()
    if slot_dir is not None:
        slot_repos = (slot_meta or {}).get("repos") or {}
        profile_repos = _slot_profile_repos(prof, slot_meta)
        slot_id = (slot_meta or {}).get("id") or slot_dir.name

        if not args.name:
            for k, v in repos.items():
                p = _slot_repo_path(k, v, slot_dir, slot_repos, profile_repos)
                if p:
                    _out(f"{k}={p}")
                else:
                    _out(f"{k}=(槽位未包含该仓库)")
            return 0

        target_path = _slot_repo_path(args.name, repos.get(args.name), slot_dir, slot_repos, profile_repos)
        if target_path:
            _out(target_path)
            return 0
        else:
            _out(f"❌ 当前处于槽位 {slot_id}，但该槽位未包含仓库 {args.name}。禁止切换到主工作区。")
            return 1

    # 正常主工作区环境
    if not args.name:
        for k, v in repos.items():
            _out(f"{k}={profile._expand(v)}")
        return 0
    if args.name not in repos:
        _out(f"profile {prof['project']} 没有仓库 {args.name}。现有：{', '.join(repos)}")
        return 1
    _out(profile._expand(repos[args.name]))
    return 0


def _private_overlay_paths(private_root):
    """Return only Manifest-approved private skills; never scan the whole private tree."""
    root = Path(private_root).expanduser()
    if not root.is_dir():
        return []
    manifest_path = root / "OVERLAY_MANIFEST.yaml"
    if not manifest_path.is_file():
        raise ValueError(f"私有根存在但缺少 Overlay manifest: {manifest_path}")
    try:
        manifest = load_file(manifest_path)
        contracts.validate_overlay(manifest)
    except (OSError, YamlSubsetError, contracts.ContractError) as exc:
        raise ValueError(f"私有 Overlay manifest 无法加载: {exc}") from exc
    if not isinstance(manifest.get("manifest_id"), str) or not manifest["manifest_id"].strip():
        raise ValueError("私有 Overlay manifest 缺少 manifest_id，拒绝加载技能")

    selected = []
    for relative in manifest.get("custom_skills", []) or []:
        skill = root / relative
        resolved = skill.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"私有 Overlay 技能越过仓库根目录: {relative}") from exc
        if skill.is_symlink() or (skill / "SKILL.md").is_symlink():
            raise ValueError(f"私有 Overlay 技能必须是无符号链接目录: {relative}")
        if not skill.is_dir():
            raise ValueError(f"Manifest 声明的私有技能目录不存在: {relative}")
        if not (skill / "SKILL.md").is_file():
            raise ValueError(f"Manifest 声明的私有技能缺少 SKILL.md: {relative}")
        selected.append(str(skill))
    return selected


def cmd_link(args):
    """分发技能到各端。默认 dry-run 之外一律真写，但绝不整目录删。"""
    kernel = Path(__file__).resolve().parent.parent / "skills"
    if not kernel.is_dir():
        _out(f"❌ 内核技能目录不存在: {kernel}")
        return 1
    tools = args.tools or list(link.DEFAULT_TOOLS)
    overlays = list(args.overlay_from or [])
    private_root = os.environ.get("AISK_PRIVATE_ROOT", "")
    if private_root:
        try:
            approved = _private_overlay_paths(private_root)
        except ValueError as exc:
            _out(f"❌ {exc}")
            return 2
        overlays.extend(skill for skill in approved if skill not in overlays)
    rc = 0
    for t in tools:
        try:
            n, stale, untouched, log = link.link(
                t, kernel, dry_run=args.dry_run, overlay_from=overlays)
        except ValueError as e:
            _out(f"❌ {e}")
            rc = 1
            continue
        tag = "[dry-run] " if args.dry_run else ""
        _out(f"{tag}{t}: 写入 {n} 个技能，回收 {stale} 个，未触碰端上其他 {untouched} 个")
        for line in log:
            _out(f"   {line}")
        if t == "antigravity" and not args.dry_run:
            f_plugin = link.write_antigravity_plugin({
                "name": "agent-skills",
                "version": "2.0.0",
                "description": "项目无关的 AI 技能内核（由 aisk link 生成，勿手工编辑）",
                "author": {"name": "agent-skills"},
                "license": "Apache-2.0",
            })
            _out(f"   plugin.json -> {f_plugin}")
            f_rules = link.write_antigravity_rules()
            _out(f"   rules/AGENTS.md -> {f_rules}")
            f_mcp = link.write_antigravity_mcp()
            _out(f"   mcp_config.json -> {f_mcp}")
    if not args.dry_run:
        _out(f"内核位置 -> {link.write_kernel_pointer(kernel.parent)}")
    return rc


def _skill_root(args):
    return Path(args.root).expanduser() if getattr(args, "root", None) else (
        Path(__file__).resolve().parent.parent / "skills"
    )


def cmd_skill(args):
    """Inspect the public canonical skill registry without loading project data."""
    router = SkillRouter()
    root = _skill_root(args)
    try:
        if args.action == "list":
            for name in router.canonical_names():
                _out(f"{name}\taliases={len(router.aliases_for(name))}")
            return 0
        if args.action == "check":
            errors = router.check(root)
            if errors:
                for error in errors:
                    _out(f"❌ {error}")
                return 1
            _out(f"✅ {len(router.canonical_names())} 个 canonical 技能通过检查")
            return 0
        if not args.name:
            _out(f"❌ skill {args.action} 需要技能名")
            return 2
        if args.action == "resolve":
            _out(router.require(args.name))
            return 0
        if args.action == "inspect":
            result = router.inspect(args.name, root)
            if args.json:
                _out(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                _out(f"name: {result['name']}")
                _out(f"requested: {result['requested']}")
                _out(f"path: {result['path']}")
                _out(f"lines: {result['lines']}")
                _out("aliases: " + (" ".join(result["aliases"]) or "(none)"))
                _out("references: " + (" ".join(result["references"]) or "(none)"))
            return 0
    except (SkillRouterError, OSError) as exc:
        _out(f"❌ {exc}")
        return 1
    return 2


def cmd_package(args):
    """生成可分发的 Codex 插件包；安装前不改变当前技能发现集合。"""
    kernel = Path(__file__).resolve().parent.parent / "skills"
    root, manifest = link.write_codex_plugin(kernel, args.output)
    _out(f"codex plugin: {root}")
    _out(f"manifest: {manifest}")
    _out("下一步：把该目录加入个人 marketplace 后安装；未安装前不会与 ~/.agents/skills 重复加载。")
    return 0


def _skill_files(root):
    if not root.is_dir():
        return []
    return sorted(p / "SKILL.md" for p in root.iterdir()
                  if p.is_dir() and (p / "SKILL.md").is_file())


def _metadata_token_estimate(files):
    """估算 Codex 选择阶段的 name + description 成本；只作回归趋势，不冒充账单。"""
    parts = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not match:
            continue
        fm = match.group(1)
        name = re.search(r"^name:\s*(.+)$", fm, re.M)
        desc = re.search(r"^description:\s*>?\s*\n?(.*?)(?=^[a-z_]+:|\Z)", fm, re.S | re.M)
        parts.append((name.group(1).strip() if name else f.parent.name) + "\n")
        parts.append((desc.group(1).strip() if desc else "") + "\n")
    wire = "".join(parts)
    cjk = len(re.findall(r"[一-鿿　-〿＀-￯]", wire))
    return math.ceil(cjk / 1.15 + (len(wire) - cjk) / 3.8)


def cmd_audit(args):
    """审计 Codex 本地技能的重复与历史影子，输出可验证的 token 风险。"""
    canonical = Path(link.TARGETS["codex"]["root"]).expanduser()
    legacy = Path(link.TARGETS["codex"]["legacy_root"]).expanduser()
    current_files, old_files = _skill_files(canonical), _skill_files(legacy)
    current = {p.parent.name for p in current_files}
    old = {p.parent.name for p in old_files}
    duplicated = sorted(current & old)
    shadows = sorted(name for name in link.CODEX_SHADOWS if name in current)
    _out(f"Codex 用户技能目录: {canonical} ({len(current)} 个，选择元数据约 {_metadata_token_estimate(current_files)} tok)")
    _out(f"旧目录: {legacy} ({len(old)} 个，选择元数据约 {_metadata_token_estimate(old_files)} tok)")
    _out(f"同名重复: {len(duplicated)}" + (f" · {' '.join(duplicated)}" if duplicated else ""))
    _out(f"历史影子: {len(shadows)}" + (f" · {' '.join(shadows)}" if shadows else ""))
    if duplicated or shadows:
        _out("结论: 仍可能增加技能选择上下文；运行 aisk link codex 可安全迁移 aisk 标记副本并归档已知旧技能。")
        return 1
    _out("结论: 未发现 aisk 已知的 Codex 重复或历史影子。")
    return 0


def cmd_launch(args):
    """预热 TLS 后再启动应用，避开「初始化慢于 UI 超时」的竞态。"""
    try:
        launch.launch(args.app, skip_prewarm=args.no_prewarm, out=_out)
    except launch.LaunchError as e:
        _out(f"❌ {e}")
        return 2
    return 0


def cmd_netcheck(args):
    """测到关键端点的 TLS 握手耗时。切代理节点后重跑本命令对比。"""
    return launch.netcheck(out=_out, timeout=args.timeout)


def cmd_secret(args):
    """凭据管理。**永远不打印值**——list 只出键名，没有 get 子命令给模型用。"""
    if args.action == "status":
        st = secrets.status()
        _out(f"当前后端: {st['active']}")
        _out(f"  钥匙串可用      : {'✅' if st['keychain'] else '❌'}")
        _out(f"  sops 已安装     : {'✅' if st['sops_binary'] else '❌'}")
        _out(f"  age 私钥存在    : {'✅' if st['age_key'] else '❌'}")
        _out(f"  加密文件存在    : {'✅' if st['sops_file'] else '❌'}")
        if not st["sops_ready"] and st["keychain"]:
            _out("\n提示：SOPS 未就绪，已自动使用钥匙串。要用 SOPS 先跑 age-keygen。")
        return 0
    if args.action == "list":
        keys = secrets.list_keys(privileged=args.privileged)
        ns = "特权" if args.privileged else "只读"
        _out(f"后端 {secrets.backend_name()} · {ns}命名空间，共 {len(keys)} 个（只列键名）：")
        for k in keys:
            _out(f"  {k}")
        if not keys:
            _out("  (空) —— 用 aisk secret set <键名> 录入")
        return 0
    if args.action == "set":
        if not args.key:
            _out("用法: aisk secret set <键名>")
            return 2
        try:
            b = secrets.prompt_and_put(args.key, privileged=args.privileged)
        except secrets.SecretError as e:
            _out(f"❌ {e}")
            return 1
        _out(f"✅ 已存入 {args.key}（后端 {b}）")
        return 0
    if args.action == "rm":
        if not args.key:
            _out("用法: aisk secret rm <键名>")
            return 2
        ok = secrets.delete(args.key, privileged=args.privileged)
        _out(f"{'✅ 已删除' if ok else '⚠️ 不存在'} {args.key}")
        return 0 if ok else 1
    return 2


def cmd_nacos(args):
    """只读 Nacos broker。口令由 CLI 代持，不进模型上下文。

    这里把通用只读能力接入内核，见 engine/brokers/nacos.py 的说明。
    """
    prof, _ = _resolve(args)
    name, env = profile.get_env(prof, args.env)
    cfg = env.get("nacos") or {}
    if not cfg:
        _out(f"❌ profile {prof['project']} 的 {name} 环境没有配置 nacos")
        _out(f"   需要 addr（或 url）/ user / secret，租户写 tenant")
        return 2

    if env.get("require_confirm") and not args.yes:
        _out(f"⛔ {name} 环境标记了 require_confirm，需要加 --yes 显式确认后才查询")
        return 2

    addr = cfg.get("addr") or cfg.get("url")
    key = cfg.get("secret")
    if not key:
        _out(f"❌ {name}.nacos 没有声明 secret 键名")
        return 2
    pwd = secrets.get(key)
    if not pwd:
        _out(f"❌ 取不到凭据 {key}（后端 {secrets.backend_name()}）")
        _out(f"   录入: aisk secret set {key}")
        return 1

    tenant = args.tenant if args.tenant is not None else (cfg.get("tenant") or "")

    try:
        token = nacos_broker.login(addr, cfg.get("user") or "nacos", pwd)
        if args.action == "ns":
            rows = nacos_broker.namespaces(addr, token)
            _out(f"{name} 的 Nacos 租户（**不是 K8s namespace**）：")
            for tid, show, cnt in rows:
                mark = "  ← profile 里 tenant 指的就是它" if tid == tenant else ""
                _out(f"  {tid:<24} {show or '':<18} 配置数 {cnt}{mark}")
            return 0

        if args.action == "list":
            items, total = nacos_broker.configs(addr, token, tenant, args.group or "")
            _out(f"{name} tenant={tenant or '(public)'} 共 {total} 条配置：")
            for did, grp, typ in items:
                _out(f"  {did:<40} {grp:<16} {typ or ''}")
            return 0

        if args.action == "get":
            if not args.data_id:
                _out("❌ get 需要 DataId：aisk nacos get <DataId> --env X")
                return 2
            # `--no-redact` 仅为兼容旧调用保留；AI/CLI 统一强制脱敏，原文不进入上下文。
            allow_unredacted = False
            text, damage = nacos_broker.get_config(
                addr, token, args.data_id, tenant,
                args.group or "DEFAULT_GROUP", redact=not allow_unredacted)
            _out(text)
            if damage:
                _out("\n" + damage)
            if not allow_unredacted:
                _out("\n（口令类字段已打码。配置里带的是真实数据库/Redis 凭据，"
                     "确需原文请人工登控制台看，别用 --no-redact 灌进上下文。）")
            return 0
    except nacos_broker.NacosError as e:
        _out(f"❌ {e}")
        return 1
    return 2


def cmd_db(args):
    """只读数据库 broker。口令由 CLI 代持，不进模型上下文。"""
    writing = args.action == "exec"

    if writing:
        # 写操作的三道门，缺一不可。默认路径根本走不到这里。
        if not args.privileged:
            _out("⛔ exec 是写操作，必须显式加 --privileged（会用特权凭据并留审计）")
            return 2
        if not args.yes:
            _out("⛔ 写操作必须显式加 --yes 确认")
            return 2
    else:
        # 先校验 SQL 再碰凭据：注定被拒的查询不该触碰凭据库，
        # 报错也该说真正的原因（写操作被拒），而不是「取不到凭据」。
        try:
            db_broker.assert_readonly(args.sql)
        except db_broker.BrokerError as e:
            _out(f"❌ {e}")
            return 1

    prof, _ = _resolve(args)
    name, env = profile.get_env(prof, args.env)
    dbcfg = env.get("db") or {}
    if not dbcfg:
        _out(f"❌ profile {prof['project']} 的 {name} 环境没有配置 db（需要 host/user/secret）")
        return 2

    if env.get("require_confirm") and not args.yes:
        _out(f"⛔ {name} 环境标记了 require_confirm，需要加 --yes 显式确认后才查询")
        return 2

    key = dbcfg.get("secret_rw") if writing else dbcfg.get("secret")
    if not key:
        which = "secret_rw" if writing else "secret"
        _out(f"❌ {name}.db 没有声明 {which} 键名")
        return 2
    pwd = secrets.get(key, privileged=writing)
    if not pwd:
        ns = " --privileged" if writing else ""
        _out(f"❌ 取不到凭据 {key}（后端 {secrets.backend_name()}）")
        _out(f"   录入: aisk secret set {key}{ns}")
        return 1

    if writing:
        user_rw = dbcfg.get("user_rw") or dbcfg.get("user")
        _out(f"⚠️ 写操作：env={name} 账号={user_rw} —— 已记入 ~/.aisk/privileged-access.log")
        try:
            out = db_broker.execute(
                args.sql,
                host=dbcfg.get("host") or dbcfg.get("vip"),
                port=dbcfg.get("port", 3306),
                user=user_rw,
                password=pwd,
                database=args.database or dbcfg.get("database"),
                mysql_bin=dbcfg.get("mysql_bin"),
            )
        except db_broker.BrokerError as e:
            secrets.audit("db.exec", key, ok=False, note=f"env={name}")
            _out(f"❌ {e}")
            return 1
        secrets.audit("db.exec", key, ok=True, note=f"env={name} sql={args.sql[:60]}")
        _out(out or "(执行完成，无输出)")
        return 0

    try:
        cols, rows, trunc = db_broker.query(
            args.sql,
            host=dbcfg.get("host") or dbcfg.get("vip"),
            port=dbcfg.get("port", 3306),
            user=dbcfg.get("user"),
            password=pwd,
            database=args.database or dbcfg.get("database"),
            max_rows=args.limit,
            redact=True,
        )
    except db_broker.BrokerError as e:
        _out(f"❌ {e}")
        try:
            run, _db = _table_query_fn(prof, name, env)
            hint = dberrors.explain(e, args.sql, prof, name,
                                    lambda sc, tb: schema.fetch_columns(run, sc, tb),
                                    run=run, prefix=(env.get("db") or {}).get("prefix"))
        except Exception:                               # noqa: BLE001
            hint = None
        if hint:
            _out("")
            _out(hint)
        return 1
    _out(db_broker.format_table(cols, rows, trunc, args.limit))
    return 0


def cmd_knowledge(args):
    """列出/定位项目知识包——只对本项目成立的拓扑、手册这类资料。"""
    prof, _ = _resolve(args)
    kdir = profile.knowledge_dir(prof)
    if not kdir:
        _out(f"profile {prof['project']} 未声明 knowledge 目录")
        _out("  在 profile 顶层加：knowledge: <相对 deploy 仓的路径>")
        return 1
    if not kdir.is_dir():
        _out(f"❌ knowledge 目录不存在: {kdir}")
        return 1
    if args.name:
        hits = sorted(kdir.rglob(f"*{args.name}*"))
        if not hits:
            _out(f"找不到 {args.name}；可用文档见 aisk knowledge")
            return 1
        for h in hits[:10]:
            _out(str(h))
        return 0
    _out(f"项目知识包: {kdir}")
    for f in sorted(kdir.rglob("*.md"))[:40]:
        _out(f"  {f.relative_to(kdir)}")
    return 0


# `aisk permit` 的默认端列表。**只在这里定义一次**，子命令 help 由它算出来——
# 曾经 help 写死「省略=全部五端」而这里实际列了六端（多了 workbuddy-ai）。
# 端数靠人手抄一定会漂，而 AI 会把 help 当事实读，所以改成结构上不可能不一致。
PERMIT_TOOLS = ["claude", "cursor", "antigravity", "codex", "workbuddy", "workbuddy-ai"]


def cmd_permit(args):
    """生成各端只读动词白名单（决策 5）。只增不改，写前自动备份。"""
    # WorkBuddy 桌面版与 AI 版是两个入口，两个都要列出来——只列桌面版会让 AI 端
    # 在 `aisk permit` 里凭空消失，被误读成「已覆盖」。
    tools = args.tools or PERMIT_TOOLS
    results = permissions.apply(tools, dry_run=args.dry_run)
    tag = "[dry-run] " if args.dry_run else ""
    _out(f"{tag}只读动词白名单：")
    for r in results:
        _out(r.line())
    unsup = [r for r in results if r.status == "unsupported"]
    if unsup:
        _out("")
        _out("以下端无法生成，如实说明而不是假装做到：")
        for r in unsup:
            _out(f"  {r.tool}: {r.detail}")
    return 0


_UNKNOWN_COL = re.compile(r"Unknown column '([^']+)'", re.I)
_TABLE_IN_SQL = re.compile(r"\bFROM\s+`?([A-Za-z0-9_]+)`?\.?`?([A-Za-z0-9_]*)`?", re.I)


def _suggest_on_column_error(err, args, prof, env_name, env):
    """撞 Unknown column 时直接给出真实字段名。

    2026-08-25 实测痛点：AI 凭电商惯例猜 order_sn/order_no，实际是
    beiyue_order_no。一次报错本该给出答案，而不是让它 DESC 一遍再重猜——
    截图里 27 条命令就是这么滚出来的。
    """
    m = _UNKNOWN_COL.search(err)
    if not m:
        return
    wrong = m.group(1).split(".")[-1]

    t = _TABLE_IN_SQL.search(args.sql or "")
    if not t:
        return
    a, b = t.group(1), t.group(2)
    schema_name, table = (a, b) if b else (None, a)

    try:
        run, _db = _table_query_fn(prof, env_name, env)
        if not schema_name:
            tables = schema.load_cache(prof["project"], env_name) or []
            hits, _ = schema.find(tables, table)
            if len(hits) != 1:
                return
            schema_name, table = hits[0]["schema"], hits[0]["table"]
        cols = schema.fetch_columns(run, schema_name, table)
    except Exception:                                   # noqa: BLE001
        return
    if not cols:
        return

    sugg = schema.suggest_columns(cols, wrong)
    if sugg:
        _out(f"\n{schema_name}.{table} 里没有 {wrong}，你要找的可能是：")
        for c in sugg:
            cm = f"  — {c['comment']}" if c["comment"] else ""
            _out(f"  {c['name']:<26} {c['type']}{cm}")
    _out(f"\n完整字段: aisk fact columns {schema_name}.{table} --env {env_name}")


def cmd_guard_main(args):
    """安装/检查 main 保护钩子。

    2026-08-27 起：main 只能由本人手工合并。这条约束必须是本地强制的——
    当时技能里声称「服务端保护」，实测是假的，AI 直推 origin/main 成功了。
    """
    import subprocess
    script = Path(__file__).resolve().parent.parent / "tools" / "install_main_guard.py"
    roots = args.roots
    if not roots:
        # 仓库位置是项目事实，只从档案取；档案里不是 git 仓库的条目（如任务数据根）由安装脚本跳过
        prof, _ = _resolve(args)
        roots = [str(p) for p in profile._repo_paths(prof) if p.is_dir()]
        if not roots:
            _out(f"❌ 档案 {prof['_path']} 的 repos 里没有存在的目录；可显式给出：aisk guard-main <仓库>...")
            return 2
    r = subprocess.run([sys.executable, str(script), *roots], text=True,
                       capture_output=True)
    _out(r.stdout.rstrip() or r.stderr.rstrip())
    return r.returncode


def cmd_backup(args):
    """快照「只在本机、没有版本管理」的那部分：项目档案、任务登记簿、未提交在制品。

    任务数据根里 99% 是团队仓库的检出，不进这里；要异地副本的是剩下那 100KB。
    """
    dest = Path(args.dest or (profile.runtime_root() / "backup")).expanduser()
    total, detail = backup.snapshot(dest, worktrees=args.wip, include_secrets=args.secrets)
    _out(f"快照 → {dest}")
    for line in detail:
        _out(f"  {line}")
    changed, note = backup.commit(dest, push=args.push)
    _out(f"{'✅' if changed or '一致' in note else '⚠️'} {note}")
    return 0 if ("失败" not in note) else 1


def cmd_doctor(args):
    """体检：profile 能不能解析、路径存不存在。P0 阶段的自检入口。"""
    rc = 0
    if getattr(args, "profile", None):
        selected, _ = _resolve(args)
        profs = [Path(selected["_path"])]
    else:
        profs = profile.list_profiles()
    _out(f"profile 目录: {profile.PROFILE_DIR}")
    if not profs:
        _out("❌ 没有任何 profile")
        return 1
    for p in profs:
        try:
            prof = profile.load(p)
        except (YamlSubsetError, Exception) as e:  # noqa: BLE001
            _out(f"❌ {p.name} 解析失败: {e}")
            rc = 1
            continue
        _out(f"✅ {p.name} 解析通过 (project={prof['project']})")
        for key, val in (prof.get("repos") or {}).items():
            path = profile._expand(val)
            mark = "✅" if path.exists() else "❌"
            if not path.exists():
                rc = 1
            _out(f"   {mark} repos.{key} -> {path}")
        for ename in (prof.get("envs") or {}):
            mdir = profile.manifest_dir(prof, ename)
            if mdir is None:
                _out(f"   ·  envs.{ename} 未声明 manifests（跳过）")
            elif mdir.is_dir():
                _out(f"   ✅ envs.{ename}.manifests -> {mdir} ({len(facts.list_services(mdir))} 个服务)")
            else:
                _out(f"   ❌ envs.{ename}.manifests 指向不存在的目录 -> {mdir}")
                rc = 1

    # 分发状态。**这层缺失过一次代价很大**：Codex 端读了一天旧内容，
    # 还留着指示模型去读明文口令的退役技能，而 doctor 当时只看 profile。
    _out("")
    _out("各端分发状态：")
    for tool, behind, retired, note in link.drift_report(
            Path(__file__).resolve().parent.parent / "skills"):
        if behind or retired:
            _out(f"   ⚠️ {tool}: {note}")
            rc = 1
        elif note:
            # 只剩「在制品叠加」这类说明：是刻意的状态，不是待办，别让 doctor 变红
            _out(f"   ℹ️ {tool}: {note}")
        else:
            _out(f"   ✅ {tool}: 与内核一致")
    return rc


def cmd_public_scan(args):
    root = Path(args.root or __import__("os").environ.get("AISK_HUB_ROOT", Path.cwd())).resolve()
    findings = privacy.scan(root)
    if args.json:
        _out(__import__("json").dumps({"ok": not findings, "findings": findings}, ensure_ascii=False, indent=2))
    else:
        _out(f"public-root: {root}")
        if findings:
            for item in findings:
                _out(f"❌ {item['rule']} {item['path']}:{item['line']}")
        else:
            _out("✅ 未发现默认隐私规则命中")
    return 0 if not findings else 1


def cmd_public_verify(args):
    root = Path(args.root or __import__("os").environ.get("AISK_HUB_ROOT", Path.cwd())).resolve()
    since = getattr(args, "since", None)
    ok, findings = privacy.verify(root, since=since)
    if args.json:
        _out(__import__("json").dumps({"ok": ok, "since": since, "findings": findings}, ensure_ascii=False, indent=2))
    else:
        _out(f"public-root: {root}")
        if since:
            _out(f"范围：merge-base({since}, HEAD)..HEAD 的待发布提交（逐提交检查新增行）")
        if findings:
            for item in findings:
                _out(f"❌ {item['rule']} {item['path']}:{item['line']}")
        elif since:
            _out("✅ 待发布提交未引入默认隐私规则命中（完整历史请运行不带 --since 的 public verify）")
        else:
            _out("✅ 工作树与可达 Git 历史均未发现默认隐私规则命中")
    return 0 if ok else 1


def cmd_public_export(args):
    source = Path(args.root or __import__("os").environ.get("AISK_HUB_ROOT", Path.cwd())).resolve()
    dest = Path(args.dest).expanduser().resolve()
    ok, findings = privacy.verify(source)
    if not ok:
        _out("❌ 发布前扫描未通过；必须先完成脱敏")
        for item in findings:
            _out(f"   {item['rule']} {item['path']}:{item['line']}")
        return 1
    if dest.exists() and any(dest.iterdir()) and not args.replace:
        _out(f"❌ 输出目录非空：{dest}；清空后重试或显式传 --replace")
        return 2
    if args.replace and dest.exists():
        import shutil
        shutil.rmtree(dest)
    copied = privacy.copy_public(source, dest)
    _out(f"✅ 已按 allowlist 导出公开候选：{dest}（{len(copied)} 项）")
    return 0


def cmd_contract(args):
    root = Path(args.root or __import__("os").environ.get("AISK_HUB_ROOT", Path.cwd())).resolve()
    if args.action == "verify":
        results = {
            "skills": contracts.validate_skill_migration(root),
            "adapter": contracts.validate_adapter(contracts.load_yaml(root, "adapter-capabilities.yaml")),
            "coordination": contracts.validate_coordination_specs(root),
        }
        _out(__import__("json").dumps({"ok": True, "root": str(root), "results": results}, ensure_ascii=False, indent=2))
        return 0
    if args.action == "tokens":
        result = token_efficiency.audit(root)
        _out(__import__("json").dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    if args.action == "token-runtime":
        if not args.task or not args.task.strip():
            _out("❌ token-runtime 需要 --task 任务描述")
            return 2
        task = {"title": args.task}
        try:
            result = Planner().plan(
                task,
                complexity=args.complexity,
                required_sources=args.required_source,
                available_sources=args.available_source,
                conflicts=args.conflict,
            )
        except (TypeError, ValueError) as exc:
            _out(f"❌ token-runtime: {exc}")
            return 2
        _out(__import__("json").dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.action == "alias":
        if not args.name:
            _out("❌ contract alias 需要技能名")
            return 2
        _out(contracts.resolve_skill_alias(root, args.name))
        return 0
    raise ValueError(f"未知 contract 动作: {args.action}")


def build_parser():
    ap = argparse.ArgumentParser(prog="aisk", description="项目无关的 AI 技能内核 CLI")
    ap.add_argument("--profile", help="显式指定 profile（不指定则按当前仓库自动匹配）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("profile", help="显示当前解析到的 profile 及其来源")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("env", help="输出当前环境事实（供技能正文注入）")
    p.add_argument("--env", help="环境名，如 dev/pre/prod")
    p.add_argument("--brief", action="store_true", help="只输出核心事实，不列服务清单")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("fact", help="查一个具体事实")
    p.add_argument("kind", choices=["service", "entry", "services", "table", "columns", "related"],
                   help="事实类型；table=表在哪个库，columns=字段，related=能跟哪些表关联")
    p.add_argument("name", nargs="?", help="服务名或表名（table 支持 %% 通配）")
    p.add_argument("--env", help="环境名")
    p.add_argument("--refresh", action="store_true", help="table: 忽略缓存重新拉取")
    p.add_argument("--like", help="columns: 只看名字或注释含该词的字段")
    p.add_argument("--limit", type=int, default=20, dest="limit",
                   help="table: 最多列出几张表")
    p.set_defaults(func=cmd_fact)

    p = sub.add_parser("link", help="把内核技能分发到各 AI 工具端")
    p.add_argument("tools", nargs="*",
                   help=f"端名：{' '.join(link.TARGETS)}（省略={' '.join(link.DEFAULT_TOOLS)}）")
    p.add_argument("--dry-run", action="store_true", help="只报告不写入")
    p.add_argument("--overlay-from", dest="overlay_from", action="append", metavar="技能目录",
                   help="在制品叠加源，**传的是一个技能目录**（含 SKILL.md），可重复；"
                        "同名技能不从内核写，改用这份覆盖，并记进端上标记的 overlaid")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("skill", help="解析、检查和查看公共 canonical 技能")
    p.add_argument("action", choices=["list", "resolve", "inspect", "check"])
    p.add_argument("name", nargs="?", help="canonical 技能名或旧别名")
    p.add_argument("--root", help="技能根目录；默认当前内核的 agent-skills/skills")
    p.add_argument("--json", action="store_true", help="inspect: 输出 JSON")
    p.set_defaults(func=cmd_skill)

    p = sub.add_parser("package", help="生成可安装的 Codex 插件包（不自动安装）")
    p.add_argument("tool", choices=["codex"])
    p.add_argument("--output", required=True, help="插件输出目录，例如 ~/.agents/plugins/agent-skills")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("audit", help="审计技能重复与历史影子")
    p.add_argument("tool", choices=["codex"])
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("repo", help="打印仓库路径（取代技能正文里的绝对路径）")
    p.add_argument("name", nargs="?", help="仓库键名；省略则列出全部")
    p.set_defaults(func=cmd_repo)

    p = sub.add_parser("launch", help="预热网络后启动应用（治 Antigravity 黑屏）")
    p.add_argument("app", help="应用键名，见 ~/.aisk/launch.yaml")
    p.add_argument("--no-prewarm", action="store_true", help="跳过预热直接启动")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("netcheck", help="测 TLS 握手耗时，判断会不会黑屏")
    p.add_argument("--timeout", type=int, default=20)
    p.set_defaults(func=cmd_netcheck)

    p = sub.add_parser("secret", help="凭据管理（永不打印值）")
    p.add_argument("action", choices=["status", "list", "set", "rm"])
    p.add_argument("key", nargs="?")
    p.add_argument("--privileged", action="store_true",
                   help="操作特权命名空间（写权限凭据），每次取用都留审计")
    p.set_defaults(func=cmd_secret)

    p = sub.add_parser("db", help="只读数据库查询（口令由 CLI 代持，不进上下文）")
    p.add_argument("action", choices=["query", "exec"])
    p.add_argument("sql")
    p.add_argument("--env", help="环境名；生产必须显式指定")
    p.add_argument("--database", "-D", help="库名")
    p.add_argument("--limit", type=int, default=200, help="最大返回行数")
    p.add_argument("--no-redact", action="store_true", help="关闭字段脱敏（慎用）")
    p.add_argument("--yes", action="store_true", help="确认 require_confirm 的环境")
    p.add_argument("--privileged", action="store_true",
                   help="exec 必需：用特权凭据执行写操作，留审计")
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("nacos", help="只读 Nacos 查询（口令由 CLI 代持，不进上下文）")
    p.add_argument("action", choices=["ns", "list", "get"],
                   help="ns=列租户 list=列 DataId get=取配置")
    p.add_argument("data_id", nargs="?", help="get 时的 DataId")
    p.add_argument("--env", help="环境名；生产必须显式指定")
    p.add_argument("--tenant", help="Nacos 租户 ID；省略用 profile 里的 tenant")
    p.add_argument("--group", help="配置分组，get 默认 DEFAULT_GROUP")
    p.add_argument("--no-redact", action="store_true", help="关闭口令打码（慎用）")
    p.add_argument("--yes", action="store_true", help="确认 require_confirm 的环境")
    p.set_defaults(func=cmd_nacos)

    p = sub.add_parser("knowledge", help="项目知识包（拓扑/手册等只对本项目成立的资料）")
    p.add_argument("name", nargs="?", help="按名字过滤")
    p.set_defaults(func=cmd_knowledge)

    p = sub.add_parser("permit", help="生成各端只读动词白名单（免确认执行）")
    p.add_argument("tools", nargs="*",
                   help=f"端名；省略=全部 {len(PERMIT_TOOLS)} 端（{'/'.join(PERMIT_TOOLS)}）")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_permit)

    p = sub.add_parser("guard-main", help="安装 main 保护钩子（main 只能人工合并）")
    p.add_argument("roots", nargs="*", help="仓库或包含仓库的目录；省略=项目档案 repos 里的仓库")
    p.set_defaults(func=cmd_guard_main)

    p = sub.add_parser("backup", help="快照档案与任务登记簿到备份仓库（不含任何仓库检出）")
    p.add_argument("--dest", help="备份仓库目录，默认 ~/.aisk/backup")
    p.add_argument("--wip", action="append", default=[],
                   help="额外快照这个 worktree 的未提交改动（可多次；内核仓库的全部 worktree 已自动包含）")
    p.add_argument("--secrets", action="store_true", help="一并带上加密后的 SOPS 文件（密钥仍只在本机）")
    p.add_argument("--push", action="store_true", help="提交后推送")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("doctor", help="体检 profile 与路径")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("public", help="公开发布前扫描与导出（默认不输出文件内容）")
    public_sub = p.add_subparsers(dest="public_action", required=True)
    for name, fn in (("scan", cmd_public_scan), ("verify", cmd_public_verify)):
        q = public_sub.add_parser(name)
        q.add_argument("--root", help="扫描根目录，默认当前 aisk-hub")
        q.add_argument("--json", action="store_true")
        if name == "verify":
            q.add_argument("--since", metavar="REV",
                           help="只检查 merge-base(REV, HEAD)..HEAD 的待发布提交（任务发布门禁用）；"
                                "不给则扫工作树与完整历史")
        q.set_defaults(func=fn)
    q = public_sub.add_parser("export")
    q.add_argument("--root", help="源目录，默认当前 aisk-hub")
    q.add_argument("--dest", required=True, help="公开候选输出目录")
    q.add_argument("--replace", action="store_true", help="允许清空非空输出目录后重新导出")
    q.set_defaults(func=cmd_public_export)

    p = sub.add_parser("contract", help="验证公共契约、Token预算或解析技能逻辑别名")
    p.add_argument("action", choices=["verify", "alias", "tokens", "token-runtime"])
    p.add_argument("name", nargs="?", help="alias 动作的技能名")
    p.add_argument("--root", help="工程根目录，默认当前 aisk-hub")
    p.add_argument("--task", help="token-runtime: 当前任务描述")
    p.add_argument("--complexity", choices=["simple", "standard", "complex", "deep", "critical", "emergency", "uncertain"], help="token-runtime: 显式复杂度")
    p.add_argument("--required-source", action="append", default=[], help="token-runtime: 必须加载的上下文源，可重复")
    p.add_argument("--available-source", action="append", default=[], help="token-runtime: 当前可用的上下文源，可重复")
    p.add_argument("--conflict", action="append", default=[], help="token-runtime: 已检测到的规则冲突，可重复")
    p.set_defaults(func=cmd_contract)

    p = sub.add_parser("task", help="多 AI 并行任务工作区（aisk task --help 查看子命令）", add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_task)
    return ap


def cmd_task(args):
    from .worktree import cli as task_cli
    return task_cli.main(args.rest, profile=getattr(args, "profile", None))


def _split_task(argv):
    """`aisk [--profile X] task …` 原样转交子命令行，避免外层 argparse 吞掉 task 自己的选项。"""
    prof, rest = None, list(argv)
    if rest[:1] == ["--profile"] and len(rest) >= 2:
        prof, rest = rest[1], rest[2:]
    elif rest and rest[0].startswith("--profile="):
        prof, rest = rest[0].split("=", 1)[1], rest[1:]
    return (prof, rest[1:]) if rest[:1] == ["task"] else (None, None)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    prof, task_args = _split_task(argv)
    if task_args is not None:
        from .worktree import cli as task_cli
        return task_cli.main(task_args, profile=prof)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (profile.ProfileError, YamlSubsetError) as e:
        _out(f"❌ {e}")
        return 2
