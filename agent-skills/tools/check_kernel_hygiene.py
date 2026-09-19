#!/usr/bin/env python3
"""Check public kernel files for local paths, private-network facts and secrets.

This checker is deliberately generic. Project-specific facts belong in a private
overlay and must not be encoded as public allow-list exceptions.
"""
from __future__ import annotations

import re
from pathlib import Path

KERNEL = Path(__file__).resolve().parents[1]
SKILLS = KERNEL / "skills"
DEFAULT_ROOTS = [
    SKILLS,
    KERNEL / "engine",
    KERNEL / "tools",
    KERNEL / "templates" / "worktree",
    KERNEL / "WORKTREE.md",
]

PATTERNS = (
    ("absolute-path", re.compile(r"(?:/Users/[^\s/'\"]+|/home/[^\s/'\"]+|[A-Za-z]:\\Users\\[^\s'\"]+)")),
    ("private-network", re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b")),
    ("secret-assignment", re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"][^<\"']{8,}['\"]")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)


def _files(root: Path):
    root = Path(root)
    if root.is_file():
        yield root
        return
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            yield path


def scan(root: Path):
    findings = []
    for path in _files(Path(root)):
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for name, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{path}:{line_no}:{name}")
    return findings


def main() -> int:
    findings = []
    for root in DEFAULT_ROOTS:
        findings.extend(scan(root))
    if findings:
        print("\n".join(findings))
        return 1
    print("public kernel hygiene: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
