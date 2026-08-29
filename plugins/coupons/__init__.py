#!/usr/bin/env python3
"""
Coupon Plugin — 智能优惠券引擎
===============================
场景券 / AI 推荐 / 订阅联动
使用独立 PG schema coupons 存储券表，主库只读查询。
"""

from i18n import _

from plugin_manager.base import BasePlugin

from .engine import CouponEngine
from .ai_recommender import AICouponRecommender
from .routes import coupon_bp, init_routes
from .models import get_db, get_main_db, init_db, set_schema_version

# ── 全局引用，方便外部调用 ──
_engine: CouponEngine = None
_recommender: AICouponRecommender = None


def get_engine() -> CouponEngine:
    """获取 CouponEngine 单例（供核心代码调用）。"""
    return _engine


def get_recommender() -> AICouponRecommender:
    """获取 AICouponRecommender 单例。"""
    return _recommender


class CouponPlugin(BasePlugin):
    name = 'coupons'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = _('智能优惠券引擎 — 场景券/AI推荐/订阅联动')
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时初始化插件数据库 + 记录 schema 版本（§10.6）。"""
        init_db()
        try:
            set_schema_version(self.version)
        except Exception:
            pass
        return True

    def on_enable(self, registry):
        """启用时初始化引擎。"""
        global _engine, _recommender

        _engine = CouponEngine(get_db, get_main_db, t_func=self.t)
        _recommender = AICouponRecommender(_engine)

        # 注入到 routes（传递 t 函数）
        init_routes(get_db, get_main_db, _engine, _recommender, t_func=self.t)

        print(f'[CouponPlugin] CouponEngine initialized')
        return True

    def register_routes(self):
        """注册蓝图为系统路由。"""
        return [coupon_bp]

    def get_dashboard_stats(self) -> dict:
        """Dashboard 统计（§2.3/§6.3）：从插件独立库取数，异常时返回零值。"""
        stats = {'total_coupons': 0, 'active_coupons': 0, 'total_used': 0, 'total_discount': 0}
        try:
            if _engine is None:
                return stats
            s = _engine.stats()
            for k in stats:
                stats[k] = s.get(k, 0)
        except Exception:
            pass
        return stats

    def get_schema_version(self) -> str:
        """从插件独立库读取当前 schema 版本（§10.6）。"""
        try:
            from .models import get_schema_version as _get_schema_version
            return _get_schema_version()
        except Exception:
            return '0.0.0'

    def migrate(self, from_version: str, to_version: str) -> bool:
        """版本升级逻辑（§10.6）：幂等建表并更新 schema 版本。"""
        try:
            init_db()
            set_schema_version(to_version)
            return True
        except Exception:
            return False

    def on_uninstall(self, registry):
        """F-010: 卸载清理 — 删除 coupons schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS coupons CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            print('[CouponPlugin] coupons schema dropped')
        except Exception as e:
            print(f'[CouponPlugin] on_uninstall cleanup failed: {e}')
        return True
