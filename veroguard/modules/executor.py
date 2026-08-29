#!/usr/bin/env python3
"""
VeroGuard — 远程命令执行模块（Phase 4）
=============================================
处理心跳响应中的远程命令，执行后通过 communicator 回执。

支持的命令:
  warn           — 显示警告横幅（通过状态文件传递）
  lock_ai        — 禁用 AI 功能
  lock_full      — 前端显示维护页，所有 API 返回 503
  shutdown       — 停止所有 verorun-* systemd 服务
  self_destruct  — 删除守护进程文件并停止服务
  update_config  — 更新运行参数
"""
import json
import logging
import os
import secrets
import subprocess
import time as _time

from .. import config

# ── 允许的命令白名单 ──
ALLOWED_ACTIONS = {
    'warn', 'lock_ai', 'lock_full',
    'shutdown', 'self_destruct', 'update_config',
}

# ── 破坏性命令（需本地运维密钥二次确认）──
DESTRUCTIVE_ACTIONS = {'shutdown', 'self_destruct'}


def _verify_ops_token(action: str, params: dict) -> bool:
    """破坏性命令本地二次确认：params.ops_token 须与本地 VG_OPS_CONFIRM 一致。

    未配置 VG_OPS_CONFIRM → 一律拒绝（fail-closed，比默认放行安全）。
    与 PROBE_SECRET 分离，攻击者即使能注入心跳响应也无法触发自毁/停服。
    """
    if action not in DESTRUCTIVE_ACTIONS:
        return True
    expected = config.VG_OPS_CONFIRM
    if not expected:
        return False
    return secrets.compare_digest(str(params.get('ops_token', '')), expected)


def _audit_log(action: str, command_id: str, params: dict, outcome: str):
    """破坏性命令审计日志（追加写入，只增不改）"""
    try:
        os.makedirs(os.path.dirname(config.COMMAND_AUDIT_LOG), exist_ok=True)
        with open(config.COMMAND_AUDIT_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'ts': _time.strftime('%Y-%m-%dT%H:%M:%S'),
                'action': action,
                'command_id': command_id,
                'params': params,
                'outcome': outcome,
            }, ensure_ascii=False) + '\n')
    except Exception as e:
        logging.error("Audit log write failed: %s", e)


def execute(cmd: dict) -> dict:
    """
    执行远程命令。

    参数:
        cmd: {"command_id": "...", "action": "...", "params": {...}, "nonce": "..."}

    返回:
        {"command_id": "...", "status": "executed"|"failed", "result": "..."}
    """
    command_id = cmd.get('command_id', 'unknown')
    action = cmd.get('action', '')
    params = cmd.get('params', {})

    if action not in ALLOWED_ACTIONS:
        return {
            "command_id": command_id,
            "status": "failed",
            "result": f"Unknown action: {action}",
        }

    # VR-SEC (V4): 破坏性命令二次确认（运维通道分离）
    if action in DESTRUCTIVE_ACTIONS:
        if not _verify_ops_token(action, params):
            _audit_log(action, command_id, params, 'REJECTED: ops_token invalid')
            logging.warning("Destructive command %s rejected: ops_token invalid",
                            action)
            return {
                "command_id": command_id,
                "status": "failed",
                "result": "ops_token verification failed",
            }
        _audit_log(action, command_id, params, 'APPROVED')

    handler = _ACTION_HANDLERS.get(action)
    if not handler:
        return {
            "command_id": command_id,
            "status": "failed",
            "result": f"No handler for: {action}",
        }

    try:
        result = handler(command_id, params)
        logging.info("Command %s (%s): %s", command_id, action, result)
        return {"command_id": command_id, "status": "executed", "result": result}
    except Exception as e:
        logging.error("Command %s (%s) failed: %s", command_id, action, e)
        return {"command_id": command_id, "status": "failed", "result": str(e)}


# ── 命令处理器 ──────────────────────────────────

def _cmd_warn(command_id: str, params: dict) -> str:
    """写入警告状态到状态文件"""
    from . import health
    health.write_status('remote_command', {
        'action': 'warn',
        'reason': params.get('reason', ''),
        'issued_at': _time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    return "warn message written to status file"


def _cmd_lock_ai(command_id: str, params: dict) -> str:
    """禁用 AI 功能"""
    from . import health
    health.write_status('remote_command', {
        'action': 'lock_ai',
        'reason': params.get('reason', ''),
        'issued_at': _time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    return "AI features locked"


def _cmd_lock_full(command_id: str, params: dict) -> str:
    """全站锁定 — 写入维护状态，前端检查此状态显示维护页面"""
    from . import health
    health.write_status('remote_command', {
        'action': 'lock_full',
        'reason': params.get('reason', ''),
        'issued_at': _time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    return "full system locked"


def _stop_services() -> list:
    """停止所有 verorun-* systemd 服务，返回已停止的服务名列表。"""
    stopped = []
    for service_name in config.SERVICE_MAP.values():
        try:
            subprocess.run(
                ["systemctl", "stop", service_name],
                timeout=15, capture_output=True, check=False,
            )
            stopped.append(service_name)
        except Exception as e:
            logging.error("Failed to stop %s: %s", service_name, e)
    logging.warning("Shutdown executed: %s", stopped)
    return stopped


def _cmd_shutdown(command_id: str, params: dict) -> str:
    """停止所有 verorun-* systemd 服务。
    VR-REL-001: 宽限期改为写入状态文件由主循环调度，禁止在子线程阻塞 sleep。
    """
    grace_hours = params.get('grace_period_hours', 0)
    if grace_hours > 0:
        from . import health
        deadline = _time.time() + grace_hours * 3600
        health.write_status('scheduled_shutdown', {'deadline': deadline})
        logging.warning("Shutdown scheduled for deadline %s (grace %dh)",
                        deadline, grace_hours)
        return f"Scheduled shutdown at {deadline}"

    stopped = _stop_services()
    return f"Stopped: {stopped}"


def _cmd_self_destruct(command_id: str, params: dict) -> str:
    """自毁：删除守护进程文件并停止自身"""
    # 先停止所有服务
    for service_name in config.SERVICE_MAP.values():
        try:
            subprocess.run(["systemctl", "stop", service_name],
                          timeout=15, capture_output=True, check=False)
        except Exception:
            pass

    # 删除自身
    guardian_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        import shutil
        shutil.rmtree(guardian_dir, ignore_errors=True)
        logging.warning("SELF-DESTRUCT: %s deleted", guardian_dir)
        return f"Self-destruct: {guardian_dir} deleted"
    except Exception as e:
        logging.error("Self-destruct failed: %s", e)
        return f"Self-destruct partial: {e}"


def _cmd_update_config(command_id: str, params: dict) -> str:
    """动态更新守护进程配置（写入状态文件供下次循环读取）"""
    from . import health
    health.write_status('config_update', {
        'params': params,
        'issued_at': _time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    return f"Config update written: {list(params.keys())}"


# ── 处理器映射 ──
_ACTION_HANDLERS = {
    'warn':           _cmd_warn,
    'lock_ai':        _cmd_lock_ai,
    'lock_full':      _cmd_lock_full,
    'shutdown':       _cmd_shutdown,
    'self_destruct':  _cmd_self_destruct,
    'update_config':  _cmd_update_config,
}
