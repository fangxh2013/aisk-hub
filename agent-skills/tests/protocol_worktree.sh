#!/bin/bash
# 协议进程验收（WORKTREE.md 验收一节）：真实子进程调用 `aisk task` 与四端钩子入口，全部在临时沙箱里进行。
# 不碰任何真实仓库与用户配置；promote 只做 --dry-run（不推送、不弹确认框）。
# 用法：AISK_TEST_HOOKS_DIR=<业务仓库的 git 钩子目录> bash tests/protocol_worktree.sh
#       钩子目录需含 pre-commit、commit-msg、pre-push、reference-transaction；不设置时跳过真实钩子相关的检查。
set -u
KERNEL="$(cd "$(dirname "$0")/.." && pwd -P)"
AISK="$KERNEL/bin/aisk"
SRC_HOOKS="${AISK_TEST_HOOKS_DIR:-}"
SB="$(mktemp -d "${TMPDIR:-/tmp}/aisk-task-protocol.XXXX")"
SB="$(cd "$SB" && pwd -P)"
export HOME="$SB/home" AISK_HOME="$SB/aisk" GIT_CONFIG_GLOBAL="$SB/gitconfig" GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=Tester GIT_AUTHOR_EMAIL=tester@example.test GIT_COMMITTER_NAME=Tester GIT_COMMITTER_EMAIL=tester@example.test
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION_ID AISK_TOOL AISK_SESSION CLAUDE_PID CLAUDE_ENV_FILE CLAUDE_PROJECT_DIR
# 清掉所有会让 detect_tool() 认出调用者的变量。**不能用 grep 做前缀匹配**：
# macOS 是 BSD grep，BRE 的 `\|` 不生效，`grep -o '^\(CODEX_\|CODEBUDDY_\)[A-Z_]*'`
# 实测命中 0 个。于是从 WorkBuddy/CodeBuddy 家族会话里跑本脚本时，沙箱里
# CODEBUDDY_* 原样留着 → detect_tool() 返回 workbuddy → 脚本自己的
# `aisk task release T001` 被当成「别的 AI 在动」拒绝 → 后面 9 项连锁失败。
# 假红和假绿一样坏：它让真正的问题淹没在噪音里。用 shell 自己的前缀匹配，不依赖 grep 方言。
while IFS='=' read -r _name _; do
  case "$_name" in
    AISK_TOOL|AISK_SESSION|CLAUDECODE|CLAUDE_CODE_*|CLAUDE_PID|CLAUDE_ENV_FILE|CLAUDE_PROJECT_DIR|CODEX_*|CODEBUDDY_*)
      unset "$_name" ;;
  esac
done < <(env)
# 清理失败就当场说清楚，别让它以「任务生命周期坏了」的样子冒出来。
DETECTED="$(python3 -c "import sys; sys.path.insert(0, '$KERNEL'); from engine.worktree import actor; print(actor.detect_tool() or '')")"
if [ -n "$DETECTED" ]; then
  echo "❌ 沙箱环境清理失败：detect_tool() 仍识别出「$DETECTED」"
  echo "   这样沙箱里的 aisk task 会把本次运行当成外部 AI，release/ready 会被拒绝。"
  echo "   请检查上面的 unset 循环是否漏了这类环境变量前缀。"
  exit 1
fi
mkdir -p "$HOME/.claude" "$AISK_HOME/profiles"
PASS=0; FAIL=0

aisk_task() { python3 "$AISK" task "$@"; }
check() { # 期望退出码 描述 命令...
  local want="$1" desc="$2"; shift 2
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" = "$want" ]; then PASS=$((PASS+1)); echo "  ✓ $desc (rc=$rc)"; else FAIL=$((FAIL+1)); echo "  ✗ $desc (rc=$rc, 期望 $want)"; echo "$out" | tail -15 | sed 's/^/      /'; fi
  LAST="$out"
}
contains() { # 描述 子串
  if printf '%s' "$LAST" | grep -qF -- "$2"; then PASS=$((PASS+1)); echo "  ✓ $1"; else FAIL=$((FAIL+1)); echo "  ✗ $1（输出里没有：$2）"; printf '%s\n' "$LAST" | tail -10 | sed 's/^/      /'; fi
}
hook_json() { # JSON 参数...：把 JSON 从 stdin 交给钩子或守卫入口
  local json="$1"; shift
  printf '%s' "$json" | python3 "$AISK" task "$@"
}
codex_shell() { # 工作目录 命令：生成 Codex shell 工具的钩子输入
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"shell","cwd":sys.argv[1],"session_id":"cx-1","tool_input":{"command":["bash","-lc",sys.argv[2]]}}))' "$1" "$2"
}

