#!/usr/bin/env python3
"""
VeroGuard — 自我保护模块（Phase 5）
=============================================
双进程守护：guardian 监控业务服务，self_protect 监控 guardian 自身。
如果 guardian 意外终止，self_protect 负责重启它。

原理:
  - guardian.py 启动时 fork 一个轻量子进程
  - 子进程定期检查父进程存活（通过 pipe/pidfile）
  - 父进程死亡 → 子进程 systemctl restart verorun-guardian
"""
import logging
import os
import signal
import subprocess
import time as _time


def start_watchdog(pidfile: str = '/var/run/verorun-guardian/guardian.pid'):
    """
    启动守护子进程。
    在 guardian.py 主进程启动时调用一次。
    子进程会监控父进程，父进程死亡时自动重启。
    """
    pid = os.fork()
    if pid > 0:
        # 父进程：记录子进程 PID，继续正常执行
        os.makedirs(os.path.dirname(pidfile), exist_ok=True)
        with open(pidfile, 'w') as f:
            # VR-REL-003: 写入 watchdog 子进程 PID（而非父进程自身 PID）
            f.write(str(pid))
        logging.info("Self-protect watchdog started (child PID: %d)", pid)
        return

    # ── 子进程：守护逻辑 ──
    _watchdog_loop(os.getppid(), pidfile)


def _watchdog_loop(parent_pid: int, pidfile: str):
    """子进程循环：监控父进程存活"""
    # 忽略终端信号，避免被误杀
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    logging.info("[watchdog] Monitoring parent PID: %d", parent_pid)

    while True:
        _time.sleep(30)

        # 检查父进程是否存活
        try:
            os.kill(parent_pid, 0)  # 信号 0 仅检查进程存在
        except OSError:
            # 父进程已死 → 重启
            logging.warning("[watchdog] Parent PID %d died, restarting...",
                          parent_pid)
            # 移除残留 pidfile
            try:
                os.remove(pidfile)
            except Exception:
                pass
            # 等待 5 秒确保资源释放
            _time.sleep(5)
            # 重启服务
            try:
                subprocess.run(
                    ["systemctl", "restart", "verorun-guardian"],
                    timeout=30, capture_output=True, check=False,
                )
                logging.info("[watchdog] verorun-guardian restarted")
            except Exception as e:
                logging.error("[watchdog] Restart failed: %s", e)
            # 退出子进程，由 systemd 重新 fork
            os._exit(0)
