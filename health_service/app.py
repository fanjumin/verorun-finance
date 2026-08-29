#!/usr/bin/env python3
"""
Health Service — 独立 Flask 入口 (v2.0)
============================================
将 Health Check 从 admin Flask (8084) 剥离为独立服务 (8085)，
确保 admin 挂了 Health Check 仍可运行。

用法:
    # 开发:
    python3 health_service/app.py

    # 生产 (gunicorn):
    gunicorn -w 2 -b 0.0.0.0:8085 health_service.app:app
"""
import os
import sys

# 确保能从项目根目录 import health_check
# 注意: 用 append() 而不是 insert(0, ...)，避免项目中的 platform/ 包 shadow stdlib platform 模块
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_DIR = os.path.join(BASE_DIR, 'auth-center')
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)
if AUTH_DIR not in sys.path:
    sys.path.append(AUTH_DIR)

from flask import Flask

# plugins/health_check 仅在 verorun-code（完整版）中存在；
# verorun-pro 精简版无 plugins 目录，跳过 health check blueprint 注册。
try:
    from plugins.health_check.routes import health_bp
    from plugins.health_check.models import init_health_tables, migrate_alert_schema
    _has_health_plugin = True
except ImportError:
    _has_health_plugin = False
    health_bp = None

app = Flask(__name__)
if _has_health_plugin:
    app.register_blueprint(health_bp)  # url_prefix 已在 BP 定义中: /admin/health


@app.route('/health')
def ping():
    """Liveness probe — health-service 自身"""
    return {'status': 'ok', 'service': 'health-service'}


@app.route('/')
def root():
    """Root redirect to /health"""
    return {'status': 'ok', 'service': 'health-service'}


@app.route('/ready')
def ready():
    """Readiness probe — 检查数据库连接"""
    try:
        from plugins.health_check.models import get_db
        with get_db() as db:
            db.execute('SELECT 1').fetchone()
        return {'status': 'ready', 'service': 'health-service'}
    except Exception as e:
        return {'status': 'not_ready', 'error': str(e)}, 503


@app.route('/api/guardian/status')
def guardian_status():
    """返回本地 verorun-guardian 守护进程的运行状态"""
    import json as _json
    try:
        with open('/var/run/verorun-guardian/status.json', 'r') as f:
            data = _json.load(f)
        return {'status': 'ok', 'data': data}
    except FileNotFoundError:
        return {'status': 'not_running', 'error': 'guardian 未运行或状态文件不存在'}, 503


if __name__ == '__main__':
    if _has_health_plugin:
        init_health_tables()
        migrate_alert_schema()
    app.run(host='0.0.0.0', port=8085, debug=False)
