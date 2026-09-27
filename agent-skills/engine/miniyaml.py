# -*- coding: utf-8 -*-
"""零依赖的 YAML 子集解析器。

为什么不用 PyYAML：内核的卖点是「接新项目只写一个配置文件」，如果这个文件需要先
`pip install pyyaml` 才能读，跨机器/跨系统就多了一道安装门槛（2026-08-24 实测本机
python3.14 没有 PyYAML）。所以优先用 PyYAML（如果装了，正确性更好），没有就退回本模块。

**设计原则：宁可报错，不可猜错。** 本模块只认 profile 真正需要的语法子集，
遇到任何看不懂的写法一律抛错并指出行号，绝不静默按错误的方式解析——
配置文件里一个被悄悄解析错的环境名，可能意味着把 DEV 的命令打到 PROD。
同一份文件在装了 PyYAML 的机器和没装的机器（例如 CI）上必须读出同样的结果：
子集内的写法与 PyYAML 结果一致；PyYAML 会读成别的东西、本模块又不复刻的写法，一律报错。

支持的子集：
  - 注释（整行 # 或行尾 空格+#）
  - 嵌套映射（靠缩进，必须是空格，禁止 Tab；同一层的条目必须对齐）
  - 键和值用「冒号+空格」分隔：`- http://host:8848`、`- build:dev` 整体是标量
  - 标量：裸字符串 / 单双引号字符串（双引号按 YAML 转义）/ 十进制整数与小数 /
    true|false|null|~（大小写规则同 PyYAML：true、True、TRUE）
  - 行内映射 {k: v, k2: v2} 与行内列表 [a, b]（条目只能是标量，不支持嵌套）
  - 列表：- item，以及首行写成 `- 键: 值`、后续键与首个键对齐的列表映射

明确不支持（遇到即报错）：锚点 & 引用 *、标签 !、多文档 ---、块标量 | >、嵌套的行内结构、
跨行书写的值，以及 PyYAML 会换算成别的类型的裸写法（前导零/十六进制/带下划线的数字、
六十进制 1:30、.5、.inf、日期时间）——这类值要当字符串用就加引号。
"""

import re

TRUE = {"true", "yes", "on"}
FALSE = {"false", "no", "off"}
NULLS = {"null", "~", ""}

# 键和值之间必须是「冒号+空白」或行尾的冒号；URL、build:dev 里的冒号属于值本身。
_PAIR = re.compile(r":(?:\s+|$)")
_INT = re.compile(r"[-+]?(?:0|[1-9][0-9]*)")
_FLOAT = re.compile(r"[-+]?[0-9]+\.[0-9]*(?:[eE][-+][0-9]+)?")
# 下面三条照抄 PyYAML（YAML 1.1）的隐式类型规则。命中它们却不是上面两种十进制写法的裸值，
# PyYAML 会换算成八进制数、六十进制数、日期等；本模块不复刻换算，直接要求加引号。
_YAML11_INT = re.compile(
    r"[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)"
    r"|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+")
_YAML11_FLOAT = re.compile(
    r"[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9_]+(?:[eE][-+][0-9]+)?"
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)")
_YAML11_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?"
    r"(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?")
# 双引号字符串的转义表，与 PyYAML 一致；表外的反斜杠组合 PyYAML 也会报错。
_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
    "r": "\r", "e": "\x1b", " ": " ", '"': '"', "\\": "\\", "/": "/", "N": "\x85", "_": "\xa0",
    "L": "\u2028", "P": "\u2029",
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


class YamlSubsetError(ValueError):
    """解析失败。消息里必须带行号，方便人直接定位配置文件。"""


def _yaml11_word(s, words):
    """PyYAML 只认全小写、首字母大写、全大写三种写法：True 是布尔值，tRue 是字符串。"""
    low = s.lower()
    return low in words and s in (low, low.capitalize(), low.upper())


