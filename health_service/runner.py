#!/usr/bin/env python3
"""
Health Service — Gunicorn Runner (v2.0)
=========================================
手动启动 gunicorn，规避 platform/ 目录 shadow stdlib platform 模块的问题。

原理：
  1. cd 到项目根目录后，Python 会自动把 CWD 加到 sys.path[0]（空字符串 ''）
  2. 但项目下有 platform/ 目录，会盖住 stdlib platform 模块
  3. 这里先把 '' 从 sys.path 移除，再把项目根目录 append 到最后
  4. 这样 import health_service.app 时能正常找到，但 import platform 时找到标准库
"""
import os
import sys

# 先把当前目录从 sys.path 移除（避免 platform/ 目录 shadow 标准库）
CWD = os.getcwd()
sys.path = [p for p in sys.path if p]  # 去掉空字符串条目

# 确保项目根目录在 sys.path 中（放在末尾，避免 shadow 标准库）
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.append(PROJECT_DIR)

# 现在才 import gunicorn（这时 import platform 会找到标准库）
from gunicorn.app.wsgiapp import run

if __name__ == '__main__':
    sys.argv = [
        'gunicorn',
        '-w', '2',
        '--max-requests', '1000',
        '-b', '0.0.0.0:8085',
        'health_service.app:app',
        '--timeout', '30',
        '--graceful-timeout', '30',
        '--log-level', 'warning',
    ]
    run()
