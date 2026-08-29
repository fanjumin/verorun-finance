#!/usr/bin/env python3
"""
VeroGuard — 统一守护进程 主入口（修正版）
===============================================
在 health_guardian.py 基础上增量添加版权保护模块。
合并后重命名为 verorun-guardian systemd 服务。

特性：
  - 主循环多通道调度：健康监控 (30s) + 完整性校验 (300s) + 心跳上报 (300s)
  - 阶梯恢复：重启 → GitHub 回滚
  - 冷却期机制，防循环回滚
  - 每日快照：--snapshot 模式（修正点 3）
  - 状态文件：/var/run/verorun-guardian/status.json

用法:
    python3 veroguard/guardian.py                # 启动守护进程
    python3 veroguard/guardian.py --rollback-now  # 手动回滚
    python3 veroguard/guardian.py --snapshot      # 创建每日快照
"""
import logging
import os
import sys
import time
from datetime import datetime

# 确保能从项目根目录 import veroguard
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from veroguard import config
from veroguard.modules import health, integrity, fingerprint, runtime, communicator, executor, self_protect

# ── 日志 ────────────────────────────────────────
os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ── 初始化 ──────────────────────────────────────

def init():
    """启动初始化：写入状态文件"""
    status = {
        "guardian_version": "2.0.0",
        "started_at": datetime.now().isoformat(),
        "health": {"status": "starting", "failures": 0},
        "integrity": {"status": "pending"},
        "heartbeat": {"last_sent": "", "last_response": ""},
        "fingerprint": {"hostname": os.uname().nodename if hasattr(os, 'uname') else ""},
    }
    health._write_status_file(status)
    # Phase 5: 启动自我保护子进程
    self_protect.start_watchdog()
    logging.info("VeroGuard Guardian started (health + integrity + heartbeat)")

# ── 主循环 ──────────────────────────────────────

def main():
    # ── CLI 模式（修正点 3） ──
    if "--snapshot" in sys.argv:
        logging.info("Snapshot mode triggered")
        health.take_snapshot()
        return

    if "--rollback-now" in sys.argv:
        logging.warning("Manual rollback triggered via --rollback-now")
        health.handle_failure()
        health.send_webhook("Manual rollback executed", "critical")
        return

    init()

    last_health_check = 0
    last_integrity_check = 0
    last_heartbeat = 0
    failures = 0
    cooldown_until = 0
    integrity_failures = 0     # 连续 critical 违规计数（fail-closed 触发阈值）
    integrity_blocked = False  # 是否处于完整性受限态
    integrity_clean_streak = 0 # 受限态后连续干净检查计数（自动恢复）

    while True:
        now = time.time()

        # VR-REL-001: 执行已到期/已过期的计划停机（由 executor 写入的宽限期调度）
        scheduled = health.read_status('scheduled_shutdown', {})
        if scheduled and isinstance(scheduled, dict):
            deadline = scheduled.get('deadline', 0)
            if deadline and now >= float(deadline):
                logging.warning("Scheduled shutdown deadline reached, stopping services")
                executor._stop_services()
                health.write_status('scheduled_shutdown', {'deadline': 0, 'executed_at': now})

        # 冷却期跳过
        if now < cooldown_until:
            remaining = int(cooldown_until - now)
            if remaining % config.CHECK_INTERVAL == 0:
                logging.info("Cooldown: %ds remaining", remaining)
            time.sleep(config.CHECK_INTERVAL)
            continue

        # ── 通道 1: 健康监控 (30s) ──
        if now - last_health_check >= config.CHECK_INTERVAL:
            failures = health.run_checks(failures)
            if failures >= config.MAX_FAILURES:
                cooldown_until = health.handle_failure()
                failures = 0
            last_health_check = now

        # ── 通道 2: 完整性校验 (300s) ──
        # VR-SEC-xxx: 检测即阻断（fail-closed）— critical 连续违规进入受限态
        if now - last_integrity_check >= config.INTEGRITY_CHECK_INTERVAL:
            violations = integrity.run()
            critical = [v for v in violations
                        if v.get('severity') == 'critical']
            if critical:
                integrity_failures += 1
                integrity_clean_streak = 0
                if (config.INTEGRITY_BLOCK_ON_CRITICAL
                        and not integrity_blocked
                        and integrity_failures >= config.INTEGRITY_BLOCK_THRESHOLD):
                    integrity_blocked = True
                    integrity_failures = 0
                    logging.critical(
                        "Critical integrity violation confirmed — entering restricted mode: %s",
                        [v.get('file') for v in critical])
                    executor._cmd_lock_full(
                        'integrity-fail-closed',
                        {'reason': 'critical integrity violation',
                         'violations': [v.get('file') for v in critical]})
                    health.write_status('integrity', {
                        'status': 'blocked',
                        'last_check': datetime.now().isoformat(),
                        'blocked_at': datetime.now().isoformat(),
                        'violations': critical,
                    })
                    health.send_webhook(
                        'CRITICAL integrity violation — system locked (fail-closed)',
                        'critical')
                else:
                    health.write_status('integrity', {
                        'status': 'violated',
                        'last_check': datetime.now().isoformat(),
                        'checked_files': 'N/A',
                        'violations': violations,
                    })
            elif violations:
                # 仅 high/warning 违规：报告不阻断
                integrity_failures = 0
                health.write_status('integrity', {
                    'status': 'violated',
                    'last_check': datetime.now().isoformat(),
                    'checked_files': 'N/A',
                    'violations': violations,
                })
            else:
                if integrity_blocked:
                    integrity_clean_streak += 1
                    if integrity_clean_streak >= config.INTEGRITY_BLOCK_RECOVER:
                        integrity_blocked = False
                        integrity_clean_streak = 0
                        logging.warning("Integrity restored — exiting restricted mode")
                        health.send_webhook(
                            'Integrity restored — system unlocked', 'info')
                else:
                    integrity_clean_streak = 0
                health.write_status('integrity', {
                    'status': 'clean',
                    'last_check': datetime.now().isoformat(),
                    'violations': [],
                })
            last_integrity_check = now

        # ── 通道 3: 心跳上报 (300s) ──
        if now - last_heartbeat >= config.HEARTBEAT_INTERVAL:
            fp = fingerprint.collect()
            rt = runtime.check()
            ig = health.read_status('integrity', {})
            response = communicator.send_heartbeat(fp, ig, rt)
            health.write_status('heartbeat', {
                'last_sent': datetime.now().isoformat(),
                'last_response': response.get('status', 'unknown'),
                'pending_commands': len(response.get('commands', [])),
            })
            # Phase 4: 处理远程命令
            commands = response.get('commands', [])
            for cmd in commands:
                result = executor.execute(cmd)
                communicator.send_ack(
                    cmd.get('command_id', ''), result['status'], result.get('result', '')
                )
            last_heartbeat = now

        time.sleep(config.CHECK_INTERVAL)


if __name__ == "__main__":
    main()