def _quoted_end(text, lineno):
    """text 以引号开头，返回与之配对的闭合引号下标；没闭合就报错。"""
    quote, i = text[0], 1
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\":
            i += 2
        elif ch == quote and quote == "'" and text[i + 1:i + 2] == "'":
            i += 2  # 单引号字符串里 '' 表示一个 '
        elif ch == quote:
            return i
        else:
            i += 1
    raise YamlSubsetError(f"第 {lineno} 行：引号没有闭合 -> {text}")


def _quoted(s, lineno):
    """把一个完整的引号字符串（首尾是配对的引号）还原成值。"""
    body = s[1:-1]
    if s[0] == "'":
        return body.replace("''", "'")
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        code = body[i + 1:i + 2]
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            i += 2
        elif code in _HEX_ESCAPES:
            width = _HEX_ESCAPES[code]
            digits = body[i + 2:i + 2 + width]
            try:
                if len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
                    raise ValueError(digits)
                out.append(chr(int(digits, 16)))
            except ValueError:
                raise YamlSubsetError(f"第 {lineno} 行：\\{code} 后面要跟 {width} 位合法的十六进制码点") from None
            i += 2 + width
        else:
            raise YamlSubsetError(
                f"第 {lineno} 行：双引号里的 \\{code} 不是 YAML 转义；"
                "Windows 路径写成 C:/work，或改用单引号 'C:\\work'")
    return "".join(out)


def _scalar(raw, lineno):
    s = raw.strip()
    if s[:1] in ("'", '"'):
        if _quoted_end(s, lineno) != len(s) - 1:
            raise YamlSubsetError(f"第 {lineno} 行：引号字符串后面还有多余内容 -> {s}")
        return _quoted(s, lineno)
    if s[:1] in ("&", "*"):
        raise YamlSubsetError(f"第 {lineno} 行：不支持 YAML 锚点/引用（& 或 *）")
    if s[:1] in ("|", ">"):
        raise YamlSubsetError(f"第 {lineno} 行：不支持块标量（| 或 >）")
    if s[:1] == "!":
        raise YamlSubsetError(f"第 {lineno} 行：不支持 YAML 标签（!）")
    if s[:1] in ("[", "{"):
        raise YamlSubsetError(f"第 {lineno} 行：行内结构只能作为整个值，不支持嵌套或当键 -> {s}")
    if s[:1] in ("}", "]", ",", "%", "@", "`") or (s[:1] in ("-", "?", ":") and s[1:2].strip() == ""):
        raise YamlSubsetError(f"第 {lineno} 行：{s[:1]!r} 不能作为裸值的开头，请给整个值加引号 -> {s}")
    if _PAIR.search(s):
        raise YamlSubsetError(f"第 {lineno} 行：值里有「冒号+空格」，请给整个值加引号 -> {s}")
    if _yaml11_word(s, TRUE):
        return True
    if _yaml11_word(s, FALSE):
        return False
    if _yaml11_word(s, NULLS):
        return None
    if _INT.fullmatch(s):
        return int(s)
    if _FLOAT.fullmatch(s):
        return float(s)
    if s in ("=", "<<") or _YAML11_INT.fullmatch(s) or _YAML11_FLOAT.fullmatch(s) \
            or _YAML11_TIMESTAMP.fullmatch(s):
        raise YamlSubsetError(
            f"第 {lineno} 行：{s!r} 在 PyYAML 里会被换算成数字、日期或特殊值，本解析器不做这种换算；"
            "当字符串用请加引号")
    return s


def _split_pair(text, lineno):
    """把 `键: 值` 拆成 (键, 值原文)；不是键值对（冒号后面没有空白）时返回 None。"""
    if text[:1] in ("'", '"'):
        end = _quoted_end(text, lineno)
        sep = re.match(r"\s*:(?:\s+|$)", text[end + 1:])
        if sep is None:
            return None
        return _quoted(text[:end + 1], lineno), text[end + 1 + sep.end():]
    sep = _PAIR.search(text)
    if sep is None:
        return None
    raw_key = text[:sep.start()].rstrip()
    if not raw_key:
        raise YamlSubsetError(f"第 {lineno} 行：键不能为空")
    # 键和值走同一套类型规则：PyYAML 会把裸键 8080、true 读成数字、布尔值，这里保持一致。
    return _scalar(raw_key, lineno), text[sep.end():]