echo "== 沙箱：$SB"
REPO="$SB/main/backend"; mkdir -p "$REPO/scripts/git-hooks" "$REPO/modules"
git -C "$REPO" init -q -b fxh
if [ -n "$SRC_HOOKS" ]; then
  cp "$SRC_HOOKS"/{pre-commit,commit-msg,pre-push,reference-transaction} "$REPO/scripts/git-hooks/"
else
  echo "（未设置 AISK_TEST_HOOKS_DIR：不装真实钩子）"
fi
echo baseline > "$REPO/modules/demo.txt"
printf 'target/\nnode_modules/\n.claude/settings.local.json\n' > "$REPO/.gitignore"
git -C "$REPO" add -A && git -C "$REPO" commit -qm "feat: 初始化"
git -C "$REPO" branch dev && git -C "$REPO" branch main
git init -q --bare "$SB/remote.git"
git -C "$REPO" remote add origin "$SB/remote.git"
git -C "$REPO" push -q origin dev main fxh:fxh-dev && git -C "$REPO" fetch -q origin
git -C "$REPO" config core.hooksPath scripts/git-hooks

cat > "$AISK_HOME/profiles/e2e.yaml" <<EOF
project: e2e
repos:
  backend: $REPO
worktrees:
  data_root: $SB/data
  integration_branch: fxh
  protected:
    - main
    - dev
    - fxh
  lease: {idle_minutes: 30, claimable_minutes: 120}
  sensitive_paths:
    - scripts/git-hooks/**
  forbidden_commands:
    scripts/bootstrap\.sh: 任务里不运行初始化脚本
  repos:
    be:
      profile_repo: backend
      trunk: dev
      push_branch: fxh-dev
      promote: ff-trunk
      hooks_required: true
      hooks_path: scripts/git-hooks
      anchors:
        - dev
        - main
      gate:
        kind: command
        argv:
          - "true"
EOF
export AISK_PROFILE=e2e

echo "== 初始化与机器配置"
check 0 "init 预览" aisk_task init
check 0 "init --apply 建锚点/门禁区/hub" aisk_task init --apply
check 1 "bind 预览报告漂移" aisk_task bind
check 0 "bind --apply 写入（沙箱 HOME 与 GIT_CONFIG_GLOBAL）" aisk_task bind --apply
check 0 "bind 再比对零漂移" aisk_task bind
check 0 "doctor 通过" aisk_task doctor

echo "== Codex 开任务（显式会话号）"
check 0 "new（Codex 会话 cx-1）" aisk_task new coupon-claim-lock --title 优惠券领取加锁 --repos be --goal "领券接口加锁" --accept "并发领券不超发" --tool codex --session cx-1
TD="$SB/data/tasks/T001-coupon-claim-lock"; WT="$TD/be"
check 0 "worktree 在任务分支上" git -C "$WT" rev-parse --abbrev-ref HEAD
contains "分支名 ai/T001-coupon-claim-lock" "ai/T001-coupon-claim-lock"
check 0 "管理目录名 aisk-task-T001-be" git -C "$WT" rev-parse --git-dir
contains "管理目录名" "worktrees/aisk-task-T001-be"
check 0 "includeIf 生效" git -C "$WT" config --get aisk.taskWorktree
check 0 "传输封锁生效" git -C "$WT" config --get protocol.allow
contains "protocol.allow=never" "never"
check 128 "真实 push 被挡住" git -C "$WT" push -q origin HEAD:refs/heads/probe
check 128 "真实 fetch 被挡住" git -C "$WT" fetch -q origin
check 1 "相似任务被查重拦下" aisk_task new coupon-claim-limit --title 优惠券领取加锁防重 --repos be --goal g --accept a --tool codex --session cx-2
contains "列出候选" "T001"
check 1 "同工具另一会话认领被拒" aisk_task claim T001 --tool codex --session cx-2
check 1 "缺会话号的认领被拒" aisk_task claim T001 --tool workbuddy
contains "要求会话号" "会话号"
check 1 "Claude 认领被拒" env CLAUDECODE=1 CLAUDE_CODE_SESSION_ID=cc-1 python3 "$AISK" task claim T001
contains "显示持有者" "codex"
check 0 "四端钩子文件齐全" test -f "$TD/.claude/settings.json" -a -f "$TD/.codex/hooks.json" -a -f "$TD/.codebuddy/settings.json" -a -f "$TD/.agents/hooks.json"

echo "== 守卫进程（四端协议）"
check 0 "Claude 写文件被租约守卫拒绝" hook_json "{\"tool_name\":\"Write\",\"cwd\":\"$TD\",\"session_id\":\"cc-1\",\"tool_input\":{\"file_path\":\"$WT/modules/demo.txt\"}}" guard --tool claude
contains "deny 协议" '"permissionDecision": "deny"'
for cmd in "git push origin HEAD" "sh -c 'git push origin HEAD'" "/usr/bin/git push" "git -c protocol.allow=always fetch" "bash scripts/bootstrap.sh"; do
  check 0 "Codex 越界命令被拒：$cmd" hook_json "$(codex_shell "$WT" "$cmd")" guard --tool codex
  contains "拒绝：$cmd" '"permissionDecision": "deny"'
done
check 0 "持有者普通读写放行" hook_json "$(codex_shell "$WT" "git status && echo ok > note.txt")" guard --tool codex
[ -z "$LAST" ] && { PASS=$((PASS+1)); echo "  ✓ 放行时无输出"; } || { FAIL=$((FAIL+1)); echo "  ✗ 放行时有输出：$LAST"; }
rm -f "$WT/note.txt"
check 0 "WorkBuddy 另一会话改文件被拒" hook_json "{\"tool_name\":\"Edit\",\"cwd\":\"$TD\",\"session_id\":\"wb-1\",\"tool_input\":{\"file_path\":\"$WT/modules/demo.txt\"}}" guard --tool workbuddy
contains "持有中" "持有中"
check 0 "Antigravity 命令越界被拒（decision 协议）" hook_json "{\"toolCall\":{\"name\":\"run_command\",\"args\":{\"CommandLine\":\"git push\",\"Cwd\":\"$WT\"}},\"conversationId\":\"ag-1\",\"workspacePaths\":[\"$TD\"]}" guard --tool antigravity
contains "decision deny" '"decision": "deny"'
echo "== 任务外 cwd 不能关掉守卫（钩子命令自带任务根）"
for tool in claude codex workbuddy; do
  check 0 "$tool 报任务外 cwd 仍被拒" hook_json "{\"tool_name\":\"Bash\",\"cwd\":\"$REPO\",\"session_id\":\"cx-1\",\"tool_input\":{\"command\":\"git push origin dev\",\"workdir\":\"$REPO\"}}" guard --tool $tool --task-root "$TD"
  contains "拒绝 ${tool}" '"permissionDecision": "deny"'
done
check 0 "Antigravity 报任务外 Cwd 仍被拒" hook_json "{\"toolCall\":{\"name\":\"run_command\",\"args\":{\"CommandLine\":\"git push origin dev\",\"Cwd\":\"$REPO\"}},\"conversationId\":\"ag-1\",\"workspacePaths\":[\"$REPO\"]}" guard --tool antigravity --task-root "$TD"
contains "decision deny" '"decision": "deny"'
check 0 "生成的钩子命令自带 --task-root" grep -q -- "--task-root $TD" "$TD/.claude/settings.json" "$TD/.codex/hooks.json" "$TD/.codebuddy/settings.json" "$TD/.agents/hooks.json"

check 0 "Antigravity 读文件放行（输出 {}）" hook_json "{\"toolCall\":{\"name\":\"view_file\",\"args\":{\"AbsolutePath\":\"$WT/modules/demo.txt\"}},\"conversationId\":\"ag-1\",\"workspacePaths\":[\"$TD\"]}" guard --tool antigravity
contains "放行" "{}"

echo "== 真实钩子下提交、记进度、暂停"
echo coupon > "$WT/modules/demo.txt"
if [ -n "$SRC_HOOKS" ]; then
  check 1 "commit-msg 拦住含工具名的说明" git -C "$WT" commit -qam "feat: codex 加锁"
fi
check 0 "规范说明提交通过真实钩子" git -C "$WT" commit -qam "feat(coupon): 领券加锁"
check 0 "note" aisk_task note T001 --done "加锁完成" --next "补并发测试" --tool codex --session cx-1
echo "wip" >> "$WT/modules/demo.txt"
HEAD_BEFORE="$(git -C "$WT" rev-parse HEAD)"
check 0 "pause（快照在制品并交还）" aisk_task pause T001 --next "补并发测试" --tool codex --session cx-1
check 0 "暂停没有产生提交" test "$(git -C "$WT" rev-parse HEAD)" = "$HEAD_BEFORE"
check 0 "在制品快照可读" git -C "$REPO" show refs/aisk/wip/T001-coupon-claim-lock/be:modules/demo.txt
contains "快照内容" "wip"
check 0 "看板显示暂停可接手" aisk_task status
contains "暂停分组" "## 暂停可接手（1）"
contains "下一步" "补并发测试"
check 0 "context 摘要" aisk_task context T001
contains "目标" "领券接口加锁"

echo "== Antigravity 与 WorkBuddy 会话事件"
check 0 "Antigravity 每轮开始认领（injectSteps）" hook_json "{\"conversationId\":\"ag-1\",\"invocationNum\":1,\"workspacePaths\":[\"$TD\"]}" hook antigravity --event PreInvocation
contains "临时消息" "ephemeralMessage"
contains "会话标识" "--session ag-1"
check 0 "操作者交还" aisk_task release T001
check 0 "WorkBuddy 会话开始认领" hook_json "{\"hook_event_name\":\"SessionStart\",\"cwd\":\"$TD\",\"session_id\":\"wb-1\"}" hook workbuddy
contains "认领上下文" "已为本会话认领"
check 0 "WorkBuddy 会话结束自动交还" hook_json "{\"hook_event_name\":\"SessionEnd\",\"cwd\":\"$TD\",\"session_id\":\"wb-1\",\"reason\":\"exit\"}" hook workbuddy
check 0 "交还后无人持有" aisk_task status --brief
contains "无人" "无人"

echo "== Claude 会话接手"
check 0 "SessionStart 钩子自动认领" hook_json "{\"hook_event_name\":\"SessionStart\",\"cwd\":\"$TD\",\"session_id\":\"cc-1\"}" hook claude
contains "注入上下文" "已为本会话认领"
check 0 "另一 Claude 会话启动只读" hook_json "{\"hook_event_name\":\"SessionStart\",\"cwd\":\"$TD\",\"session_id\":\"cc-9\"}" hook claude
contains "只读提示" "只读"
check 0 "Claude 续做提交" git -C "$WT" commit -qam "test(coupon): 补并发测试"
sed -i '' 's/（待填写）/无/g' "$TD/HANDOFF.md"
check 0 "check" env CLAUDECODE=1 CLAUDE_CODE_SESSION_ID=cc-1 python3 "$AISK" task check T001
check 0 "ready" env CLAUDECODE=1 CLAUDE_CODE_SESSION_ID=cc-1 python3 "$AISK" task ready T001

echo "== 落地与上主干预演"
FXH_BEFORE="$(git -C "$REPO" rev-parse fxh)"
check 0 "land --dry-run" aisk_task land T001 --dry-run
check 0 "dry-run 不动集成分支" test "$(git -C "$REPO" rev-parse fxh)" = "$FXH_BEFORE"
check 0 "land" aisk_task land T001
check 0 "集成分支快进到门禁验证过的合并提交" git -C "$REPO" log --oneline -1 fxh
contains "落地说明" "merge: 落地优惠券领取加锁(T001)"
check 0 "主工作区干净" test -z "$(git -C "$REPO" status --porcelain)"
check 0 "promote --dry-run" aisk_task promote --repos be --dry-run
contains "预演不推送" "dry-run"
check 0 "远端 dev 未变" test "$(git -C "$SB/remote.git" rev-parse dev)" = "$(git -C "$REPO" rev-parse origin/dev)"
check 0 "SessionEnd 钩子（任务已落地，不报错）" hook_json "{\"hook_event_name\":\"SessionEnd\",\"cwd\":\"$TD\",\"session_id\":\"cc-1\",\"reason\":\"exit\"}" hook claude
check 0 "doctor 仍通过" aisk_task doctor
check 0 "看板" aisk_task status
contains "已落地分组" "## 已落地待上主干（1）"

echo
echo "通过 $PASS 项，失败 $FAIL 项"
[ "$FAIL" = 0 ] && rm -rf "$SB" || echo "失败现场保留在 $SB"
[ "$FAIL" = 0 ]
