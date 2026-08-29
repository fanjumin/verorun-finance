#!/usr/bin/env python3
"""
Plugin Manager — 插件独立日志通道
====================================
每个插件拥有独立日志文件，位于 data/logs/plugins/<identifier>.log。

日志轮转:
  - 单文件最大 5MB
  - 最多保留 3 个备份
  - 按日期轮转（每天一个新文件）

用法:
    from plugin_manager.logger import get_plugin_logger
    log = get_plugin_logger('coupons')
    log.info('Coupon applied: order=%s, amount=%d', order_no, amount)
"""

import os
import logging
import logging.handlers
from typing import Dict, Optional

# ── 日志目录 ──────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, 'data', 'logs', 'plugins')


def ensure_log_dir():
    """确保日志目录存在"""
    os.makedirs(LOG_DIR, exist_ok=True)


# ── 日志器缓存 ────────────────────────────────────────────────────────

_loggers: Dict[str, logging.Logger] = {}
_initialized = False


def init_plugin_logging():
    """初始化插件日志系统（应用启动时调用一次）"""
    global _initialized
    if _initialized:
        return
    ensure_log_dir()
    _initialized = True


def get_plugin_logger(identifier: str) -> logging.Logger:
    """获取指定插件的日志器

    自动创建 logger，写入 data/logs/plugins/<identifier>.log。
    日志格式: 2026-07-07 10:30:00 [INFO] message
    """
    global _initialized
    if not _initialized:
        init_plugin_logging()

    if identifier in _loggers:
        return _loggers[identifier]

    logger = logging.getLogger(f'plugin.{identifier}')
    logger.setLevel(logging.DEBUG)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    log_path = os.path.join(LOG_DIR, f'{identifier}.log')

    # RotatingFileHandler: 5MB, 3 backups
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding='utf-8',
    )
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    _loggers[identifier] = logger
    return logger


def get_log_path(identifier: str) -> str:
    """获取插件日志文件路径"""
    return os.path.join(LOG_DIR, f'{identifier}.log')


def read_plugin_log(identifier: str, lines: int = 50) -> str:
    """读取插件日志的最后 N 行"""
    log_path = get_log_path(identifier)
    if not os.path.isfile(log_path):
        return ''

    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:]
        return ''.join(tail)
    except (IOError, OSError) as e:
        return f'[read error: {e}]'


def clear_plugin_log(identifier: str) -> bool:
    """清空插件日志文件"""
    log_path = get_log_path(identifier)
    if not os.path.isfile(log_path):
        return False
    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write('')
        return True
    except IOError:
        return False
