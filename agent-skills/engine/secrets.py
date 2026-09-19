# -*- coding: utf-8 -*-
"""凭据后端。可切换：系统钥匙串 / SOPS+age / 环境变量。

**架构铁律（不可配置，见设计文档 §7.5）**：
  1. `secret get` 只给人和 broker 用。模型侧靠 permissions deny + hook 拦死，
     引导它改用 broker 子命令（如 `aisk db query`），而不是自己拿口令。
  2. broker 永不回显口令，错误信息里也不带——只说「哪个键取不到」，不说值。
  3. 生产 broker 只实现读，不实现写。

**为什么默认是 SOPS 而不是钥匙串**（决策 6，2026-08-24 重新确认）：
用户要求无人值守自主执行。钥匙串的信任按二进制路径授予，homebrew 升级 python
后路径变了会重新弹窗；SOPS 读 age 私钥文件，零提示零维护。
但 SOPS 需要先 `age-keygen` 初始化，所以**没初始化时自动退回钥匙串**，
保证开箱可用。

历史教训：2026-07-08 用户试过 SOPS 又当天撤销，原因是裸 `sops -d --extract`
太难用。这次全程被 `aisk` 包一层，用户不直接碰 sops 命令。
"""

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 两个命名空间，物理隔离：
#   aisk            只读凭据。broker 默认只读这里，随便取。
#   aisk-privileged 写权限凭据（root / DBA / 业务写账号 / 管理员控制台）。
#                   只有显式带 --privileged 的调用才碰得到，且必须留审计。
#
# 边界不是「AI 永远拿不到特权凭据」——AGENTS.md 明确写了管理员授权时
# AI 必须按授权范围执行。边界是「默认够不着 + 要用必须显式 + 用了留痕」。
SERVICE = "aisk"
SERVICE_PRIVILEGED = "aisk-privileged"
RUNTIME_ROOT = Path(os.environ.get("AISK_HOME") or (Path.home() / ".aisk"))
AUDIT_LOG = RUNTIME_ROOT / "privileged-access.log"
PRIVATE_ROOT = Path(os.environ.get("AISK_PRIVATE_ROOT") or RUNTIME_ROOT)


def _service(privileged=False):
    return SERVICE_PRIVILEGED if privileged else SERVICE


def audit(action, key, ok=True, note=""):
    """特权凭据的每一次取用都记一笔。只记键名，绝不记值。"""
    import datetime
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts}\t{action}\t{key}\t{'ok' if ok else 'fail'}\t{note}\n")


if sys.platform == "darwin":
    AGE_KEY = Path.home() / "Library/Application Support/sops/age/keys.txt"
