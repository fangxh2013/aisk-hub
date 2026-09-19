#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩技能 description 的常驻 token 成本。

**为什么需要这一步**：description 是唯一无条件常驻的部分，每次会话开场都要付费。
主库原描述是写给人看的段落（含背景、举例、完整句子），但它真正的功能只有两条：
  1. 让模型判断「这个任务该不该用我」
  2. 路由：明确说清什么情况该去用别的技能

保留这两条功能所需的关键词与路由指针，去掉叙述性文字，实测可从平均 98 → 75 tok。
被删掉的背景说明不丢失——它们本来就在正文里有更完整的版本。

用法：python3 tools/trim_descriptions.py [--dry-run]
"""

import argparse
import re
import sys
from pathlib import Path

KERNEL = Path(__file__).resolve().parent.parent

# 只改写最占成本的几个。其余本来就短，改写收益小、引入错误风险高。
TRIMMED = {
    "dev-rules": "统一编码规范（唯一来源）：后端 Java/Spring Boot、前端 Vue/Element Plus、数据库 DDL/SQL、Nacos 配置、用语规范。生成代码后逐条自查；写注释/日志/文档前先看用语规范。勿用：后端怎么写→dev-code；前端怎么写→dev-web-code；提交发版→git-flow。触发：编码规范、命名规范、用语规范、规则对齐、自查。",

    "dev-web-code": "管理后台前端开发（xinhua-platform-web）：页面定位、views/api/store/router/permission 改动、Element Plus CRUD、字典与权限联动、biz-* 菜单图标、构建验证与排障。勿用：多 AI 派活→ai-worktree。触发：前端页面、管理页、CRUD、字典、权限联动、菜单图标。",

    "brainstorming": "需求澄清与方案探索。接到模糊需求、一句话需求、改造/重构诉求时先用：问清目标、边界、约束、验收，对比多方案，产出最小可执行计划。禁止需求没厘清就写代码。产出可直接喂给 change-safety / systematic-debugging。触发：需求不清、帮我加个功能、怎么设计、方案对比。",

    "dev-code": "后端开发（xinhua-platform Java 微服务）：模块目录定位、Controller/Service/Mapper/XML 分层归位、Feign 契约、事务、权限、数据范围、最小编译验证与排障。勿用：多 AI 派活→ai-worktree；编码规范→dev-rules。触发：写接口、改后端、加字段、Feign、事务、编译报错。",

    "ai-worktree": "多 AI 并行开发的 worktree 编排：给 Claude/Codex/Gemini/WorkBuddy 派活、体检、增删 agent、回收 agent 分支。勿用：合入 dev→git-flow；写代码规范→dev-code。worktree 内的 agent 读该目录 AGENT-WORKTREE.md。触发：多AI、并行开发、worktree、派活、agent分支、隔离工作区。",

    "db-schema-backup": "多环境表结构备份与对齐：DEV/PRE/PROD 纯 DDL 导出、分库与全量结构落盘、基于 DEV 基线生成幂等对齐 DDL。勿用：建表与索引设计→database-design；线上只读排查→ops。触发：备份表结构、导出表结构、表结构对齐、比对表结构。",

    "change-safety": "变更安全：动手前先想清怎么验证，改完必须过最小门槛（后端 mvn compile / 前端 npm run build:dev）并逐项回归受影响功能，禁止改完就交。适配受限构建环境（构建走 Jenkins、order 模块本地不可编译）。触发：改完怎么验证、会不会影响别的、回归、验证门槛。",

    "systematic-debugging": "系统化排障：bug、报错、接口 404、页面空白、分页 total=0、数据不一致、线上与本地不一致时用。先复现、再分层定位根因、最小验证后才改，禁止盲改或 try-catch 掩盖。内置高频故障目录，按现象直查根因。触发：报错、不生效、查不到数据、和预期不一样、排查。",

    "security-review": "后端安全编码审查（区别于 Claude 内置 security-review 的 diff 审查）：认证鉴权、敏感数据脱敏、输入校验、文件上传、限流、审计日志。触发：安全审查、鉴权、JWT、Token、XSS、SQL注入、脱敏、文件上传、越权、限流。",

    "api-design": "API 接口设计规范：统一响应格式、错误码、分页、VO/DTO、Feign 客户端、OpenAPI 注解。触发：接口设计、统一响应、错误码、分页、Feign、Swagger、VO、DTO。",

    "frontend-review": "前端代码审查（xinhua-platform-web）：Vue 最佳实践、性能与代码质量，优先对照 dev-rules 与实战事故案例。勿用：提交前跨层审查→pre-commit-review。触发：前端代码审查、review 一下、性能优化、代码质量。",
}


def trim(dry=False):
    changed = 0
    for name, desc in TRIMMED.items():
        f = KERNEL / "skills" / name / "SKILL.md"
        if not f.is_file():
            print(f"  ⚠️ 跳过（不存在）: {name}")
            continue
        text = f.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            print(f"  ⚠️ 跳过（无 frontmatter）: {name}")
            continue
        fm, body = m.group(1), m.group(2)
        new_fm = re.sub(
            r"^description:.*?(?=^[a-z_]+:|\Z)",
            f"description: >\n  {desc}\n",
            fm, count=1, flags=re.S | re.M,
        )
        if new_fm == fm:
            print(f"  ⚠️ 未匹配到 description: {name}")
            continue
        if not dry:
            f.write_text(f"---\n{new_fm.rstrip()}\n---\n{body}", encoding="utf-8")
        changed += 1
    return changed


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    n = trim(a.dry_run)
    print(f"{'[dry-run] ' if a.dry_run else ''}改写 {n} 个 description")
    sys.exit(0)
