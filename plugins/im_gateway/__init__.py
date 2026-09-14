#!/usr/bin/env python3
"""
IM Gateway Plugin — 即时通讯网关插件
======================================
独立数据库 im_gateway.db

统一管理即时通讯频道（飞书 / 企业微信 / QQ / 钉钉）的凭据配置、
连接测试与消息/媒体推送，通过 adapter 基类抽象，便于扩展 Telegram / LINE。
"""

from i18n import _
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from plugin_manager.base import BasePlugin
from plugin_manager.logger import get_plugin_logger
from .models import init_im_db, migrate_from_main_db

logger = get_plugin_logger('im_gateway')

# 模块级 i18n 引用，由 on_enable 注入
_t = lambda text: text

# PG advisory lock 固定键：插件层按需安装依赖时串行化（独立于 token 刷新锁）
_DEPS_INSTALL_LOCK_KEY = 0x64657073676F  # 'depsgo'


def init_i18n(t_fn):
    """供插件启用时注入 i18n 翻译函数"""
    global _t
    _t = t_fn


class ImGatewayPlugin(BasePlugin):
    name = 'im_gateway'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = ('IM Gateway — Unified channel gateway '
                   '(IM messaging + social publishing + OAuth)')
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时初始化独立数据库 + 从主库迁移已有频道配置"""
        init_im_db()
        try:
            n = migrate_from_main_db()
            if n:
                logger.info(f'Migrated {n} channel configurations from main database')
        except Exception as e:
            logger.warning(f'Channel configuration migration warning: {e}')
        return True

    def on_enable(self, registry):
        """启用时初始化数据库 + i18n + 按需安装弱依赖 + 自调度 token 刷新（幂等）"""
        init_im_db()
        init_i18n(self.t)
        self._ensure_deps()
        self._start_token_refresh_scheduler()
        logger.info('IM gateway plugin enabled')
        return True

    @staticmethod
    def _ensure_deps():
        """插件层按需安装弱依赖（tweepy / praw），默认开启，失败不阻断。

        - 仅当 tweepy / praw 缺失时触发 pip install（sys.executable -m pip）。
        - 用 PG advisory lock 串行化，避免 gunicorn 多 worker 并发安装。
        - 环境变量 IM_GATEWAY_AUTO_INSTALL_DEPS=0/false/no 可关闭。
        - 国内服务器无需 X/Reddit 或 pip 源不可达时，安装失败仅记日志，
          插件照常启用，X/Reddit 渠道按各自懒加载逻辑降级报错。
        """
        try:
            import tweepy  # noqa: F401
            import praw    # noqa: F401
            return
        except ImportError:
            pass
        flag = os.getenv('IM_GATEWAY_AUTO_INSTALL_DEPS', '1').strip().lower()
        if flag in ('0', 'false', 'no'):
            logger.info('[Gateway] deps missing and auto-install disabled; '
                        'X/Reddit channels will degrade')
            return
        lock_conn = None
        try:
            from .models import get_im_db
            lock_conn = get_im_db()
            cur = lock_conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (_DEPS_INSTALL_LOCK_KEY,))
            if not cur.fetchone()[0]:
                logger.info('[Gateway] deps install running in another worker; skip')
                return
        except Exception as e:
            logger.warning('[Gateway] deps install lock acquire failed (%s); continue', e)
        try:
            import subprocess
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--quiet',
                 '--disable-pip-version-check', 'tweepy', 'praw'],
                capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info('[Gateway] installed optional deps tweepy/praw')
            else:
                logger.warning('[Gateway] optional deps install failed rc=%s: %s',
                               result.returncode,
                               (result.stderr or result.stdout)[:300])
        except Exception as e:
            logger.warning('[Gateway] optional deps install error: %s', e)
        finally:
            if lock_conn is not None:
                try:
                    lock_conn.close()  # 归还池前回滚事务，自动释放 advisory lock
                except Exception:
                    pass

    def _start_token_refresh_scheduler(self):
        """插件内自调度：每日 04:00 扫描刷新临近过期 token（进程内兜底）。

        框架已具备 register_jobs() 消费方（manager.register_all_plugin_jobs），
        但框架注册仅在 admin 进程执行；本插件若同时被其他 app 进程加载，
        仍需进程内自建 daemon BackgroundScheduler 兜底。两处可能同时注册，
        由 scheduler.refresh_expiring_tokens() 内的 PG advisory lock 保证
        单实例执行，重复调度仅冗余、无副作用。
        """
        self._token_scheduler = None
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from .scheduler import refresh_expiring_tokens
            sched = BackgroundScheduler(timezone='Asia/Shanghai', daemon=True)
            sched.add_job(refresh_expiring_tokens, 'cron', hour=4, minute=0,
                          id='gateway_token_refresh', replace_existing=True,
                          max_instances=1, coalesce=True, misfire_grace_time=300)
            sched.start()
            self._token_scheduler = sched
            logger.info('[Gateway] scheduled gateway_token_refresh at 04:00')
        except Exception as e:
            logger.warning('[Gateway] token refresh scheduler start failed: %s', e)
            self._token_scheduler = None

    def register_routes(self):
        """注册 Flask 路由（管理端频道配置 API + OAuth 授权 + 入站 Webhook + 聚合概览 + 开发者 Key + 登录提供方 + 小程序账户 + 小程序登录）"""
        from .routes import im_bp
        from .routes_oauth import oauth_bp
        from .routes_webhook import webhook_bp
        from .routes_overview import overview_bp
        from .routes_developer import developer_bp
        from .routes_login import login_bp
        from .routes_miniapp import miniapp_bp
        from .routes_mini_login import mini_login_bp
        from .routes_third_login import third_login_bp
        return [im_bp, oauth_bp, webhook_bp, overview_bp, developer_bp, login_bp, miniapp_bp, mini_login_bp, third_login_bp]

    def register_jobs(self):
        """新增：统一网关定时任务（token 自动刷新）"""
        from .scheduler import GATEWAY_JOBS
        return GATEWAY_JOBS

    def on_disable(self, registry):
        """禁用时清理（停止自调度器）"""
        sched = getattr(self, '_token_scheduler', None)
        if sched is not None:
            try:
                sched.shutdown(wait=False)
            except Exception:
                pass
            self._token_scheduler = None
        logger.warning('IM gateway plugin disabled')
        return True

    def get_dashboard_stats(self) -> dict:
        """Dashboard 聚合统计（读插件独立库 channel_configs，幂等）。"""
        stats = {'total_channels': 0, 'enabled_channels': 0}
        try:
            from .models import get_im_db
            with get_im_db() as conn:
                total = conn.execute('SELECT COUNT(*) AS c FROM channel_configs').fetchone()
                enabled = conn.execute(
                    'SELECT COUNT(*) AS c FROM channel_configs WHERE is_enabled=1'
                ).fetchone()
            stats['total_channels'] = int(total['c']) if total else 0
            stats['enabled_channels'] = int(enabled['c']) if enabled else 0
        except Exception:
            logger.warning('get_dashboard_stats failed', exc_info=True)
        return stats

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 im_gateway schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS im_gateway CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            logger.info('im_gateway schema dropped')
        except Exception as e:
            logger.error('on_uninstall cleanup failed: %s', e, exc_info=True)
        return True

    # ── 对外接口：供主系统（媒体库）调用推送 ──

    def push_media(self, channel, file_url, filename, mime):
        """向指定频道推送媒体文件。

        供主系统 media_library_push 调用。插件禁用时该实例不存在，
        主系统需据此提示_("IM Gateway is not enabled")。

        Args:
            channel: 'feishu' | 'wecom'
            file_url: 文件可访问 URL
            filename: 文件名
            mime: MIME 类型

        Raises:
            Exception: 频道未配置或推送失败
        """
        from .adapters import get_adapter
        adapter = get_adapter(channel)
        if adapter is None:
            raise Exception(_('Channel {channel} does not support media push').format(channel=channel))
        adapter.push_media(file_url, filename, mime)
