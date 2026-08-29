"""Two-Factor Authentication 插件主类。

纯插件设计：代码全部在插件目录内，不修改系统核心。
- register_routes() 返回 Blueprint 列表（框架要求）。
- 插件由管理员在后台启用后才注册路由/建表；未启用时系统中不存在任何 2FA 逻辑。
- 提供 TOTP 绑定/关闭/校验 API 与 setup-page。
- 卸载用 get_raw_connection()（裸 psycopg2），必须用 cursor 执行。
"""
import os
from plugin_manager.base import BasePlugin

from .routes import bp
from .models import get_two_factor_db, get_raw_connection, run_migrations_with_lock
from .services import TOTPService, get_encryption_key

logger = None  # 由 PluginManager 注入 _log


class TwoFactorAuthPlugin(BasePlugin):
    identifier = "two_factor_auth"

    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'

    def __init__(self):
        super().__init__()
        self.name = "Two-Factor Authentication"
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))

    # ── 路由注册（返回列表）──
    def register_routes(self):
        return [bp]

    # ── 安装：建表迁移 ──
    def on_install(self, registry=None):
        try:
            self._run_migrations()
            self.log("2FA schema initialized")
            return True
        except Exception as e:
            self.log(f"2FA migration failed: {e}", 'error')
            return False

    def _run_migrations(self):
        """顺序执行 migrations/ 下全部 .sql（0001 → 0002…），advisory lock 串行化 + run-once。

        B1/B2 修复：多 gunicorn worker 并发迁移（并发 DDL 互持 AccessExclusiveLock 死锁）
        改为裸连接 + pg_try_advisory_xact_lock 串行化，其余 worker 跳过；
        schema_migrations 记录已应用文件，已应用的不再重跑
        （0002 不再重复转换时间列导致 TIMESTAMPTZ 数据偏移）。
        """
        run_migrations_with_lock(self.plugin_dir, self.log)

    # ── 启用：跑迁移（兼容仅启用未安装的场景）+ 注册登录预检钩子 + 启动清理调度 ──
    def on_enable(self, registry=None):
        try:
            self._run_migrations()
        except Exception as e:
            self.log(f"2FA migration failed: {e}", 'error')

        self._register_login_filter()
        self._start_cleanup_scheduler()
        return True

    # ── 禁用：注销登录预检钩子 + 停止清理调度（禁用后系统行为与未安装完全一致）──
    def on_disable(self, registry=None):
        self._unregister_login_filter()
        self._stop_cleanup_scheduler()
        return True

    # ── 卸载：清理独立 schema（一次性裸连接，用完即关）──
    def on_uninstall(self, registry=None):
        try:
            conn = get_raw_connection()
            try:
                cur = conn.cursor()
                cur.execute("DROP SCHEMA IF EXISTS two_factor_auth CASCADE")
                conn.commit()
            finally:
                conn.close()
            self.log("2FA schema dropped")
        except Exception as e:
            self.log(f"2FA uninstall failed: {e}", 'error')
        return True

    # ── 登录预检钩子（核心扩展点 'auth.before_issue_session'）──
    def _register_login_filter(self):
        """注册登录预检钩子：启用插件后才拦截；未启用时核心 filter 无订阅、零影响。"""
        try:
            from plugin_manager.hooks import get_hook_registry
            from .services import pre_login_check
            get_hook_registry().add_filter(
                'auth.before_issue_session', pre_login_check,
                identifier='two_factor_auth')
            self.log("2FA login precheck filter registered")
        except Exception as e:
            self.log(f"2FA login precheck filter register failed: {e}", 'error')

    def _unregister_login_filter(self):
        """注销登录预检钩子：禁用插件后系统行为与未安装完全一致（反向安全）。"""
        try:
            from plugin_manager.hooks import get_hook_registry
            get_hook_registry().remove_filter(
                'auth.before_issue_session', identifier='two_factor_auth')
        except Exception:
            pass

    # ── 定时清理（复审 follow-up #2：on_enable 启动、on_disable 停止）──
    def _start_cleanup_scheduler(self):
        """启动每小时清理任务（过期 challenge / setup_token）。

        APScheduler 为项目既有依赖（admin/automation、orchestrator 均在用），
        BackgroundScheduler 守护线程不阻塞 gunicorn worker 生命周期。
        """
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._scheduler = BackgroundScheduler(daemon=True)
            self._scheduler.add_job(
                _cleanup_expired_challenges, 'interval', hours=1,
                id='twofa_cleanup', coalesce=True, max_instances=1)
            self._scheduler.start()
            self.log("2FA cleanup scheduler started")
        except Exception as e:
            self._scheduler = None
            self.log(f"2FA cleanup scheduler start failed: {e}", 'error')

    def _stop_cleanup_scheduler(self):
        """停止清理任务，on_disable 时调用。"""
        try:
            if getattr(self, '_scheduler', None):
                self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception:
            pass


# ── 定时清理 ──
def _cleanup_expired_challenges():
    """清理已过期/已消费的 challenge 与过期 setup_token（防两表无限增长）。

    多 worker 下各进程独立调度，DELETE 幂等、短事务，重复执行无副作用。
    """
    try:
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        with get_two_factor_db() as conn:
            conn.execute(
                "DELETE FROM two_factor_challenges "
                "WHERE consumed=true OR expires_at < %s",
                (now - timedelta(hours=1),))
            conn.execute(
                "DELETE FROM setup_tokens WHERE expires_at < %s", (now,))
            conn.commit()
    except Exception:
        pass



