# -*- coding: utf-8 -*-
"""事实提取：从部署仓的 K8s manifest 里解析服务事实。

**为什么不把服务清单写进 profile**：现有 `service-facts.sh` 已经是从
部署仓库的 manifest 是部署事实的权威源。如果 profile 里再抄一份
服务清单，就变成第三份会漂移的副本（技能正文 → profile → manifest）。
profile 只存「部署仓在哪、各环境 manifest 目录在哪」，事实一律现场解析。

这里用的是行扫描而非完整 YAML 解析：manifest 是多文档（--- 分隔）的 K8s 清单，
miniyaml 明确不支持多文档，而这里只需要少量字段，行扫描更稳且不引入依赖。
"""

import re
from pathlib import Path


def _docs(text):
    """按 --- 切分多文档 YAML，返回每段文本。"""
    parts, cur = [], []
    for line in text.splitlines():
        if line.strip() == "---":
            if cur:
                parts.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        parts.append("\n".join(cur))
    return parts


def _first(pattern, text, group=1):
    m = re.search(pattern, text, re.M)
    return m.group(group).strip() if m else None


def service_manifest(mdir, service):
    if not mdir or not Path(mdir).is_dir():
        return None
    f = Path(mdir) / f"{service}.yaml"
    return f if f.is_file() else None


def list_services(mdir):
    if not mdir or not Path(mdir).is_dir():
        return []
    return sorted(p.stem for p in Path(mdir).glob("*.yaml"))


def find_jenkins_job(jdir, service):
    """从 jenkins-jobs 目录反查 Job 名。

    不硬编码服务→Job 映射表：现有 service-facts.sh 用的就是硬编码 case 表，
    实测已经漂了（scrm 有 manifest 但表里没有，返回 manual-check）。
    以文件系统为准，表就不会漂。
    """
    if not jdir or not Path(jdir).is_dir():
        return None
    names = [f.stem for f in Path(jdir).glob("*.xml")]
    for n in names:
        if n.endswith(f"-{service}"):
            return n
    return None


def find_nacos_dataid(cdir, service):
    """从 nacos-configs 目录反查 DataId，同样以文件系统为准。"""
    if not cdir or not Path(cdir).is_dir():
        return None
    for f in sorted(Path(cdir).glob("*.yml")):
        if f.stem.endswith(f"-{service}") or f.stem == service:
            return f.name
    return None


def service_facts(mdir, service, jdir=None, cdir=None):
    """解析一个服务的部署事实。返回 dict；解析不到的字段为 None。"""
    f = service_manifest(mdir, service)
    if not f:
        return None
    text = f.read_text(encoding="utf-8")
    out = {
        "service": service,
        "manifest": str(f),
        "namespace": None,
        "image": None,
        "deployment": None,
        "service_type": None,
        "node_port": None,
        "ports": [],
        "gray_version": None,
        "jenkins_job": find_jenkins_job(jdir, service),
        "nacos_dataid": find_nacos_dataid(cdir, service),
    }
    for doc in _docs(text):
        kind = _first(r"^kind:\s*(\S+)", doc)
        if kind == "Deployment":
            out["deployment"] = _first(r"^\s*-?\s*name:\s*(\S+)", doc)
            out["namespace"] = out["namespace"] or _first(r"^\s*namespace:\s*(\S+)", doc)
            img = _first(r'^\s*-?\s*image:\s*"?([^"\s]+)"?', doc)
            out["image"] = out["image"] or img
            gv = re.search(r"name:\s*GRAY_VERSION\s*\n\s*value:\s*\"?([^\"\n]+)\"?", doc)
            if gv:
                out["gray_version"] = gv.group(1).strip()
        elif kind == "Service":
            out["service_type"] = _first(r"^\s*type:\s*(\S+)", doc)
            out["namespace"] = out["namespace"] or _first(r"^\s*namespace:\s*(\S+)", doc)
            np = _first(r"^\s*nodePort:\s*(\d+)", doc)
            if np:
                out["node_port"] = np
            out["ports"] = re.findall(r"^\s*(?:port|targetPort):\s*(\d+)", doc, re.M)
    return out


# 这些键不进 env 摘要：要么单独成行显示，要么是内部字段/敏感指针
_SUMMARY_SKIP = {"hosts", "k8s", "policy", "require_confirm", "db",
                 "manifests", "jenkins_jobs", "nacos_configs", "structure"}


def env_summary(prof, env_name, env):
    """把一个环境的关键事实压成短清单——这是要注入技能正文的内容，必须紧凑。

    **展示策略是「除非明确排除，否则都显示」**，不是白名单。
    2026-08-24 踩过：白名单写死成 nacos/registry/build 三个，
    profile 里配了 frontend_url 却永远不显示——而技能正文已经改成
    「地址见 aisk env 的 frontend_url」，等于指向一个看不见的东西。
    加一个 profile 键就要改一次内核代码，这本身就违背了「换项目只写 yaml」。
    """
    lines = [f"project={prof.get('project')} env={env_name}"]

    hosts = env.get("hosts") or {}
    if isinstance(hosts, dict) and hosts:
        lines.append("hosts: " + " ".join(f"{k}={v}" for k, v in hosts.items()))
    k8s = env.get("k8s") or {}
    if isinstance(k8s, dict) and k8s:
        lines.append("k8s: " + " ".join(f"{k}={v}" for k, v in k8s.items()))

    for key, v in (env or {}).items():
        if key in _SUMMARY_SKIP or v is None or v == "":
            continue
        if isinstance(v, dict):
            lines.append(f"{key}: " + " ".join(f"{k}={vv}" for k, vv in v.items()))
        else:
            lines.append(f"{key}: {v}")

    # db 单独处理：显示连法但**不显示 secret 键名以外的东西**，口令永远不在这
    db = env.get("db") or {}
    if isinstance(db, dict) and db:
        shown = {k: v for k, v in db.items() if k not in ("secret", "secret_rw")}
        if shown:
            lines.append("db: " + " ".join(f"{k}={v}" for k, v in shown.items())
                         + "  （口令由 aisk secret 代持，查库用 aisk db query）")

    pol = env.get("policy")
    if pol:
        extra = " require_confirm=true" if env.get("require_confirm") else ""
        lines.append(f"policy: {pol}{extra}")
    return lines
