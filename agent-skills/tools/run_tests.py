#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归入口：改动过程中跑 fast，提交前跑 full。

    python3 tools/run_tests.py fast   # 单测（除最慢的 test_worktree）+ 卫生/漂移检查
    python3 tools/run_tests.py full   # 全部单测 + 协议进程验收 + 卫生/漂移检查

**为什么要分档**：`test_task_hardening` 与 `test_worktree` 是两个耗时大头，加起来占了大半时间。
改一行等两分钟，人就会开始跳过测试——跳过测试正是这套东西最不能出的事。
fast 覆盖守卫、钩子协议、生命周期加固、迁移与文档漂移，留下的 test_worktree 是耗时最长的
集成用例，提交前必须跑。

**这里刻意不写秒数**：机器快慢不同，写死了只会腐烂（`tools/check_doc_drift.py` 也不断言耗时）。

本仓库**有 CI**（`.github/workflows/quality.yml`，push master / PR 触发），但它只跑
`./bin/aisk public verify` 与全量单测，**不跑**协议进程验收、内核卫生检查与文档漂移检查。
所以本地 `full` 仍是提交前唯一的完整门禁，别因为 CI 绿了就跳过它。

协议脚本装业务仓真实钩子时给 AISK_TEST_HOOKS_DIR=<仓库>/scripts/git-hooks（不给则跳过真实钩子相关项）。
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

KERNEL = Path(__file__).resolve().parent.parent
FAST_MODULES = ["test_kernel.py", "test_quality.py", "test_task_migration.py",
                "test_task_hardening.py", "test_doc_drift.py"]


def run(label, argv):
    start = time.perf_counter()
    result = subprocess.run(argv, cwd=KERNEL)
    seconds = time.perf_counter() - start
    status = "✅" if result.returncode == 0 else "❌"
    print(f"{status} {label}（{seconds:.1f} 秒）", flush=True)
    return result.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("level", choices=["fast", "full"])
    args = ap.parse_args()
    rc = 0
    started = time.perf_counter()
    if args.level == "fast":
        for module in FAST_MODULES:
            rc |= run(f"单测 {module}", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", module, "-q"])
    else:
        rc |= run("全部单测", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"])
        rc |= run("协议进程验收", ["bash", "tests/protocol_worktree.sh"])
    rc |= run("卫生检查", [sys.executable, "tools/check_kernel_hygiene.py"])
    rc |= run("文档漂移", [sys.executable, "tools/check_doc_drift.py"])
    print(f"\n{'通过' if rc == 0 else '失败'}：{args.level} 回归共 {time.perf_counter() - started:.1f} 秒")
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
