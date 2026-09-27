"""零依赖 YAML 子集解析器的回归用例。

CI 用 setup-python 的解释器，没有 PyYAML，规范文件全靠 miniyaml 读；开发机装了 PyYAML
就走 PyYAML。两边读同一份文件必须得到同样的结果，读不准的写法必须报错而不是猜。
这里的期望值都写死，不依赖 PyYAML，CI 上照样生效；装了 PyYAML 时再逐条对照一遍。
"""

import subprocess
import unittest
from pathlib import Path

from engine import miniyaml

ROOT = Path(__file__).resolve().parents[2]

# (文本, 期望结果)：与 PyYAML 的读法逐条一致
EXACT = [
    # 键和值之间必须是「冒号+空格」，URL 与 build:dev 里的冒号属于值
    ("l:\n  - Vitest 单元测试与构建门禁 (build:dev)\n", {"l": ["Vitest 单元测试与构建门禁 (build:dev)"]}),
    ("l:\n  - http://host:8848\n  - a:b\n  - 'x': y\n  - \"a: b\"\n",
     {"l": ["http://host:8848", "a:b", {"x": "y"}, "a: b"]}),
    ("a:b: c\n", {"a:b": "c"}),
    ("url: http://x:80/#frag\n", {"url": "http://x:80/#frag"}),
    # 标量类型：大小写规则、十进制整数与小数都和 PyYAML 相同
    ("a: true\nb: True\nc: TRUE\nd: tRue\ne: Off\nf: y\n",
     {"a": True, "b": True, "c": True, "d": "tRue", "e": False, "f": "y"}),
    ("a: null\nb: Null\nc: nUll\nd: ~\ne:\n", {"a": None, "b": None, "c": "nUll", "d": None, "e": None}),
    ("a: 1\nb: -1\nc: +5\nd: 09\ne: 0X1F\n", {"a": 1, "b": -1, "c": 5, "d": "09", "e": "0X1F"}),
    ("a: 99.9\nb: 0.20\nc: 1.\nd: 1.0e+3\ne: 1e3\nf: 2.1.0\ng: -.5\n",
     {"a": 99.9, "b": 0.2, "c": 1.0, "d": 1000.0, "e": "1e3", "f": "2.1.0", "g": "-.5"}),
    ("a: 0:30\nb: 8080:80\nc: 2026-9-27\n", {"a": "0:30", "b": "8080:80", "c": "2026-9-27"}),
    ("8080: web\ntrue: x\n", {8080: "web", True: "x"}),
    # 引号：双引号按 YAML 转义，单引号里 '' 表示 '
    ("a: \"C:\\\\work\"\nb: 'C:\\work'\nc: 'it''s'\nd: \"\\u4e2d\\x41\\t\"\n",
     {"a": "C:\\work", "b": "C:\\work", "c": "it's", "d": "中A\t"}),
    ("a: \"q\\\"uote # not comment\" # comment\n", {"a": 'q"uote # not comment'}),
    ("'q k': v\n\"r k\": w\n", {"q k": "v", "r k": "w"}),
    # 注释：只有值开头的引号才算引号，it's 里的撇号不能把 # 注释吞进值
    ("a: it's # comment\nb: don't 'quote' me # c\nc: x#c\n", {"a": "it's", "b": "don't 'quote' me", "c": "x#c"}),
    ("pattern: \\s*['\"](?!me)[^'\"]+['\"] # note\n", {"pattern": "\\s*['\"](?!me)[^'\"]+['\"]"}),
    # 行内映射与行内列表：引号里的逗号、括号属于值
    ("a: {x: 1, y: 'a, b', z: \"{v}\"}\nb: {}\nc: {x: 1,}\n",
     {"a": {"x": 1, "y": "a, b", "z": "{v}"}, "b": {}, "c": {"x": 1}}),
    ("a: [1, 'b, c', \"d]\", true]\nb: []\nc: [http://x:80]\n",
     {"a": [1, "b, c", "d]", True], "b": [], "c": ["http://x:80"]}),
    ("l:\n  - [a, b]\n  - {x: 1}\n", {"l": [["a", "b"], {"x": 1}]}),
    # 列表映射与零缩进列表
    ("l:\n  - id: a\n    tags:\n    - x\n    - y\n    weight: 1\n  - id: b\n",
     {"l": [{"id": "a", "tags": ["x", "y"], "weight": 1}, {"id": "b"}]}),
    ("l:\n  - steps:\n    - a\n  - other\n", {"l": [{"steps": ["a"]}, "other"]}),
    ("l:\n  -   id: x\n      w: 1\n", {"l": [{"id": "x", "w": 1}]}),
    ("l:\n- a\n- b\nother: 1\n", {"l": ["a", "b"], "other": 1}),
    # Windows 记事本保存的 BOM
    ("\ufeffa: 1\n", {"a": 1}),
]

