#!/usr/bin/env python3
"""
SMS Service Plugin — 短信服务插件（完全独立）
============================================
验证码发送、模板管理、提供商配置。
- 独立数据库：PG schema `sms`（不依赖主库）
- 独立 i18n：插件自带翻译文件
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin


class SmsPlugin(BasePlugin):
    name = 'sms'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'SMS Service — phone verification code sending with Aliyun/Twilio providers'
    author = 'VeroRun'

    def get_config_value(self, key: str, default=None):
        """优先 PluginManager，回退到 plugin.json 默认值"""
        app = getattr(self, 'app', None)
        if app is not None:
            try:
                mgr = getattr(app.extensions, 'get', lambda x: None)('plugin_manager')
                if mgr:
                    pm_cfg = mgr.get_config(self.identifier) or {}
                    if key in pm_cfg:
                        return pm_cfg[key]
            except Exception:
                pass
        return self._config.get(key, default)

    def on_install(self, registry):
        """安装时初始化独立 PG schema sms + 迁移历史数据"""
        from .models import init_sms_db, migrate_from_main_db
        init_sms_db()
        migrate_from_main_db()
        return True

    def on_enable(self, registry):
        """启用时初始化数据库 + i18n（幂等）"""
        from .models import init_sms_db
        init_sms_db()
        print(self.t('[SmsPlugin] ✅ SMS service plugin is enabled (sms schema)'))
        return True

    def register_routes(self):
        """注册 Flask 路由"""
        from .routes import sms_bp
        return [sms_bp]

    def on_disable(self, registry):
        """禁用时清理"""
        print(self.t('[SmsPlugin] ⚠️ SMS service plugin is disabled'))
        return True

    def get_dashboard_stats(self) -> dict:
        """Dashboard 聚合统计（读 sms 独立 schema，幂等）。"""
        stats = {'today_sent': 0, 'total_templates': 0}
        try:
            from .models import get_sms_db
            with get_sms_db() as conn:
                today = conn.execute(
                    'SELECT COUNT(*) AS c FROM sms_logs WHERE created_at::timestamptz>=CURRENT_DATE'
                ).fetchone()
                tpl = conn.execute('SELECT COUNT(*) AS c FROM sms_templates').fetchone()
                stats['today_sent'] = int(today['c']) if today else 0
                stats['total_templates'] = int(tpl['c']) if tpl else 0
        except Exception:
            print('[SmsPlugin] get_dashboard_stats failed')
        return stats

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 sms schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS sms CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            print(self.t('[SmsPlugin] sms schema dropped'))
        except Exception as e:
            print(f'[SmsPlugin] on_uninstall cleanup failed: {e}')
        return True

    # ── 对外接口（供其他模块通过 get_instance('sms') 调用）──

    def send_sms(self, phone, code, purpose='login'):
        """发送验证码（委托给 providers/sms/）"""
        from .services import send_sms as _send
        return _send(phone, code, purpose)

    def generate_code(self, length=6):
        """生成随机验证码"""
        from .services import generate_code as _gen
        return _gen(length)

    def validate_phone(self, phone, country_code=''):
        """验证手机号格式"""
        from .services import validate_phone as _val
        return _val(phone, country_code)

    def get_countries(self):
        """获取支持的国家列表"""
        from .countries import COUNTRIES
        return COUNTRIES

    def check_rate_limit(self, phone, max_per_hour=5):
        """检查手机号是否超出频率限制"""
        from .services import check_rate_limit as _chk
        return _chk(phone, max_per_hour)

    # ── Login / Register method registration (dynamic UI) ──

    def get_login_methods(self):
        """Register SMS login as a dynamic login method for the frontend."""
        return [{
            'type': 'sms',
            'name': 'SMS Login',
            'icon': 'phone',
            'tab_id': 'tabSms',
            'priority': 20,
            'fields': [
                {'name': 'phone', 'type': 'tel', 'placeholder': 'Enter phone number',
                 'autocomplete': 'tel', 'maxlength': 13},
                {'name': 'code', 'type': 'text', 'placeholder': 'Enter 6-digit code',
                 'autocomplete': 'one-time-code', 'inputmode': 'numeric', 'maxlength': 6},
            ],
            'send_code_url': '/auth/sms/send',
            'submit_url': '/auth/sms/login',
            'submit_text': 'Log In / Register',
        }]

    def get_register_methods(self):
        """Register phone-based registration as a dynamic method."""
        return [{
            'type': 'sms',
            'name': 'Phone Registration',
            'icon': 'phone',
            'register_url': '/register',
            'priority': 10,
        }]
