#!/usr/bin/env python3
"""
阿里巴巴开放平台配置管理
配置优先级：
1. ali_api_config 表（插件自有，迁移后优先）
2. system_config 表（旧，迁移兼容）
3. 环境变量（ALIBABA_APP_KEY / ALIBABA_APP_SECRET / ALIBABA_API_GATEWAY）
4. 默认值
"""

import os
import json as _json
from typing import Dict, Any, Optional


def _get_alibaba_config_from_db() -> Dict[str, str]:
    """从插件自有 ali_api_config 表读取阿里巴巴配置
    优先级（回退链）：
      1. ali_api_config 表（插件自有）
      2. system_config 表（旧，迁移兼容）
    """
    required_keys = [
        'alibaba_app_key', 'alibaba_app_secret',
        'alibaba_api_gateway', 'alibaba_redirect_domains',
    ]
    try:
        from .models import get_db
        with get_db() as conn:
            # ① 优先从 ali_api_config 读取（插件独立库）
            placeholders = ','.join('%s' for _ in required_keys)
            rows = conn.execute(
                f"SELECT key, value FROM ali_api_config WHERE key IN ({placeholders})",
                required_keys
            ).fetchall()
            if rows:
                return {r['key']: r['value'].strip() for r in rows}

        # ② 回退：从旧 system_config（主库）只读
        from .models import get_main_db
        with get_main_db() as mconn:
            placeholders = ','.join('%s' for _ in required_keys)
            rows = mconn.execute(
                f"SELECT key, value FROM system_config WHERE key IN ({placeholders})",
                required_keys
            ).fetchall()
            return {r['key']: r['value'].strip() for r in rows} if rows else {}
    except Exception:
        return {}


_db_cfg = _get_alibaba_config_from_db()

# ===== 阿里巴巴开放平台配置 =====
ALIBABA_CONFIG = {
    # API基础配置
    "app_key": _db_cfg.get('alibaba_app_key', '') or os.environ.get("ALIBABA_APP_KEY", ""),
    "app_secret": _db_cfg.get('alibaba_app_secret', '') or os.environ.get("ALIBABA_APP_SECRET", ""),
    "api_gateway": _db_cfg.get('alibaba_api_gateway', '') or os.environ.get("ALIBABA_API_GATEWAY", "https://gw.open.1688.com/openapi"),
    
    # API版本
    "api_version": "param2/1/cn.alibaba.open/",
    "api_version_v2": "param2/1",
    
    # 常用API端点
    "endpoints": {
        "product_get": "alibaba.product.get",  # 商品详情
        "product_search": "alibaba.product.search",  # 商品搜索
        "category_get": "alibaba.category.get",  # 类目查询
        "logistics_get": "alibaba.logistics.get",  # 物流查询
    },
    
    # 签名配置
    "sign_method": "hmac-sha1",
    "timestamp_format": "%Y-%m-%d %H:%M:%S",
    
    # 默认请求参数
    "default_params": {
        "page_size": 20,
        "page_no": 1,
        "order_by": "gmv_desc",  # 按成交额降序
    }
}


def reload_config() -> Dict[str, Any]:
    """从数据库重新加载 alibaba 配置到内存（配置保存后调用，避免重启服务）。

    原地更新 ALIBABA_CONFIG 与 RATE_LIMIT_CONFIG 字典；由于 config['alibaba']
    与 config['rate_limit'] 引用同一字典对象，其他模块已持有的 config 引用
    也能立即看到新值。
    """
    _cfg = _get_alibaba_config_from_db()
    ALIBABA_CONFIG['app_key'] = (_cfg.get('alibaba_app_key', '')
                                 or os.environ.get("ALIBABA_APP_KEY", ""))
    ALIBABA_CONFIG['app_secret'] = (_cfg.get('alibaba_app_secret', '')
                                    or os.environ.get("ALIBABA_APP_SECRET", ""))
    ALIBABA_CONFIG['api_gateway'] = (_cfg.get('alibaba_api_gateway', '')
                                     or os.environ.get("ALIBABA_API_GATEWAY",
                                                       "https://gw.open.1688.com/openapi"))
    # 同步重载风控配置（DB 优先，未配置项保留当前值）
    _rl_db = _get_rate_limit_from_db()
    for _k in RATE_LIMIT_CONFIG:
        if _k in _rl_db:
            RATE_LIMIT_CONFIG[_k] = _coerce_rl_value(_k, _rl_db[_k])
    return validate_config()


def _get_rate_limit_from_db() -> Dict[str, Any]:
    """从 ali_api_config 表读取风控配置（JSON），读取失败返回空 dict。

    配置保存（POST /config）时写入 ali_api_config 表 key='rate_limit'，
    值为此处解析的 JSON 对象；未配置时回退环境变量/默认值。
    """
    try:
        from .models import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM ali_api_config WHERE key = %s",
                ('rate_limit',)
            ).fetchone()
            if row and row.get('value'):
                data = _json.loads(row['value'])
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


_RATE_LIMIT_FIELD_TYPES = {
    'user_daily_limit': int,
    'user_hourly_limit': int,
    'global_concurrent_limit': int,
    'global_qps_limit': int,
    'circuit_breaker_threshold': float,
    'circuit_breaker_window': int,
    'circuit_breaker_timeout': int,
    'max_retries': int,
    'retry_delay': float,
}


def _coerce_rl_value(key: str, value) -> Any:
    """按字段类型安全转换风控配置值，失败返回原值。"""
    t = _RATE_LIMIT_FIELD_TYPES.get(key)
    if t is None:
        return value
    try:
        return t(value)
    except (TypeError, ValueError):
        return value


