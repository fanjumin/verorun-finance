#!/usr/bin/env python3
"""
Captcha Embedded Plugin — Slider Captcha (滑块验证码)
======================================================
核心逻辑（生成器、安全、行为分析、存储）内聚于插件自身 captcha/ 包，
通过插件自有 routes.py 的 Flask Blueprint 暴露 REST API。

端点:
  GET  /api/captcha/generate     → 生成拼图挑战
  POST /api/captcha/verify       → 验证位置 + 行为分析
  POST /api/captcha/consume      → 一次性消费 Token
  GET  /api/captcha/admin/stats/ → 统计数据
"""

from plugin_manager.base import BasePlugin


class CaptchaEmbeddedPlugin(BasePlugin):
    name = 'captcha_embedded'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Slider Captcha — Puzzle generation + behavior analysis + rate limiting'
    author = 'VeroRun'

    def on_enable(self, registry):
        """启用时注册 i18n"""
        from .routes import init_i18n
        init_i18n(self.t)
        print('[CaptchaEmbedded] Plugin i18n initialized')
        return True

    def register_routes(self):
        """注册 Captcha Blueprint（插件自有的 routes.py）"""
        from .routes import captcha_bp
        return [captcha_bp]

    def get_dashboard_stats(self):
        """Dashboard 统计卡片数据（来自 store 统计，异常时返回零值）"""
        try:
            from .captcha.store import get_stats
            s = get_stats()
            return {
                'total_requests': s.get('total_requests', 0),
                'pass_rate': s.get('pass_rate', 0),
                'last_hour': s.get('last_hour', 0),
            }
        except Exception:
            return {'total_requests': 0, 'pass_rate': 0, 'last_hour': 0}

    def on_disable(self, registry):
        """禁用时卸载 Blueprint（stats 端点需重启后移除）"""
        print('[CaptchaEmbedded] Disabled')
        return True

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 captcha_embedded schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS captcha_embedded CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            print('[CaptchaEmbedded] captcha_embedded schema dropped')
        except Exception as e:
            print(f'[CaptchaEmbedded] on_uninstall cleanup failed: {e}')
        return True
