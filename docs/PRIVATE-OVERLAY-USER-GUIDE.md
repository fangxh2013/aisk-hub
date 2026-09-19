# 私有 Overlay 授权用户部署指南

本文面向已经获得 `fangxh2013/aisk-private` 只读权限的同事。没有私有仓权限时，不能安装
新华业务私有技能；请只执行公开仓 README 的 A 流程。本文不包含服务器地址、账号、Token 或
业务配置正文，所有路径都由使用者在本机填写。

## 1. 安装后的目录边界

建议每位使用者保持以下目录关系；运行态不放进任何 Git 仓库：

```text
~/work/aisk/
├── aisk-hub/                 # Public，公共内核
└── aisk-private/             # Private，仅授权用户可读

~/.aisk/                      # 用户配置、profile、凭据引用
└── profiles/

~/.aisk-runtime/hub/          # 任务状态、备份、审计和生成文件
```

不要把 `~/.aisk`、`~/.aisk-runtime`、真实 profile、密钥或服务器配置提交到任一仓库。

## 2. 从零安装

```bash
mkdir -p "$HOME/work/aisk"

git clone https://github.com/fangxh2013/aisk-hub.git \
  "$HOME/work/aisk/aisk-hub"

git clone https://github.com/fangxh2013/aisk-private.git \
  "$HOME/work/aisk/aisk-private"

export AISK_HUB_ROOT="$HOME/work/aisk/aisk-hub"
export AISKHUB_PRIVATE_ROOT="$HOME/work/aisk/aisk-private"
export AISKHUB_PROFILE_DIR="$HOME/.aisk/profiles"

cd "$AISK_HUB_ROOT"
./bin/aisk public verify
```

私有仓库必须由 GitHub 组织管理员授予读权限。不要把私有仓库复制进公共仓库，也不要使用
`--overlay-from` 绕过 Manifest 的批准清单；该选项只用于维护者的单技能迁移演练。

## 3. 校验内核、Manifest 和 lock

先校验，再分发：

```bash
python3 "$AISKHUB_PRIVATE_ROOT/tools/validate_overlay.py" \
  --root "$AISKHUB_PRIVATE_ROOT" \
  --kernel-root "$AISK_HUB_ROOT"

PYTHONPATH="$AISK_HUB_ROOT/agent-skills" \
python3 "$AISK_HUB_ROOT/tools/verify_spec.py" \
  --private-manifest "$AISKHUB_PRIVATE_ROOT/OVERLAY_MANIFEST.yaml" \
  --overlay-lock "$AISKHUB_PRIVATE_ROOT/tools/overlay.lock.json" \
  --require-real \
  --no-report-files
```

必须同时满足：

- `kernel_commit` 与公开内核检出一致；
- `kernel_content_digest` 与公开 HEAD archive 摘要一致；
- `overlay_digest` 与 Manifest 内容一致；
- Manifest 中声明的每个 `custom_skills` 都存在且含 `SKILL.md`。

任何校验失败都停止安装，不删除字段、不改成 placeholder、不手工跳过 lock。

## 4. 安装技能和权限

先观察目标端与待回收内容：

```bash
./bin/aisk link claude codex antigravity workbuddy workbuddy-ai --dry-run
./bin/aisk permit --dry-run
```

确认输出只涉及预期用户目录后，再执行：

```bash
./bin/aisk link claude codex antigravity workbuddy workbuddy-ai
./bin/aisk permit
```

`link` 只加载公共内核技能和 Manifest `custom_skills` 白名单中的私有技能。私有仓库里用于
回滚的旧技能即使仍在磁盘上，也不会因为目录存在而被自动加载。

## 5. 配置项目 profile

```bash
mkdir -p "$AISKHUB_PROFILE_DIR"
cp "$AISKHUB_PRIVATE_ROOT/profiles/xinhua-template.yaml" \
  "$AISKHUB_PROFILE_DIR/xinhua.yaml"
```

编辑本机的 `xinhua.yaml`，填写本机仓库路径和脱敏事实位置；真实密码、Token、数据库正文、
Nacos 内容和生产地址不得写入 Git。然后执行：

```bash
./bin/aisk --profile xinhua profile
./bin/aisk --profile xinhua doctor
```

## 6. 升级与回滚

升级顺序固定为：更新公共仓 → 更新私有仓 → 校验 lock → dry-run → 人工确认 → link。升级前
保存当前 `OVERLAY_MANIFEST.yaml` 和 `tools/overlay.lock.json` 的 Git 提交号；回滚时恢复这
两个文件到同一历史版本，重新执行第 3 节校验，再重新 link。

## 7. 常见故障

| 现象 | 处理 |
|---|---|
| 没有私有技能 | 确认 GitHub 权限、`AISKHUB_PRIVATE_ROOT` 和 Manifest `custom_skills` |
| lock 不匹配 | 同步公开仓与私有仓的已批准提交，不要手改摘要 |
| profile 找不到 | 设置 `AISKHUB_PROFILE_DIR`，并使用 `--profile` 显式指定 |
| 目标端同名技能未覆盖 | 先检查是否为未受 aisk 管理的第三方技能；系统默认保留它 |
| 弹窗身份不对 | 停止高危动作，检查 `--tool`、任务号和会话号，禁止降级绕过 |

真实客户端发现、钩子和弹窗联调须单独记录为 `real_runtime_score`；本地 dry-run 不得冒充
实机通过。
