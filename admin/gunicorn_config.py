"""
Gunicorn 配置文件 — post_fork 延迟初始化
============================================
将 i18n seeding 从 app 模块导入时移到 worker fork 后执行。
pg_try_advisory_lock 确保只有一个 worker 执行播种，其他跳过。
"""
import os


def post_fork(server, worker):
    """Worker 启动后执行一次 i18n 播种初始化"""
    try:
        from i18n import seed_from_yaml
        seed_from_yaml('zh-CN')
        seed_from_yaml('en')
    except Exception as e:
        print(f'[gunicorn:post_fork] i18n seed warning: {e}')
