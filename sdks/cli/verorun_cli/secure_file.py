"""跨平台机密文件写入与权限收紧。

本模块存在的原因（本次修复的最高优先级问题）：
    `os.chmod(path, 0o600)` 在 Windows 上**不映射到 NTFS ACL**。实测在
    `os.name == 'nt'` 下 chmod 之后 `stat.S_IMODE()` 仍为 `0o666`，组/其他用户
    的读权限位并未被清除，即 `~/.verorun/token.json` 里保存的管理员 JWT
    实际上毫无文件权限保护。

因此这里按平台分流：
    * POSIX  —— 用 `os.open(..., 0o600)` 原子创建，避免"先 open 再 chmod"
                之间存在的可读窗口；已存在的文件补一次 chmod 收紧。
    * Windows —— 写入后调用 `icacls` 先授予当前用户、再断开继承，使文件
                仅当前账户可读写；任何一步失败都**明确告警**，绝不静默假装安全。

本模块不 import 任何 VeroRun 核心模块，仅使用标准库。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from typing import Any, Dict, List, Optional, Tuple

IS_WINDOWS = os.name == "nt"

# Windows 上被视为"过宽"的主体：出现其中任一即认为文件未受保护
_BROAD_PRINCIPALS = (
    "everyone",
    "todos",              # 西语系统的 Everyone
    "jeder",              # 德语系统的 Everyone
    "authenticated users",
    "builtin\\users",
    "users",
    "interactive",
    "network",
)

_ICACLS_TIMEOUT = 15


# --------------------------------------------------------------------------- #
# 目录 / 文件写入
# --------------------------------------------------------------------------- #
def ensure_secure_dir(path: str) -> Tuple[bool, str]:
    """创建目录并尽力收紧权限，返回 (是否已收紧, 说明)。"""
    os.makedirs(path, exist_ok=True)
    return harden(path, is_dir=True)


def write_secret(path: str, payload: Any, *, indent: Optional[int] = None) -> Tuple[bool, str]:
    """把 payload 以 JSON 写入 path，并收紧权限。

    返回 (是否确认已收紧权限, 说明)。调用方应在返回 False 时向用户告警，
    而不是假定凭证是安全的。
    """
    parent = os.path.dirname(path)
    if parent:
        ensure_secure_dir(parent)

    text = json.dumps(payload, ensure_ascii=False, indent=indent)

    if IS_WINDOWS:
        # Windows 下 os.open 的 mode 基本无效，直接普通写入后交给 icacls
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        # 原子创建：文件自诞生起就是 0o600，不存在"短暂可读"的竞态窗口
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)

    return harden(path)


def harden(path: str, *, is_dir: bool = False) -> Tuple[bool, str]:
    """收紧单个路径的权限，返回 (是否确认已收紧, 说明)。"""
    if not os.path.exists(path):
        return False, f"路径不存在：{path}"
    if IS_WINDOWS:
        return _harden_windows(path)
    return _harden_posix(path, is_dir=is_dir)


# --------------------------------------------------------------------------- #
# POSIX
# --------------------------------------------------------------------------- #
def _harden_posix(path: str, *, is_dir: bool = False) -> Tuple[bool, str]:
    mode = 0o700 if is_dir else 0o600
    try:
        os.chmod(path, mode)
    except OSError as exc:
        return False, f"chmod 失败：{exc}"
    actual = stat.S_IMODE(os.stat(path).st_mode)
    if actual & 0o077:
        return False, f"chmod 后权限仍为 {oct(actual)}（组/其他用户仍可访问）"
    return True, f"已设置为 {oct(actual)}（仅所有者可访问）"


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
def _current_windows_principal() -> Optional[str]:
    user = os.environ.get("USERNAME") or ""
    if not user:
        return None
    domain = os.environ.get("USERDOMAIN") or ""
    return f"{domain}\\{user}" if domain else user


def _run_icacls(args: List[str]) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["icacls", *args],
            capture_output=True,
            timeout=_ICACLS_TIMEOUT,
        )
    except FileNotFoundError:
        return False, "系统未提供 icacls 命令"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"icacls 执行异常：{exc}"
    out = _decode(proc.stdout)
    err = _decode(proc.stderr)
    if proc.returncode != 0:
        raw = (err or out or "").strip().splitlines()
        return False, f"icacls 返回码 {proc.returncode}：{raw[0] if raw else '无输出'}"
    return True, out.strip()


def _decode(b: bytes) -> str:
    """把 icacls 输出解码为文本。

    中文 Windows 下 icacls 默认以 GBK 输出，直接用 utf-8 解码会抛
    UnicodeDecodeError（且不被 subprocess.SubprocessError 捕获，会冲垮调用方）。
    故先试 utf-8，失败再退回 gbk 并以 replace 兜底。
    """
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("gbk", errors="replace")


def _harden_windows(path: str) -> Tuple[bool, str]:
    principal = _current_windows_principal()
    if not principal:
        return False, "无法确定当前 Windows 账户（USERNAME 环境变量为空）"

    # 顺序很关键：先授予当前用户，再断开继承。
    # 反过来做，一旦 grant 失败就可能把自己也锁在门外。
    ok, detail = _run_icacls([path, "/grant:r", f"{principal}:(R,W)"])
    if not ok:
        return False, f"授予当前用户权限失败：{detail}"

    ok, detail = _run_icacls([path, "/inheritance:r"])
    if not ok:
        return False, f"断开 ACL 继承失败（文件可能仍被父目录 ACL 放行）：{detail}"

    # 复核：确认 ACL 中不再出现过宽主体
    entries = _windows_acl_principals(path)
    broad = [e for e in entries if _is_broad(e)]
    if broad:
        return False, f"ACL 中仍存在过宽主体：{', '.join(broad)}"
    return True, f"已通过 icacls 限定为 {principal} 独占读写"


def _windows_acl_principals(path: str) -> List[str]:
    """解析 `icacls <path>` 输出，取出被授权的主体名列表。"""
    ok, out = _run_icacls([path])
    if not ok:
        return []
    principals: List[str] = []
    for idx, line in enumerate(out.splitlines()):
        line = line.strip()
        if not line or line.lower().startswith("successfully processed"):
            continue
        if idx == 0:
            # 首行形如: "C:\path\token.json NT AUTHORITY\SYSTEM:(F)"
            line = line[len(path):].strip() if line.startswith(path) else line
        if ":" not in line:
            continue
        # 主体名可能含冒号后的权限串，取最后一个 ":(" 之前的部分
        marker = line.rfind(":(")
        name = line[:marker] if marker > 0 else line.split(":")[0]
        name = name.strip()
        if name:
            principals.append(name)
    return principals


def _is_broad(principal: str) -> bool:
    low = principal.strip().lower()
    for broad in _BROAD_PRINCIPALS:
        if low == broad or low.endswith("\\" + broad):
            return True
    return False


# --------------------------------------------------------------------------- #
# 供 status / 测试使用的自检
# --------------------------------------------------------------------------- #
def describe_protection(path: str) -> Dict[str, Any]:
    """报告某个凭证文件的真实权限状态（供 `verorun status` 与测试使用）。"""
    info: Dict[str, Any] = {
        "path": path,
        "exists": os.path.exists(path),
        "platform": "windows" if IS_WINDOWS else "posix",
        "protected": None,
        "detail": "",
    }
    if not info["exists"]:
        info["detail"] = "文件不存在"
        return info

    if IS_WINDOWS:
        entries = _windows_acl_principals(path)
        broad = [e for e in entries if _is_broad(e)]
        info["acl_principals"] = entries
        info["protected"] = not broad
        info["detail"] = (
            f"ACL 存在过宽主体：{', '.join(broad)}" if broad
            else "ACL 未包含 Everyone / Users / Authenticated Users 等过宽主体"
        )
        return info

    mode = stat.S_IMODE(os.stat(path).st_mode)
    info["mode"] = oct(mode)
    info["protected"] = (mode & 0o077) == 0
    info["detail"] = (
        f"权限 {oct(mode)}，仅所有者可访问" if info["protected"]
        else f"权限 {oct(mode)}，组/其他用户仍可访问"
    )
    return info
