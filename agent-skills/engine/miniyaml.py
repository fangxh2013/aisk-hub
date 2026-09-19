# -*- coding: utf-8 -*-
"""零依赖的 YAML 子集解析器。

为什么不用 PyYAML：内核的卖点是「接新项目只写一个配置文件」，如果这个文件需要先
`pip install pyyaml` 才能读，跨机器/跨系统就多了一道安装门槛（2026-08-24 实测本机
python3.14 没有 PyYAML）。所以优先用 PyYAML（如果装了，正确性更好），没有就退回本模块。

**设计原则：宁可报错，不可猜错。** 本模块只认 profile 真正需要的语法子集，
遇到任何看不懂的写法一律抛错并指出行号，绝不静默按错误的方式解析——
配置文件里一个被悄悄解析错的环境名，可能意味着把 DEV 的命令打到 PROD。

支持的子集：
  - 注释（整行 # 或行尾 空格+#）
  - 嵌套映射（靠缩进，必须是空格，禁止 Tab）
  - 标量：裸字符串 / 单双引号字符串 / 整数 / true|false|null|~
  - 行内映射：{k: v, k2: v2}（不支持嵌套行内映射）
  - 列表：- item（标量列表；不支持列表套映射）

明确不支持（遇到即报错）：锚点 & 引用 *、多文档 ---、块标量 | >、复杂嵌套行内结构。
"""

import re

TRUE = {"true", "yes", "on"}
FALSE = {"false", "no", "off"}
NULLS = {"null", "~", ""}


class YamlSubsetError(ValueError):
    """解析失败。消息里必须带行号，方便人直接定位配置文件。"""


def _scalar(raw, lineno):
    s = raw.strip()
    if s.startswith(("&", "*")):
        raise YamlSubsetError(f"第 {lineno} 行：不支持 YAML 锚点/引用（& 或 *）")
    if s in ("|", ">") or s.startswith(("| ", "> ")):
        raise YamlSubsetError(f"第 {lineno} 行：不支持块标量（| 或 >）")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in TRUE:
        return True
    if low in FALSE:
        return False
    if low in NULLS:
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _inline_map(body, lineno):
    """解析 {k: v, k2: v2}。不支持嵌套花括号。"""
    inner = body.strip()[1:-1].strip()
    out = {}
    if not inner:
        return out
    if "{" in inner or "}" in inner:
        raise YamlSubsetError(f"第 {lineno} 行：不支持嵌套的行内映射")
    for part in inner.split(","):
        if ":" not in part:
            raise YamlSubsetError(f"第 {lineno} 行：行内映射的 '{part.strip()}' 缺少冒号")
        k, v = part.split(":", 1)
        out[k.strip()] = _scalar(v, lineno)
    return out


def _strip_comment(line):
    """去掉行尾注释，但不能误伤引号里的 #（如 URL 里的锚点）。"""
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def loads(text):
    """把 YAML 子集文本解析成 dict。失败抛 YamlSubsetError（含行号）。"""
    rows = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw.split("#")[0]:
            raise YamlSubsetError(f"第 {lineno} 行：缩进含 Tab，YAML 只允许空格")
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if line.strip() == "---":
            raise YamlSubsetError(f"第 {lineno} 行：不支持多文档分隔符 ---")
        indent = len(line) - len(line.lstrip(" "))
        rows.append((indent, line.strip(), lineno))

    root = {}
    # stack 存 (indent, 容器)。容器是 dict 或 list。
    stack = [(-1, root)]

    for idx, (indent, content, lineno) in enumerate(rows):
        while len(stack) > 1 and indent <= stack[-1][0]:
            # YAML permits an "indentless sequence":
            #
            #   items:
            #   - one
            #   - two
            #
            # Keep the list parent for more items at the same indentation,
            # but pop it before the next sibling mapping key.
            if (isinstance(stack[-1][1], list) and indent == stack[-1][0]
                    and content.startswith("- ")):
                break
            stack.pop()
        parent = stack[-1][1]

        if content.startswith("- "):
            if not isinstance(parent, list):
                raise YamlSubsetError(f"第 {lineno} 行：列表项没有对应的父级键")
            body = content[2:].strip()
            # 整体被引号包住的标量一律当标量：`- "http://host:8848"` 里的冒号是值的一部分，
            # 不是列表映射的分隔符。只放行裸 URL 是不够的；带端口的推送前缀若被拆成映射，
            # 生成的 git 配置会得到垃圾值，推送封锁可能静默失效。
            quoted = len(body) >= 2 and body[:1] in ("'", '"') and body[-1:] == body[:1]
            # 支持技能/状态规范常用的列表映射：
            #   - id: check
            #     weight: 10
            # 仍然只支持一个简单键值作为列表项首行，不扩展成完整 YAML。
            if ":" in body and not quoted and not body.startswith(("http://", "https://")):
                key, rest = body.split(":", 1)
                key, rest = key.strip(), rest.strip()
                if not key:
                    raise YamlSubsetError(f"第 {lineno} 行：列表映射键不能为空")
                item = {key: _scalar(rest, lineno)}
                parent.append(item)
                stack.append((indent, item))
            else:
                parent.append(_scalar(body, lineno))
            continue

        if ":" not in content:
            raise YamlSubsetError(f"第 {lineno} 行：既不是键值对也不是列表项 -> {content!r}")

        key, rest = content.split(":", 1)
        key, rest = key.strip(), rest.strip()
        if not isinstance(parent, dict):
            raise YamlSubsetError(f"第 {lineno} 行：在列表内部出现了键值对，不支持")

        if rest.startswith("{"):
            if not rest.endswith("}"):
                raise YamlSubsetError(f"第 {lineno} 行：行内映射没有闭合的 }}")
            parent[key] = _inline_map(rest, lineno)
            continue

        if rest:
            parent[key] = _scalar(rest, lineno)
            continue

        # 空值：看下一行的缩进决定是嵌套映射、列表，还是真的 null
        nxt = rows[idx + 1] if idx + 1 < len(rows) else None
        if nxt and (nxt[0] > indent or (nxt[0] == indent and nxt[1].startswith("- "))):
            container = [] if nxt[1].startswith("- ") else {}
        else:
            container = None
        parent[key] = container
        if container is not None:
            # For an indentless sequence, use the sequence indentation as the
            # sibling boundary; nested sequences retain the key indentation.
            stack.append((nxt[0] if nxt and nxt[0] == indent else indent, container))

    return root


def load_file(path):
    """优先用 PyYAML（正确性更好），没装则退回本模块的子集解析器。"""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError:
        return loads(text)
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise YamlSubsetError(f"{path} 顶层必须是映射，实际是 {type(data).__name__}")
    return data
