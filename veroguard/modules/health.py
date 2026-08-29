#!/usr/bin/env python3
"""
VeroGuard — 健康监控模块（修正版）
============================================
从 health_guardian.py 迁移核心逻辑。
修正点 1：SERVICE_MAP 使用实际 systemd 服务名。
修正点 3：保留每日快照功能 (take_snapshot)。

阶梯恢复策略：
  阶梯 1: systemctl restart <service>
  阶梯 2: 从 GitHub tag 全量回滚
"""
import json
import logging
import os
import subprocess
import time
import urllib.request
from datetime import datetime

from .. import config

# ── 状态文件读写 ────────────────────────────────

def _read_status_file():
    try:
        with open(config.STATUS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _write_status_file(data):
    os.makedirs(os.path.dirname(config.STATUS_FILE), exist_ok=True)
    with open(config.STATUS_FILE, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def write_status(section, value):
    """写入状态文件的某个 section"""
    data = _read_status_file()
    if isinstance(value, dict):
        if section not in data:
            data[section] = {}
        data[section].update(value)
    else:
        data[section] = value
    _write_status_file(data)

def read_status(section, default=None):
    """读取状态文件的某个 section"""
    data = _read_status_file()
    return data.get(section, default)

# ── 通知 ────────────────────────────────────────

def send_webhook(message: str, severity: str = "warning"):
    """发送 Webhook 通知（如已配置 URL）"""
    if not config.WEBHOOK_URL:
        return
    try:
        payload = json.dumps({
            "text": message,
            "severity": severity,
            "service": "verorun-guardian",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }).encode()
        req = urllib.request.Request(
            config.WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        logging.info("Webhook sent: %s - %s", severity, message)
    except Exception as e:
        logging.error("Webhook failed: %s", e)

# ── 系统操作 ────────────────────────────────────

def restart_service(service_name: str) -> bool:
    """重启 systemd 服务，返回是否成功"""
    try:
        result = subprocess.run(
            ["systemctl", "restart", service_name],
            timeout=15, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            logging.info("Service '%s' restarted OK", service_name)
            return True
        else:
            logging.error("Service '%s' restart failed: %s",
                          service_name, result.stderr.strip())
            return False
    except Exception as e:
        logging.error("Service '%s' restart error: %s", service_name, e)
        return False


def rollback_file(filepath: str):
    """从 GitHub tag 拉取单个文件回滚"""
    tag = config.ROLLBACK_TAG
    url = "%s/%s/%s" % (config.GITHUB_RAW_BASE, tag, filepath)
    dest = os.path.join(config.PROJECT_DIR, filepath)
    try:
        req = urllib.request.urlopen(url, timeout=15)
        content = req.read()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(content)
        logging.info("  Restored %s", filepath)
    except Exception as e:
        logging.error("  Failed %s: %s", filepath, e)


def check_all_services() -> bool:
    """检查所有服务是否正常，返回 True 表示全部正常"""
    for url in config.TARGETS:
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            if resp.status != 200:
                return False
        except Exception:
            return False
    return True

# ── 核心：健康检查 ──────────────────────────────

def run_checks(failures: int) -> int:
    """轮询所有服务 /health，返回累计失败次数"""
    all_ok = True
    status = {}
    for url in config.TARGETS:
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            ok = (resp.status == 200)
            status[url] = 'ok' if ok else 'status_%s' % resp.status
            if not ok:
                all_ok = False
        except Exception as e:
            status[url] = 'error: %s' % e
            all_ok = False

    write_status('health', {
        'status': 'ok' if all_ok else 'degraded',
        'services': status,
        'last_check': datetime.now().isoformat(),
    })
    return 0 if all_ok else failures + 1


def handle_failure() -> float:
    """阶梯恢复：重启 → 回滚，返回冷却结束时间（0 表示恢复成功）"""
    # 阶梯 1: 重启所有失败的服务
    logging.warning("Attempting restart of all services")
    for service_name in config.SERVICE_MAP.values():
        restart_service(service_name)

    time.sleep(5)

    # 验证重启是否恢复
    if check_all_services():
        logging.info("Restart fixed all services, no rollback needed")
        write_status('health', {'status': 'ok'})
        return 0

    # 阶梯 2: 全量远程回滚（修正点 6：使用扩展的文件列表；未配置远程源时跳过）
    if not config.GITHUB_RAW_BASE:
        logging.warning("Remote rollback disabled (GUARDIAN_GITHUB_RAW empty), skipping")
    else:
        logging.warning("Restart failed, rolling back from GitHub tag %s",
                        config.ROLLBACK_TAG)
        for filepath in config.FILES_TO_ROLLBACK:
            rollback_file(filepath)

    # 回滚后重启所有服务
    for service_name in config.SERVICE_MAP.values():
        restart_service(service_name)

    msg = "Rollback triggered for all services at tag %s" % config.ROLLBACK_TAG
    send_webhook(msg, "critical")

    logging.warning("Entering cooldown for %ds", config.COOLDOWN_SECS)
    return time.time() + config.COOLDOWN_SECS


# ── 每日快照（修正点 3：从 health_guardian.py 迁移） ──

def take_snapshot():
    """每日快照：自动 git commit 本地变更，作为回滚恢复点"""
    try:
        subprocess.run(
            ["git", "add", "."],
            cwd=config.PROJECT_DIR, timeout=30, check=False,
            capture_output=True,
        )
        date_str = time.strftime("%Y-%m-%d")
        result = subprocess.run(
            ["git", "commit", "-m", "auto-snapshot: %s" % date_str],
            cwd=config.PROJECT_DIR, timeout=30, check=False,
            capture_output=True, text=True,
        )
        logging.info("Snapshot: %s", result.stdout.strip()
                     if result.stdout else "no changes")
    except Exception as e:
        logging.error("Snapshot failed: %s", e)