def _split_flow(inner, lineno):
    """按引号外的逗号切分行内映射/列表的条目；引号里的逗号、括号都属于值。"""
    parts, start, i = [], 0, 0
    while i < len(inner):
        ch = inner[i]
        before = inner[start:i]
        if ch in ("'", '"') and (not before.strip() or re.search(r":\s+$", before)):
            i += _quoted_end(inner[i:], lineno) + 1
            continue
        if ch in "{}[]":
            raise YamlSubsetError(f"第 {lineno} 行：不支持嵌套的行内结构")
        if ch == ",":
            parts.append(inner[start:i].strip())
            start = i + 1
        i += 1
    last = inner[start:].strip()
    if last or parts:
        parts.append(last)
    if any(not part for part in parts[:-1]):
        raise YamlSubsetError(f"第 {lineno} 行：行内结构里有空条目（连续的逗号）")
    return [part for part in parts if part]


def _inline_map(body, lineno):
    """解析 {k: v, k2: v2}。不支持嵌套花括号。"""
    s = body.strip()
    if not s.endswith("}"):
        raise YamlSubsetError(f"第 {lineno} 行：行内映射没有闭合的 }}")
    out = {}
    for part in _split_flow(s[1:-1], lineno):
        pair = _split_pair(part, lineno)
        if pair is None:
            raise YamlSubsetError(f"第 {lineno} 行：行内映射的 '{part}' 缺少「冒号+空格」")
        key, rest = pair
        out[key] = _scalar(rest, lineno)
    return out


def _inline_list(body, lineno):
    """解析 [a, b]。条目只能是标量，不支持嵌套。"""
    s = body.strip()
    if not s.endswith("]"):
        raise YamlSubsetError(f"第 {lineno} 行：行内列表没有闭合的 ]")
    items = []
    for part in _split_flow(s[1:-1], lineno):
        if _split_pair(part, lineno) is not None:
            raise YamlSubsetError(f"第 {lineno} 行：行内列表的 '{part}' 里有「冒号+空格」，当字符串用请加引号")
        items.append(_scalar(part, lineno))
    return items


def _flow(body, lineno):
    """以 { 或 [ 开头的值：行内映射或行内列表；否则返回 None。"""
    if body.startswith("{"):
        return _inline_map(body, lineno)
    if body.startswith("["):
        return _inline_list(body, lineno)
    return None


def _starts_scalar(before, depth):
    """引号前面只有缩进、`- `、`键: `，或在行内结构里紧跟 `{`、`[`、`,` 时，它才开始一个引号字符串。

    YAML 里 `it's` 中间的撇号是普通字符；把它当引号会让后面的 # 注释被当成值吞进去。
    """
    head = before.rstrip()
    if not head or re.fullmatch(r"\s*-", head):
        return True
    if head.endswith(":") and len(head) < len(before):
        return True
    return depth > 0 and head.endswith(("{", "[", ","))


def _strip_comment(line, lineno):
    """去掉行尾注释，但不能误伤引号里的 #（如 URL 里的锚点）。"""
    depth, i = 0, 0
    while i < len(line):
        ch = line[i]
        if ch in ("'", '"') and _starts_scalar(line[:i], depth):
            # 双引号里的 \" 不结束字符串，否则 "a\"b # c" 会被从中间截断
            i += _quoted_end(line[i:], lineno) + 1
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
        if ch in "{[" and _starts_scalar(line[:i], depth):
            depth += 1  # 只有值开头的括号才开始行内结构；正则 [^'"] 里的括号是普通字符
        elif ch in "}]" and depth > 0:
            depth -= 1
        i += 1
    return line.rstrip()


def _is_item(content):
    return content == "-" or content.startswith("- ")