# ===== 风控配置 =====
# DB 配置优先（ali_api_config.rate_limit），其次环境变量，最后默认值
_rate_limit_db = _get_rate_limit_from_db()

RATE_LIMIT_CONFIG = {
    # 用户级限流
    "user_daily_limit": int(_rate_limit_db.get('user_daily_limit', os.environ.get("ALIBABA_USER_DAILY_LIMIT", "1000"))),
    "user_hourly_limit": int(_rate_limit_db.get('user_hourly_limit', os.environ.get("ALIBABA_USER_HOURLY_LIMIT", "100"))),
    
    # 全局并发控制
    "global_concurrent_limit": int(_rate_limit_db.get('global_concurrent_limit', os.environ.get("ALIBABA_GLOBAL_CONCURRENT_LIMIT", "10"))),
    "global_qps_limit": int(_rate_limit_db.get('global_qps_limit', os.environ.get("ALIBABA_GLOBAL_QPS_LIMIT", "5"))),
    
    # 熔断保护
    "circuit_breaker_threshold": float(_rate_limit_db.get('circuit_breaker_threshold', os.environ.get("ALIBABA_CIRCUIT_BREAKER_THRESHOLD", "0.5"))),
    "circuit_breaker_window": int(_rate_limit_db.get('circuit_breaker_window', os.environ.get("ALIBABA_CIRCUIT_BREAKER_WINDOW", "60"))),
    "circuit_breaker_timeout": int(_rate_limit_db.get('circuit_breaker_timeout', os.environ.get("ALIBABA_CIRCUIT_BREAKER_TIMEOUT", "300"))),
    
    # 重试配置
    "max_retries": int(_rate_limit_db.get('max_retries', os.environ.get("ALIBABA_MAX_RETRIES", "3"))),
    "retry_delay": float(_rate_limit_db.get('retry_delay', os.environ.get("ALIBABA_RETRY_DELAY", "1.0"))),
}

# ===== 缓存配置 =====
CACHE_CONFIG = {
    # Redis配置（如果可用）
    "redis_host": os.environ.get("REDIS_HOST", "localhost"),
    "redis_port": int(os.environ.get("REDIS_PORT", "6379")),
    "redis_db": int(os.environ.get("ALIBABA_REDIS_DB", "0")),
    "redis_password": os.environ.get("REDIS_PASSWORD", ""),
    
    # 内存缓存（备用）
    "memory_cache_ttl": int(os.environ.get("ALIBABA_MEMORY_CACHE_TTL", "300")),  # 5分钟
    "memory_cache_maxsize": int(os.environ.get("ALIBABA_MEMORY_CACHE_MAXSIZE", "1000")),
    
    # 商品缓存时间（秒）
    "product_cache_ttl": int(os.environ.get("ALIBABA_PRODUCT_CACHE_TTL", "3600")),  # 1小时
    "category_cache_ttl": int(os.environ.get("ALIBABA_CATEGORY_CACHE_TTL", "86400")),  # 24小时
}

# ===== AI内容生成配置 =====
AI_CONFIG = {
    # AI 走统一网关（agent_matrix model_policy tier/standard），此处不配置 provider/model
    
    # 标题优化配置
    "title_prompt": "请优化以下商品标题，使其更吸引人、符合SEO要求，同时保留核心关键词：{title}",
    "title_max_length": 60,
    
    # 描述优化配置
    "description_prompt": "请优化以下商品描述，使其更具营销力，突出产品卖点，适合电商平台展示：{description}",
    "description_max_length": 500,
    
    # 生成限制
    "max_tokens": int(os.environ.get("ALIBABA_AI_MAX_TOKENS", "1000")),
    "temperature": float(os.environ.get("ALIBABA_AI_TEMPERATURE", "0.7")),
}

# ===== 数据库配置 =====
DB_CONFIG = {
    # 使用主项目的数据库
    "db_path": os.environ.get("DB_PATH", ""),  # 为空时使用主项目配置
    "table_prefix": "ali_api_",
}

# ===== 日志配置 =====
LOG_CONFIG = {
    "level": os.environ.get("ALIBABA_LOG_LEVEL", "INFO"),
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": os.environ.get("ALIBABA_LOG_FILE", "ali_api.log"),
    "max_size": int(os.environ.get("ALIBABA_LOG_MAX_SIZE", "10485760")),  # 10MB
    "backup_count": int(os.environ.get("ALIBABA_LOG_BACKUP_COUNT", "5")),
}

def validate_config() -> Dict[str, Any]:
    """验证配置并返回有效配置"""
    errors = []
    
    # 检查阿里巴巴API配置
    if not ALIBABA_CONFIG["app_key"]:
        errors.append("ALIBABA_APP_KEY 未配置")
    if not ALIBABA_CONFIG["app_secret"]:
        errors.append("ALIBABA_APP_SECRET 未配置")
    
    # 检查风控配置
    if RATE_LIMIT_CONFIG["user_daily_limit"] <= 0:
        errors.append("用户每日限流必须大于0")
    if RATE_LIMIT_CONFIG["global_concurrent_limit"] <= 0:
        errors.append("全局并发限制必须大于0")
    
    import logging
    if errors:
        logging.warning(f"配置验证警告: {', '.join(errors)}")
    
    return {
        "alibaba": ALIBABA_CONFIG,
        "rate_limit": RATE_LIMIT_CONFIG,
        "cache": CACHE_CONFIG,
        "ai": AI_CONFIG,
        "db": DB_CONFIG,
        "log": LOG_CONFIG,
    }

# 导出配置（非阻塞验证）
config = validate_config()

if __name__ == "__main__":
    # 测试配置
    import pprint
    print("阿里巴巴API配置验证通过:")
    pprint.pprint(config, depth=2)