elif sys.platform.startswith("win"):
    # sops（Go 程序）在 Windows 上按 os.UserConfigDir() 语义找默认路径，即
    # %AppData%，不是 Linux 的 XDG ~/.config——这两条路径长得像但不是一回事。
    # 2026-08-25 Win11 实测确认：私钥放这里，`sops -d` 不用设 SOPS_AGE_KEY_FILE
    # 就能直接解密；之前误放到 ~/.config 下时解密必然失败（sops 找不到私钥，
    # 报「failed to create reader for decrypting」，容易被误判成密钥本身有问题）。
    # `.get(k, default)` 在这里不够：沙箱化的 agent（WorkBuddy）会把 APPDATA 传成
    # 空串而不是不传，空串取不到 default，AGE_KEY 会变成相对路径。用 `or` 兜住。
    AGE_KEY = Path(os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")) / "sops/age/keys.txt"
else:
    AGE_KEY = Path.home() / ".config/sops/age/keys.txt"
SOPS_FILE = PRIVATE_ROOT / "secrets.sops.yaml"
SOPS_FILE_PRIVILEGED = PRIVATE_ROOT / "secrets-privileged.sops.yaml"


def _sops_env():
    """给 sops 子进程显式指路，不依赖它自己按环境变量猜私钥位置。

    2026-09-06 定位：WorkBuddy 的沙箱 shell 把 APPDATA 传成空串，而 sops（Go）
    的默认查找走 os.UserConfigDir()＝%AppData%，于是永远看不到
    %USERPROFILE%\\AppData\\Roaming\\sops\\age\\keys.txt，报
    「failed to create reader for decrypting ... no identity matched」——
    私钥文件在、公钥和 recipient 也对得上，只是 sops 没往那儿找。
    同一个沙箱里手动 export SOPS_AGE_KEY_FILE 再跑 sops 就正常。

    aisk 自己已经算好了 AGE_KEY，没理由让 sops 再猜一遍：直接塞
    SOPS_AGE_KEY_FILE。三端通用，mac/Linux 上也只是把本来就对的路径写死。
    """
    env = dict(os.environ)
    env["SOPS_AGE_KEY_FILE"] = str(AGE_KEY)
    return env


def _sops_path(privileged=False):
    return SOPS_FILE_PRIVILEGED if privileged else SOPS_FILE


class SecretError(Exception):
    pass


# ───────────────────────────── 钥匙串后端 ─────────────────────────────

def _kc_available():
    return sys.platform == "darwin" and shutil.which("security") is not None


def _kc_set(key, value, privileged=False):
    subprocess.run(
        ["security", "add-generic-password", "-a", key, "-s", _service(privileged),
         "-U", "-w", value],
        check=True, capture_output=True,
    )


def _kc_get(key, privileged=False):
    r = subprocess.run(
        ["security", "find-generic-password", "-a", key, "-s", _service(privileged), "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.rstrip("\n")


def _kc_del(key, privileged=False):
    r = subprocess.run(
        ["security", "delete-generic-password", "-a", key, "-s", _service(privileged)],
        capture_output=True,
    )
    return r.returncode == 0


def _kc_list(privileged=False):
    """钥匙串没有按 service 列举的官方接口，用 dump 过滤。只返回键名，不碰值。"""
    want = _service(privileged)
    r = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    keys, cur_svc, cur_acct = [], None, None
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith('"svce"'):
            cur_svc = s.split("=", 1)[-1].strip().strip('"')
        elif s.startswith('"acct"'):
            cur_acct = s.split("=", 1)[-1].strip().strip('"')
        elif s.startswith("keychain:"):
            if cur_svc == want and cur_acct:
                keys.append(cur_acct)
            cur_svc = cur_acct = None
    return sorted(set(keys))


# ────────────────────────────── SOPS 后端 ──────────────────────────────

def _sops_available():
    return shutil.which("sops") is not None and AGE_KEY.is_file()


def _sops_load(privileged=False):
    path = _sops_path(privileged)
    if not path.is_file():
        return {}
    sops_bin = shutil.which("sops")
    if not sops_bin:
        raise SecretError("sops 未安装或不在 PATH 中")
    r = subprocess.run([sops_bin, "-d", "--output-type", "json", str(path)],
                       capture_output=True, text=True, env=_sops_env())
    if r.returncode != 0:
        raise SecretError(f"sops 解密失败：{r.stderr.strip()[:200]}")
    try:
        return json.loads(r.stdout) or {}
    except json.JSONDecodeError as e:
        raise SecretError(f"sops 输出不是合法 JSON：{e}") from e


def _sops_save(data, privileged=False):
    recipients = _age_recipient()
    path = _sops_path(privileged)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    sops_bin = shutil.which("sops")
    if not sops_bin:
        raise SecretError("sops 未安装或不在 PATH 中")
    try:
        r = subprocess.run(
            [sops_bin, "-e", "--age", recipients, "--input-type", "json",
             "--output-type", "yaml", str(tmp)],
            capture_output=True, text=True, env=_sops_env(),
        )
        if r.returncode != 0:
            raise SecretError(f"sops 加密失败：{r.stderr.strip()[:200]}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r.stdout, encoding="utf-8")
        path.chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)


def _age_recipient():
    for line in AGE_KEY.read_text(encoding="utf-8").splitlines():
        if line.startswith("# public key:"):
            return line.split(":", 1)[1].strip()
    raise SecretError(f"{AGE_KEY} 里找不到公钥行（# public key: age1...）")


# ────────────────────────────── 统一接口 ──────────────────────────────

def backend_name(explicit=None):
    b = explicit or os.environ.get("AISK_SECRET_BACKEND")
    if b:
        return b
    if _sops_available():
        return "sops"
    if _kc_available():
        return "keychain"
    return "env"


def get(key, backend=None, privileged=False):
    """取一个凭据。找不到返回 None。**调用方永远不要把返回值打印出来。**

    privileged=True 会去特权命名空间取，并**强制留审计**——
    这是「授权时能用，但用了一定留痕」的落点。
    """
    b = backend_name(backend)
    if b == "env":
        pre = "AISKP_" if privileged else "AISK_"
        v = os.environ.get(pre + key.upper().replace("/", "_").replace("-", "_"))
    elif b == "keychain":
        v = _kc_get(key, privileged)
    elif b == "sops":
        v = _sops_load(privileged).get(key)
    else:
        raise SecretError(f"未知后端：{b}")
    if privileged:
        audit("get", key, ok=v is not None)
    return v


def put(key, value, backend=None, privileged=False):
    b = backend_name(backend)
    if b == "keychain":
        _kc_set(key, value, privileged)
    elif b == "sops":
        data = _sops_load(privileged)
        data[key] = value
        _sops_save(data, privileged)
    else:
        raise SecretError(f"后端 {b} 不支持写入（env 后端请自行 export）")
    if privileged:
        audit("set", key)
    return b


def delete(key, backend=None, privileged=False):
    b = backend_name(backend)
    if b == "keychain":
        ok = _kc_del(key, privileged)
        if privileged:
            audit("delete", key, ok=ok)
        return ok
    if b == "sops":
        data = _sops_load(privileged)
        if key not in data:
            return False
        del data[key]
        _sops_save(data, privileged)
        if privileged:
            audit("delete", key)
        return True
    raise SecretError(f"后端 {b} 不支持删除")


def list_keys(backend=None, privileged=False):
    """只返回键名，永远不返回值。"""
    b = backend_name(backend)
    if b == "keychain":
        return _kc_list(privileged)
    if b == "sops":
        return sorted(_sops_load(privileged).keys())
    prefix = "AISKP_" if privileged else "AISK_"
    return sorted(k[len(prefix):].lower() for k in os.environ if k.startswith(prefix))


def prompt_and_put(key, backend=None, privileged=False):
    """交互式录入。用 getpass 保证不回显、不进 shell 历史、不进 ps 输出。"""
    v1 = getpass.getpass(f"  输入 {key} 的值（不回显）: ")
    if not v1:
        raise SecretError("值为空，已取消")
    v2 = getpass.getpass("  再输入一次确认: ")
    if v1 != v2:
        raise SecretError("两次输入不一致，已取消")
    return put(key, v1, backend, privileged)


def status():
    """后端可用性体检。不碰任何值。"""
    return {
        "active": backend_name(),
        "keychain": _kc_available(),
        "sops_binary": shutil.which("sops") is not None,
        "age_key": AGE_KEY.is_file(),
        "sops_file": SOPS_FILE.is_file(),
        "sops_ready": _sops_available(),
    }