def _assign(mapping, key, rest, key_col, idx, rows, stack, lineno):
    """把一个键值写进映射；值为空时看下一行决定是嵌套映射、列表，还是真的 null。"""
    if rest[:1] in ("{", "["):
        mapping[key] = _flow(rest, lineno)
        return
    if rest:
        mapping[key] = _scalar(rest, lineno)
        return
    nxt = rows[idx + 1] if idx + 1 < len(rows) else None
    if nxt and (nxt[0] > key_col or (nxt[0] == key_col and _is_item(nxt[1]))):
        container = [] if _is_item(nxt[1]) else {}
        # 与键同列的列表（indentless sequence）以列表自身的缩进为边界；更深的嵌套仍以键的缩进为边界。
        stack.append([nxt[0] if nxt[0] == key_col else key_col, container, None])
    else:
        container = None
    mapping[key] = container


def loads(text):
    """把 YAML 子集文本解析成 dict。失败抛 YamlSubsetError（含行号）。"""
    if text.startswith("\ufeff"):
        text = text[1:]  # Windows 记事本保存的 UTF-8 BOM；PyYAML 同样跳过它
    rows = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw.split("#")[0]:
            raise YamlSubsetError(f"第 {lineno} 行：缩进含 Tab，YAML 只允许空格")
        line = _strip_comment(raw, lineno)
        if not line.strip():
            continue
        if line.strip() == "---":
            raise YamlSubsetError(f"第 {lineno} 行：不支持多文档分隔符 ---")
        indent = len(line) - len(line.lstrip(" "))
        rows.append((indent, line.strip(), lineno))

    root = {}
    # stack 每层是 [边界缩进, 容器, 条目所在的列]。条目列在读到容器的第一个条目时确定，
    # 之后同一容器的条目必须与它对齐。跨行书写的值（续行比同层条目缩进更深）也因此报错，
    # 否则续行里的 `version=:expected` 这类片段会被悄悄当成一个新键。
    stack = [[-1, root, None]]

    for idx, (indent, content, lineno) in enumerate(rows):
        item_row = _is_item(content)
        while len(stack) > 1 and indent <= stack[-1][0]:
            # YAML permits an "indentless sequence":
            #
            #   items:
            #   - one
            #   - two
            #
            # Keep the list parent for more items at the same indentation,
            # but pop it before the next sibling mapping key.
            if isinstance(stack[-1][1], list) and indent == stack[-1][0] and item_row:
                break
            stack.pop()
        frame = stack[-1]
        parent = frame[1]
        if frame[2] is None:
            frame[2] = indent
        elif indent != frame[2]:
            raise YamlSubsetError(
                f"第 {lineno} 行：缩进 {indent} 格，同一层的条目缩进 {frame[2]} 格。同层条目必须对齐；"
                "本解析器也不支持跨行书写的值，请写在一行（含冒号等符号时加引号）")

        if item_row:
            if not isinstance(parent, list):
                raise YamlSubsetError(f"第 {lineno} 行：列表项没有对应的父级键")
            body = content[1:].strip()
            if not body or _is_item(body):
                raise YamlSubsetError(f"第 {lineno} 行：不支持空列表项或列表套列表")
            if body[:1] in ("{", "["):
                parent.append(_flow(body, lineno))
                continue
            pair = _split_pair(body, lineno)
            if pair is None:
                parent.append(_scalar(body, lineno))
                continue
            # 列表映射：首行是一个键值，后续键与首个键对齐
            #   - id: check
            #     weight: 10
            item = {}
            parent.append(item)
            key_col = indent + len(content) - len(body)
            stack.append([indent, item, key_col])
            _assign(item, pair[0], pair[1], key_col, idx, rows, stack, lineno)
            continue

        pair = _split_pair(content, lineno)
        if pair is None:
            raise YamlSubsetError(f"第 {lineno} 行：既不是键值对也不是列表项 -> {content!r}")
        if not isinstance(parent, dict):
            raise YamlSubsetError(f"第 {lineno} 行：在列表内部出现了键值对，不支持")
        _assign(parent, pair[0], pair[1], indent, idx, rows, stack, lineno)

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
