#!/usr/bin/env python3
"""
Unified logging for VeroRun.

Provides `get_logger(name)` that returns a logger with:
  - Console handler (human-readable, coloured via default StreamHandler)
  - JSON file handler (structured, for log aggregation)

Log level controlled by LOG_LEVEL env var (default: INFO).
JSON log file path controlled by LOG_FILE env var (default: logs/verorun.log).
"""

import logging
import json
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Structured JSON log lines."""
    def format(self, record):
        payload = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'name': record.name,
            'msg': record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            payload['exc'] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_loggers = {}


def get_logger(name=None):
    """Return a unified logger.

    Args:
        name: logger name (usually __name__).  If None, returns the root logger.

    Usage:
        from shared.logging import get_logger
        logger = get_logger(__name__)
        logger.info('something happened')
    """
    if name and name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

    # Avoid duplicate handlers on repeated calls
    if not logger.handlers:
        # Console handler — human readable
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.DEBUG)
        console.setFormatter(logging.Formatter(
            '[%(asctime)s] %(levelname)-5s %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logger.addHandler(console)

        # JSON file handler — structured (only when LOG_FILE is set)
        log_file = os.environ.get('LOG_FILE', '')
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = logging.FileHandler(log_file, encoding='utf-8')
            fh.setLevel(logging.INFO)
            fh.setFormatter(JsonFormatter())
            logger.addHandler(fh)

    if name:
        _loggers[name] = logger
    return logger
