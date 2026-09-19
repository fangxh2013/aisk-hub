# -*- coding: utf-8 -*-
"""Profile 加载与解析。

**关键设计（对应设计文档 §7.8）：不使用全局 `active` 指针。**
全局可变状态在多项目/多 agent 并行时会把 DEV 的命令打到 PROD——这台机器自己的
AI 记忆里至少两次记录过并发会话互踩状态的真实事故，不是理论风险。
所以 profile 由「当前所在仓库」推导：从 cwd 向上找 git 根，再匹配各 profile 的 repos.*。
匹配不到或匹配到多个，一律要求显式 --profile，绝不静默猜测。
"""

import os
from pathlib import Path

from . import miniyaml

_PROFILE_ROOT = os.environ.get("AISK_PROFILE_DIR")
PROFILE_DIR = (
    Path(_PROFILE_ROOT).expanduser()
    if _PROFILE_ROOT
    else Path(os.environ.get("AISK_HOME", Path.home() / ".aisk")) / "profiles"
)


def runtime_root() -> Path:
    """返回运行时根目录；它与私有 profile 目录可以分离。"""
    configured = os.environ.get("AISK_HOME")
    if configured:
        return Path(configured).expanduser()
    # 纯内核调用/测试会替换 PROFILE_DIR；没有启动器注入 AISK_HOME 时，
    # 沿用 profile 的父目录，避免把临时状态写进真实用户目录。
    return PROFILE_DIR.parent.expanduser()


class ProfileError(Exception):
    pass


def _expand(p):
    """展开 ~ 和环境变量，返回绝对 Path。Windows 上 ~ 同样有效。"""
    return Path(os.path.expandvars(str(p))).expanduser()


def list_profiles():
    if not PROFILE_DIR.is_dir():
        return []
    return sorted(PROFILE_DIR.glob("*.yaml"))


def load(path):
    data = miniyaml.load_file(path)
    data.setdefault("project", path.stem)
    data.setdefault("repos", {})
    data.setdefault("envs", {})
    if not isinstance(data["envs"], dict):
        raise ProfileError(f"{path}: envs 必须是映射")
    data["_path"] = path
    return data