# 读不准就必须报错的写法
REFUSED = [
    # 跨行书写的值：续行曾被悄悄当成新键 `version=`（spec/state-machine.yaml 的真实回归）
    "c:\n  version_cas: UPDATE t SET v=v+1 WHERE id=:id AND\n    version=:expected\n",
    "a:\n  b: some text\n    more text\n",
    "items:\n  - foo\n    - bar\n",
    "key:\n  long text\n",
    # 同层条目没对齐
    "a:\n  b: 1\n c: 2\n",
    "a:\n    b: 1\n  c: 2\n",
    "l:\n  - id: check\n      more: x\n",
    # PyYAML 会换算成别的类型：八进制、十六进制、下划线、六十进制、.5、inf、日期
    "a: 010\n", "a: 0x1F\n", "a: 0b101\n", "a: 1_000\n", "a: 1:30\n", "a: 30080:30\n", "a: .5\n",
    "a: .inf\n", "a: .nan\n", "a: 2026-09-27\n", "a: 2026-09-27 10:00:00\n", "a: =\n",
    # 引号
    "a: \"C:\\work\"\n", "a: 'x\n", "a: \"x\" y\n",
    # 值里有「冒号+空格」、非法开头
    "a: b: c\n", "a: - b\n", "a: @x\n", "a: `x`\n", "key:value\n", ": x\n",
    # 子集外的语法
    "a: |\n  x\n", "a: &x 1\n", "a: *x\n", "a: !!str 1\n", "a: 1\n---\nb: 2\n",
    "a: {x: {y: 1}}\n", "a: [[1]]\n", "a: [a: 1]\n", "a: {x:1}\n", "a: {x: 1,, y: 2}\n",
    "l:\n  - - a\n", "l:\n  -\n    x: 1\n",
    "a:\n\tb: 1\n",
]


class MiniYamlTests(unittest.TestCase):
    def test_reads_the_subset_exactly(self):
        for text, expected in EXACT:
            with self.subTest(text=text):
                got = miniyaml.loads(text)
                self.assertEqual(got, expected)
                self.assertEqual(repr(got), repr(expected), "类型也必须一致（99.9 是小数，不是字符串）")

    def test_refuses_what_it_cannot_read_exactly(self):
        for text in REFUSED:
            with self.subTest(text=text):
                with self.assertRaises(miniyaml.YamlSubsetError) as ctx:
                    miniyaml.loads(text)
                self.assertIn("行", str(ctx.exception), "报错必须带行号")

    def test_never_disagrees_with_pyyaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("没有 PyYAML（CI 环境），上面写死的期望值已覆盖同样的用例")
        for text in [text for text, _ in EXACT] + REFUSED:
            with self.subTest(text=text):
                try:
                    ours = miniyaml.loads(text)
                except miniyaml.YamlSubsetError:
                    continue  # 报错永远安全：宁可报错，不可猜错
                self.assertEqual(repr(ours), repr(yaml.safe_load(text)))

    def test_every_tracked_spec_and_template_parses_the_same(self):
        listed = subprocess.run(
            ["git", "ls-files", "*.yaml", "*.yml"], cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
        files = [name for name in listed if not name.startswith(".github/")]
        self.assertGreaterEqual(len(files), 12)
        try:
            import yaml
        except ImportError:
            yaml = None
        for name in files:
            with self.subTest(file=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                ours = miniyaml.loads(text)
                if yaml is not None:
                    self.assertEqual(repr(ours), repr(yaml.safe_load(text)))

    def test_spec_values_that_were_misread_before(self):
        def load(name):
            return miniyaml.loads((ROOT / name).read_text(encoding="utf-8"))

        cas = load("spec/state-machine.yaml")["concurrency_control"]
        self.assertEqual(set(cas) & {"version="}, set(), "续行不能再变成一个新键")
        self.assertTrue(cas["version_cas"].endswith("AND version=:expected"))
        skills = load("spec/skill-migration-map.yaml")["skills"]
        self.assertIn("Vitest 单元测试与构建门禁 (build:dev)", skills["web-engineering"]["retained_capabilities"])
        self.assertEqual(load("spec/acceptance-matrix.yaml")["pass_threshold"], 99.9)
        self.assertEqual(load("spec/token-efficiency.yaml")["budgets"]["max_duplicate_line_ratio"], 0.2)


if __name__ == "__main__":
    unittest.main()
