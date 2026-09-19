# -*- coding: utf-8 -*-
"""只读 Nacos 查询代理。口令由 CLI 代持，不进上下文。

**为什么必须有这个**：2026-08-26 实测，模型被问「生产 nacos 是什么情况」时，
手搓了 `ssh prod kubectl exec ... | base64 -d | json.loads` 去挖 K8s Secret,
因为 aisk 根本没有 nacos 能力——而运维技能的触发词里明明写着「nacos 配置」。
**触发了却给不出东西，模型只能自己编。**

**历史成因（我造成的回归）**：唯一带鉴权的读取脚本 `nacos-query.sh` 原在
旧技能目录，迁移时没有搬过来，
能力就此凭空消失，`nacos-sync-prod-to-pre.sh` 也随之走进「找不到查询脚本」死路。

**不提供无鉴权兜底**：Nacos 鉴权失败返回的是错误页/空串而不是 HTTP 4xx，
无鉴权请求拿回来的是 HTML 错误页；继续往下跑会把错误页当配置内容用。
这条是 `nacos-sync-prod-to-pre.sh` 用血换来的，原样保留。
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

REDACTED = "[REDACTED]"
TIMEOUT = 8

# **刻意不复用 db.py 的 SENSITIVE_COL**：那个正则把 addr / account / name 也算敏感，
# 用在配置文件上会把 `server-addr`、`username` 一起打码——而这些恰恰是排障要看的。
# 过度脱敏和漏脱敏一样是 bug（information_schema 那次已经教过一遍）。
SENSITIVE_KEY = re.compile(
    r"(pass(word)?|pwd|secret|credential|token"
    r"|api[-_.]?key|access[-_.]?key|secret[-_.]?key|private[-_.]?key)", re.I)

# 匹配 yaml 的 `key: value` 与 properties 的 `key=value`，允许前导 `- ` 和缩进
_KV = re.compile(r"^(\s*-?\s*)([\w.\-]+)(\s*[:=]\s*)(\S.*)$")


class NacosError(Exception):
    pass


def redact_config(text):
    """按键名打码配置里的口令。**Nacos 配置里全是数据库/Redis/MinIO 口令，
    这层比查库的脱敏更要紧**——一次 get 就可能把整套凭据灌进模型上下文。"""
    out = []
    for line in text.splitlines():
        m = _KV.match(line)
        if m and SENSITIVE_KEY.search(m.group(2)):
            out.append(m.group(1) + m.group(2) + m.group(3) + REDACTED)
        else:
            out.append(line)
    return "\n".join(out)


def _request(url, data=None, timeout=TIMEOUT):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise NacosError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise NacosError(f"连不上 {url}（{e.reason}）") from None


def _base(addr):
    """把 profile 里的地址补成可请求的 base URL。"""
    if not addr:
        raise NacosError("profile 的 nacos 段没有 addr/url")
    if not addr.startswith(("http://", "https://")):
        addr = "http://" + addr
    return addr.rstrip("/").removesuffix("/nacos")


def login(addr, user, password):
    """换 accessToken。拿不到就直接失败，绝不退化成无鉴权请求。"""
    url = f"{_base(addr)}/nacos/v1/auth/login"
    raw = _request(url, {"username": user, "password": password})
    try:
        token = json.loads(raw).get("accessToken")
    except json.JSONDecodeError:
        raise NacosError(f"登录返回的不是 JSON（多半是错误页）：{raw[:120]}") from None
    if not token:
        raise NacosError("登录成功但没有 accessToken——账号可能无权限")
    return token


def namespaces(addr, token):
    """列出租户（Nacos 自己的 namespace，**不是 K8s 的 namespace**）。"""
    url = f"{_base(addr)}/nacos/v1/console/namespaces?accessToken={token}"
    raw = _request(url)
    try:
        data = json.loads(raw).get("data") or []
    except json.JSONDecodeError:
        raise NacosError(f"返回的不是 JSON（多半是鉴权失败页）：{raw[:120]}") from None
    return [(d.get("namespace") or "(public)", d.get("namespaceShowName"),
             d.get("configCount")) for d in data]


def configs(addr, token, tenant="", group="", page_size=200):
    """列出某租户下的 DataId。"""
    q = {"accessToken": token, "dataId": "", "group": group, "search": "blur",
         "pageNo": 1, "pageSize": page_size, "tenant": tenant}
    url = f"{_base(addr)}/nacos/v1/cs/configs?" + urllib.parse.urlencode(q)
    raw = _request(url)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise NacosError(f"返回的不是 JSON（多半是鉴权失败页）：{raw[:120]}") from None
    return [(i.get("dataId"), i.get("group"), i.get("type"))
            for i in (data.get("pageItems") or [])], data.get("totalCount", 0)


def get_config(addr, token, data_id, tenant="", group="DEFAULT_GROUP", redact=True):
    q = {"accessToken": token, "dataId": data_id, "group": group, "tenant": tenant}
    url = f"{_base(addr)}/nacos/v1/cs/configs?" + urllib.parse.urlencode(q)
    raw = _request(url)
    # Nacos 查不到时返回空串或 "config data not exist"，不是 404——必须显式判定，
    # 否则空内容会被当成「配置就是空的」继续用下去。
    if not raw.strip() or "config data not exist" in raw.lower():
        raise NacosError(f"DataId 不存在：{data_id} [{group}] @ tenant={tenant or '(public)'}")
    return (redact_config(raw) if redact else raw), damage_note(raw)


def damage_note(text):
    """检测配置在 Nacos 里**存的时候就已经坏了**的情况，返回提示（无损坏返回 None）。

    典型现象：DEV 的业务配置原始字节里带着 \xef\xbf\xbd，
    那是 U+FFFD 自身的 UTF-8 编码——说明入库前就被有损转码过一次，
    不是这里解码解错了。**不提示的话，乱码会被当成本工具的 bug**，
    人就会去查错方向。
    """
    n = text.count("\ufffd")
    if not n:
        return None
    return (f"⚠️ 这条配置里有 {n} 处乱码，**是它存进 Nacos 时就已经损坏的**"
            f"（原始字节含 U+FFFD），不是本次读取的问题。"
            f"\n   先确认损坏是否只在注释里；若落在配置值上，需要重新导入修复。")