def repo_root(start=None):
    """从 start（默认 cwd）向上找 git 仓库根。找不到返回 None。"""
    cur = Path(start or Path.cwd()).resolve()
    for cand in [cur, *cur.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def find_slot_meta(start=None):
    """向上查找任务元数据（兼容旧槽位），返回 (task_dir, task_meta)。"""
    from .worktree import names
    cur = Path(start or Path.cwd()).resolve()
    for cand in [cur, *cur.parents]:
        meta_file = names.task_meta_file(cand)
        if meta_file:
            try:
                import json
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                return cand, data
            except Exception:
                return cand, {}
    return None, None


def resolve_linked_repo(root):
    """若 root 是 linked worktree，从 .git 文件反查其主仓库根目录 Path；否则返回 root。"""
    if root is None:
        return None
    git_file = Path(root) / ".git"
    if git_file.is_file():
        try:
            line = git_file.read_text(encoding="utf-8").strip()
            if line.startswith("gitdir:"):
                gitdir = Path(line.split(":", 1)[1].strip()).resolve()
                if "worktrees" in gitdir.parts:
                    idx = gitdir.parts.index("worktrees")
                    parent_git = Path(*gitdir.parts[:idx])
                    if parent_git.name == ".git":
                        return parent_git.parent.resolve()
        except Exception:
            pass
    return root


def _repo_paths(prof):
    out = []
    repos = prof.get("repos") or {}
    if isinstance(repos, dict):
        for v in repos.values():
            if isinstance(v, str):
                expanded = os.path.expandvars(v)
                if "$" in expanded:
                    continue
                p = Path(expanded).expanduser()
                if p.is_absolute():
                    out.append(p.resolve())
    return out


def db_prefixes(prof):
    """返回 [(环境名, 库前缀), ...]。库前缀是识别一个库属于哪个项目/环境的最强信号。"""
    out = []
    for env_name, env in (prof.get("envs") or {}).items():
        pre = ((env or {}).get("db") or {}).get("prefix")
        if pre:
            out.append((env_name, pre))
    return out


def match_by_database(dbname, profiles=None):
    """用库名反查 profile 和环境。

    典型失败：模型调 aisk_db_query 给了 database=example_dev_cloud，
    profile 却猜成 example——因为库名和仓库目录都叫 example，只有 profile 叫 demo。
    **它要的答案本来就在它自己给的参数里**：demo 的 dev 段写着 prefix: example_dev_，
    精确匹配。与其让模型猜名字，不如从它已经给出的库名反推。

    返回 [(profile 名, prof, 环境名, 前缀), ...]，按前缀长度降序——
    前缀越长越具体，免得 example_ 抢走 example_dev_ 的匹配。
    """
    if not dbname:
        return []
    profiles = profiles if profiles is not None else list_profiles()
    hits = []
    for p in profiles:
        try:
            prof = load(p)
        except Exception:                    # noqa: BLE001  坏 profile 不该拖垮反查
            continue
        for env_name, prefix in db_prefixes(prof):
            if dbname.startswith(prefix):
                hits.append((p.stem, prof, env_name, prefix))
    hits.sort(key=lambda h: -len(h[3]))
    return hits


def _suggest_profile(wrong, profiles):
    """名字猜错时反推该用哪个，别只甩一句「现有：…」让模型接着猜。"""
    w = wrong.lower()
    for p in profiles:
        try:
            prof = load(p)
        except Exception:                    # noqa: BLE001
            continue
        for env_name, prefix in db_prefixes(prof):
            if w in prefix.lower():
                return (f"\n你要找的多半是 **{p.stem}**——它 {env_name} 环境的库前缀是 "
                        f"{prefix}，里面就带着 {wrong}。")
        for rp in _repo_paths(prof):
            if w in str(rp).lower():
                return (f"\n你要找的多半是 **{p.stem}**——它的仓库路径是 {rp}，"
                        f"里面就带着 {wrong}。")
    return ""


def resolve(explicit=None, start=None, db_hint=None):
    """返回 (profile, 解释字符串)。解释字符串说明为什么选中它，便于排查。"""
    profiles = list_profiles()
    if not profiles:
        raise ProfileError(
            f"没有找到任何 profile。请先创建 {PROFILE_DIR}/<项目名>.yaml\n"
            f"可从模板复制：agent-skills/templates/profile.example.yaml"
        )

    avail = ", ".join(p.stem for p in profiles)

    if explicit:
        for p in profiles:
            if p.stem == explicit:
                return load(p), f"显式指定 --profile {explicit}"
        raise ProfileError(
            f"没有名为 {explicit} 的 profile。现有：{avail}"
            + _suggest_profile(explicit, profiles)
        )

    root = repo_root(start)
    slot_dir, _ = find_slot_meta(start)

    # 1. 尝试从 git 仓库或 linked worktree 反查
    if root is not None:
        main_root = resolve_linked_repo(root)
        matches = []
        for p in profiles:
            prof = load(p)
            for rp in _repo_paths(prof):
                if (rp == root or rp in root.parents or root in rp.parents or
                        rp == main_root or rp in main_root.parents or main_root in rp.parents):
                    matches.append((p, prof, rp))
                    break
        if len(matches) == 1:
            p, prof, rp = matches[0]
            extra = f"（通过 linked worktree {root.name} 反查）" if main_root != root else ""
            return prof, f"当前仓库 {main_root}{extra} 匹配 {p.stem} 的 repos 条目 {rp}"
        if len(matches) > 1:
            names = ", ".join(m[0].stem for m in matches)
            raise ProfileError(
                f"当前仓库 {root} 同时匹配多个 profile（{names}），拒绝猜测。"
                f"请用 --profile 显式指定。"
            )

    # 2. 若不在 git repo 内但在 xw 槽位根目录，按槽位内的子仓库反查
    if slot_dir is not None:
        matches = []
        for sub in sorted(slot_dir.iterdir()):
            if sub.is_dir() and (sub / ".git").exists():
                sub_main = resolve_linked_repo(sub)
                for p in profiles:
                    prof = load(p)
                    for rp in _repo_paths(prof):
                        if rp == sub_main or rp in sub_main.parents or sub_main in rp.parents:
                            if not any(m[0] == p for m in matches):
                                matches.append((p, prof, rp))
        if len(matches) == 1:
            p, prof, rp = matches[0]
            return prof, f"当前槽位 {slot_dir.name} 包含的仓库匹配 {p.stem} 的 repos 条目 {rp}"
        # 仓库反查无果 → 落到库名兜底

    # 库名兜底。**MCP 场景下这是唯一可用的信号**：AI 工具拉起 MCP 服务端时用的是
    # 自己的目录，cwd 永远不在仓库里，git 反查必然失败；但库名通常就在调用参数里。
    if db_hint:
        hits = match_by_database(db_hint, profiles)
        names = {h[0] for h in hits}
        if len(names) == 1:
            pname, prof, env_name, prefix = hits[0]
            return prof, f"库名 {db_hint} 命中 {pname} 的 {env_name} 前缀 {prefix}"
        if len(names) > 1:
            raise ProfileError(
                f"库名 {db_hint} 同时匹配 {', '.join(sorted(names))}，拒绝猜测。"
                f"请用 --profile 显式指定。"
            )

    if root is None:
        raise ProfileError(
            "当前目录不在任何 git 仓库内，无法自动判定 profile。\n"
            f"现有 profile：{avail}\n"
            "请用 --profile <名字> 指定；走 MCP 时也可以直接给 database 参数，"
            "会按库前缀反查。"
        )
    raise ProfileError(
        f"当前仓库 {root} 不属于任何 profile 的 repos。\n"
        f"现有 profile：{avail}\n"
        f"请用 --profile <名字> 显式指定，或把该仓库加进对应 profile 的 repos。"
    )


def get_env(prof, env_name=None):
    """取环境配置。不提供名字时，只有在恰好一个环境时才自动选，否则要求显式指定。"""
    envs = prof.get("envs") or {}
    if not envs:
        raise ProfileError(f"profile {prof['project']} 没有定义任何 envs")
    if env_name:
        if env_name not in envs:
            raise ProfileError(
                f"profile {prof['project']} 没有环境 {env_name}。现有：{', '.join(envs)}"
            )
        return env_name, (envs[env_name] or {})
    if len(envs) == 1:
        only = next(iter(envs))
        return only, (envs[only] or {})
    raise ProfileError(
        f"profile {prof['project']} 有多个环境（{', '.join(envs)}），请用 --env 明确指定。"
        f"\n生产环境必须显式写 --env，不做任何默认推断。"
    )


def deploy_root(prof):
    repos = prof.get("repos") or {}
    d = repos.get("deploy")
    return _expand(d) if d else None


def env_path(prof, env_name, key):
    """取该环境下某个相对 deploy 仓的路径（如 manifests / jenkins_jobs / nacos_configs）。

    各环境布局不一致（实测 dev/pre/prod 三者结构都不同），所以路径由 profile 逐个环境
    显式声明，内核不做任何目录约定的假设。未声明或 deploy 仓缺失时返回 None。
    """
    _, env = get_env(prof, env_name)
    rel = env.get(key)
    root = deploy_root(prof)
    if not rel or not root:
        return None
    return root / rel


def knowledge_dir(prof):
    """项目知识包目录（拓扑图、部署手册这类**只对本项目成立**的资料）。

    **为什么要分出来**：像「DEV 双集群拓扑图」这种东西，把 IP 换成 <data-host>
    占位符之后对谁都没用了——它本来就是项目知识误放进了通用内核。
    参数化解决不了归属错误，只会把有用的文档变成没用的模板。

    正确做法是按归属拆：内核留「怎么排查 K8s」的方法，项目留「本项目长什么样」
    的拓扑。技能正文用 `aisk knowledge` 指过去，换项目时换的是这个目录。

    profile 里声明 `knowledge: <相对 deploy 仓的路径>` 或绝对路径；未声明返回 None。
    """
    k = prof.get("knowledge")
    if not k:
        return None
    kp = _expand(k)
    if kp.is_absolute():
        return kp
    root = deploy_root(prof)
    return (root / k) if root else None


def manifest_dir(prof, env_name):
    """返回该环境的 manifest 目录（Path）或 None。"""
    return env_path(prof, env_name, "manifests")
