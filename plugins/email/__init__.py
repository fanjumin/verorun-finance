#!/usr/bin/env python3
"""
Email Service Plugin — 邮件服务插件（完全独立）
================================================
统一的邮件服务：SMTP 发信 + IMAP 收信 + 附件 + 已发送记录。
- 独立数据库：PG schema `email`（不依赖主库）
- 独立配置：环境变量 + plugin.json 默认值（不依赖 system_config）
- 独立 i18n：插件自带翻译文件
"""

from i18n import _
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin, clear_plugin_yaml_cache

# 模块级 i18n 引用，由 on_enable 注入
_t = lambda text: text


def init_i18n(t_fn):
    """供插件启用时注入 i18n 翻译函数"""
    global _t
    _t = t_fn


class EmailPlugin(BasePlugin):
    name = 'email'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Email Service — SMTP/IMAP email client with inbox, compose, attachments, and contact management'
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时初始化独立 PG schema: email"""
        from .models import init_email_db
        init_email_db()
        return True

    def on_enable(self, registry):
        """启用时初始化数据库 + i18n（幂等）"""
        from .models import init_email_db
        init_email_db()
        init_i18n(self.t)
        print(_('[EmailPlugin] ✅ Email service plugin enabled (PG schema: email)'))
        return True

    def register_routes(self):
        """注册 Flask 路由"""
        from .routes import email_bp
        return [email_bp]

    # ── 钩子契约兑现（plugin.json hooks.provides 声明的 3 个 action）──

    def activate(self):
        """[ACTIVE 阶段] 注册钩子。

        落点必须是 activate() 而不是 on_enable()：平台铁律（见
        chatbot/__init__.py:45 注释）—— ACTIVE 插件重启只走 setup() + activate()，
        进程内 provider（钩子回调）挂在别的时机会在重启后**静默失效**。
        """
        self._register_hooks()

    def deactivate(self):
        """[DISABLED 阶段] 注销钩子 —— 停用后行为须与未安装本插件一致（反向安全）。"""
        self._unregister_hooks()
        return super().deactivate()

    def _register_hooks(self):
        """兑现 email/send、email/send_contact、email/get_config。

        背景（D17）：这三个钩子此前只在 plugin.json 声明，**全仓无 add_action 注册**，
        功能实际经 MCP Agent Tools 面实现（email_mcp_server.py TOOLS）。
        两条通道并存不冲突 —— MCP 供 Agent 子进程调用，钩子供插件间同步调用；
        这里补上钩子面，使声明与实现一致（声明即承诺）。

        回调签名对齐 services 层：send_email 返 (ok, msg)，send_contact_email 返
        (ok, msg)，get_smtp_config 返配置 dict。add_action 按 (hook, identifier)
        幂等去重，重复调用安全。
        """
        try:
            from plugin_manager.hooks import get_hook_registry
            reg = get_hook_registry()

            def _hook_send(to_addr=None, subject='', body_text='', **kw):
                from .services import send_email
                return send_email(to_addr, subject, body_text, **kw)

            def _hook_send_contact(name='', email_addr='', subject='', message='', **kw):
                from .services import send_contact_email
                return send_contact_email(name, email_addr, subject, message, **kw)

            def _hook_get_config(**_kw):
                # EM-2：钩子面与 MCP 面口径一致，SMTP 密码一律不明文外泄
                from .services import get_smtp_config
                _cfg = dict(get_smtp_config() or {})
                if _cfg.get('smtp_pass'):
                    _cfg['smtp_pass'] = '********'
                return _cfg

            reg.add_action('email/send', _hook_send, priority=10, identifier='email')
            reg.add_action('email/send_contact', _hook_send_contact,
                           priority=10, identifier='email')
            reg.add_action('email/get_config', _hook_get_config,
                           priority=10, identifier='email')
            print(_('[EmailPlugin] ✅ hooks registered: email/send, email/send_contact,'
                    ' email/get_config'))
        except Exception as e:
            # 钩子注册失败不得阻断插件启用 —— 路由面与告警消费面仍可用
            print(_('[EmailPlugin] ⚠️ hook registration failed: {}').format(e))

    def _unregister_hooks(self):
        """注销本插件注册的全部 action（按 identifier 精确移除，不影响他插件同名钩子）。"""
        try:
            from plugin_manager.hooks import get_hook_registry
            reg = get_hook_registry()
            for hook in ('email/send', 'email/send_contact', 'email/get_config'):
                try:
                    reg.remove_action(hook, identifier='email')
                except Exception:
                    pass
        except Exception:
            pass

    # ── 事件消费：把内核告警以邮件投递 ──

    def get_event_handlers(self):
        """订阅内核事件（对标 im_gateway/__init__.py:163 范式）。

        stock_analysis.alert_engine 以 do_action("stock.alert.triggered", ev)
        **位置参数**派发（hooks.do_action 原样透传），故处理器必须接受位置
        事件体。ev 结构见 alert_engine._trigger()：
        {alert_id, symbol, name, type, threshold, observed, message, channels, at}。
        """
        def _on_stock_alert(ev=None, **kwargs):
            self.push_alert(ev if isinstance(ev, dict) else dict(kwargs))

        def _on_morning_brief(ev=None, **kwargs):
            self.push_morning_brief(ev if isinstance(ev, dict) else dict(kwargs))

        return {
            'stock.alert.triggered': _on_stock_alert,
            # O6：晨报由 stock_analysis 以 do_action 派发（hook registry 通道），
            # 与告警同缝；若对方改用 event_bus.emit 则此处收不到（D10 教训）。
            'stock.morning_brief.ready': _on_morning_brief,
        }

    def push_alert(self, ev: dict) -> list:
        """把告警事件投递到邮件渠道，返回逐收件人结果（供日志/排障）。

        仅在告警自身的 channels 含 'email' 时发送 —— 尊重用户在该条告警上选的
        渠道（stock_analysis CHANNEL_CODES = in_app/email/im），不把只勾了
        「站内信」的告警擅自发邮件。收件人未配置或发送失败均只记日志、不抛错，
        避免影响 60s 扫描主链路（stock_analysis 侧对钩子异常已有兜底）。
        """
        if not ev or 'email' not in (ev.get('channels') or []):
            return []
        to = self._alert_recipient()
        if not to:
            print(_('[EmailPlugin] ⚠️ alert mail skipped: recipient not configured'))
            return []
        subject, body = self._format_alert_mail(ev)
        try:
            from .services import send_email
            ok, msg = send_email(to, subject, body)
        except Exception as e:
            print(_('[EmailPlugin] ⚠️ alert mail failed: {}').format(e))
            return [{'to': to, 'result': {'success': False, 'error': str(e)}}]
        if not ok:
            print(_('[EmailPlugin] ⚠️ alert mail rejected: {}').format(msg))
        return [{'to': to, 'result': {'success': bool(ok), 'message': msg}}]

    @staticmethod
    def _alert_recipient() -> str:
        """告警邮件收件人（Q1 方案 a：单地址）。

        优先级：环境变量 ALERT_RECIPIENT > 插件设置 alert_recipient。
        未配置时返回空串，由 push_alert 跳过该渠道。
        """
        to = os.environ.get('ALERT_RECIPIENT', '').strip()
        if to:
            return to
        try:
            from flask import current_app
            mgr = current_app.extensions.get('plugin_manager')
            if mgr:
                cfg = mgr.get_config('email') or {}
                return str(cfg.get('alert_recipient', '') or '').strip()
        except Exception:
            pass
        return ''

    @staticmethod
    def _format_alert_mail(ev: dict):
        """告警邮件 (subject, body)，文案对齐 im_gateway._format_alert_text。"""
        symbol = ev.get('symbol') or ''
        name = ev.get('name') or symbol
        subject = '【VeroRun 告警】{} ({})'.format(name, symbol).strip()
        lines = [subject, '', '标的: {} ({})'.format(name, symbol).strip()]
        if ev.get('message'):
            lines.append('说明: {}'.format(ev['message']))
        if ev.get('observed') is not None:
            line = '当前值: {}'.format(ev['observed'])
            if ev.get('threshold') is not None:
                line += ' / 阈值: {}'.format(ev['threshold'])
            lines.append(line)
        if ev.get('type'):
            lines.append('规则: {}'.format(ev['type']))
        if ev.get('at'):
            lines.append('时间: {}'.format(ev['at']))
        lines.append('')
        lines.append('— 本邮件由 VeroRun 告警引擎自动发送，仅供研究参考，不构成投资建议。')
        return subject, '\n'.join(lines)

    def push_morning_brief(self, ev: dict) -> list:
        """把晨报投递到邮件渠道（O6 闭环）。

        与 push_alert 同语义：收件人未配置或发送失败只记日志、不抛错 ——
        定时任务链路不该因为一封邮件失败而中断。
        """
        if not ev or not ev.get('total'):
            return []
        to = self._alert_recipient()
        if not to:
            print(_('[EmailPlugin] morning brief skipped: recipient not configured'))
            return []
        subject, body = self._format_morning_mail(ev)
        try:
            from .services import send_email
            ok, msg = send_email(to, subject, body)
        except Exception as e:
            print(_('[EmailPlugin] morning brief failed: {}').format(e))
            return [{'to': to, 'result': {'success': False, 'error': str(e)}}]
        if not ok:
            print(_('[EmailPlugin] morning brief rejected: {}').format(msg))
        return [{'to': to, 'result': {'success': bool(ok), 'message': msg}}]

    @staticmethod
    def _format_morning_mail(ev: dict):
        """晨报邮件 (subject, body)。"""
        td = ev.get('trade_date') or ''
        subject = '【VeroRun 晨报】{} 交易日信号汇总'.format(td)
        lines = [subject, '', '交易日: {}'.format(td),
                 '信号总数: {}'.format(ev.get('total', 0))]
        run = ev.get('run') or {}
        if run:
            lines.append('批量执行: 成功 {}/{}，失败 {}'.format(
                run.get('ok', 0), run.get('total', 0), run.get('failed', 0)))
        for sig, items in (ev.get('groups') or {}).items():
            lines.append('')
            lines.append('■ {}（{} 只）'.format(str(sig).upper(), len(items)))
            for it in items:
                conf = it.get('confidence')
                conf_s = ' 置信度 {:.2f}'.format(conf) if conf is not None else ''
                lines.append('  - {} ({}){}'.format(
                    it.get('name') or it.get('symbol'), it.get('symbol'), conf_s))
        lines.append('')
        lines.append('— 本邮件由 VeroRun 晨报任务自动发送，仅供研究参考，不构成投资建议。')
        return subject, '\n'.join(lines)

    def on_disable(self, registry):
        """禁用时清理"""
        print(_('[EmailPlugin] ⚠️ Email service plugin disabled'))
        return True

    def on_uninstall(self, registry=None):
        """卸载时清理独立 PG schema（§4.2/§12.5 零残留）

        注意：PluginManager.uninstall() 以无参方式调用本方法，
        故签名必须使用 registry=None 默认值，避免 TypeError 被静默吞掉。
        """
        try:
            from plugins._base.db import get_raw_connection
            conn = get_raw_connection()
            cur = conn.cursor()
            cur.execute("DROP SCHEMA IF EXISTS email CASCADE")
            conn.commit()
            conn.close()
            clear_plugin_yaml_cache('email')
            print(_('[EmailPlugin] ✅ PG schema email dropped on uninstall'))
        except Exception as e:
            print(_('[EmailPlugin] ⚠️ Uninstall cleanup warning: {}').format(e))
        return True

    def get_dashboard_stats(self):
        """Dashboard 统计：已发送邮件总数、联系人总数（§2.3/§6.3）。"""
        try:
            from .models import get_email_db
            with get_email_db() as db:
                total_sent = db.execute("SELECT COUNT(*) AS count FROM email_sent").fetchone()['count']
                total_contacts = db.execute(
                    "SELECT COUNT(DISTINCT to_addr) AS count FROM email_sent"
                ).fetchone()['count']
            return {'total_sent': total_sent, 'total_contacts': total_contacts}
        except Exception:
            return {'total_sent': 0, 'total_contacts': 0}

    def get_schema_version(self):
        """返回当前 schema 版本（§10.6）。"""
        return '1.2.0'

    def migrate(self, from_version, to_version):
        """版本升级逻辑（§10.6）。当前 schema 无历史迁移需求，直接放行。"""
        return True