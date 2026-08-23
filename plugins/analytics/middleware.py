#!/usr/bin/env python3
"""
analytics/middleware.py — Flask 请求捕获中间件

职责（按执行顺序）:
  1. 请求开始时: 记录开始时间 + 扩展请求属性
  2. 请求结束时: 采集所有数据 → 匿名化 IP → 生成 Visitor Hash / Session Hash
     → 写入 analytics_logs + 更新会话
  3. IP 匿名化在接收请求的第一时间完成
  4. 自动区分真实用户 / AI 爬虫 / 内部测试

集成方式:
  from analytics.middleware import AnalyticsMiddleware
  AnalyticsMiddleware(app, service_name='trademind')

或者手动注册 before_request / after_request:
  from analytics.middleware import capture_before, capture_after
  app.before_request(capture_before)
  app.after_request(capture_after)
"""

import time
import re
import os
import sys
import logging
import psycopg2
import fnmatch
from datetime import datetime

logger = logging.getLogger('analytics.middleware')

# ─── 本地导入 ──────────────────────────────────────────────────────────────────
from . import models as am
from .geoip import geoip_lookup, init_geoip
from .ua_parser import parse_ua

# ─── 全局状态 ──────────────────────────────────────────────────────────────────

# 排除规则（从隐私配置读取，启动时也设默认值）
EXCLUDE_PATHS = [
    '/static/*', '/favicon.ico', '/robots.txt', '/health',
    '/admin/analytics/*',  # 排除仪表盘自引用请求
]
EXCLUDE_PATH_REGEX = None

INTERNAL_IP_RANGES = [
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
]

# 可信代理 CIDR 列表：仅当直连方属于这些网段时，才采信 X-Forwarded-For
# 部署在 Nginx 后且可自行扩展（如加入内网负载均衡网段）
TRUSTED_PROXIES = [
    '127.0.0.1', '::1',
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
]


def _is_trusted_proxy(ip: str) -> bool:
    """判断直连 IP 是否为可信代理（仅可信来源才采信 X-Forwarded-For）"""
    if not ip:
        return False
    try:
        import ipaddress
        ip_obj = ipaddress.ip_address(ip)
        for r in TRUSTED_PROXIES:
            if ip_obj in ipaddress.ip_network(r, strict=False):
                return True
    except Exception:
        logger.warning('Invalid proxy IP detected: %r', ip, exc_info=True)
    return False


def _get_client_ip():
    """获取真实客户端 IP：仅当直连方为可信代理时采信 X-Forwarded-For

    攻击者直接构造 X-Forwarded-For 无法伪造（直连方不可信时直接取 remote_addr）。
    """
    from flask import request
    remote = request.remote_addr or ''
    if _is_trusted_proxy(remote):
        forwarded = request.headers.get('X-Forwarded-For', '')
        if forwarded:
            # 取最左侧（最原始）客户端 IP
            return forwarded.split(',')[0].strip()
    return remote

ANALYTICS_ENABLED = True
SERVICE_NAME = 'unknown'

# 性能采样率：高流量下可降采样（1.0 = 全部记录）
SAMPLE_RATE = 1.0

# 数据库写入重试配置
_DB_RETRIES = 3
_DB_RETRY_DELAY = 0.1  # 100ms


def _db_write(func):
    """装饰器：遇到 database is locked 时自动重试"""
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(_DB_RETRIES):
            try:
                return func(*args, **kwargs)
            except (psycopg2.OperationalError, psycopg2.errors.SerializationFailure) as e:
                if attempt < _DB_RETRIES - 1:
                    time.sleep(_DB_RETRY_DELAY * (attempt + 1))
                    continue
                raise
    return wrapper


def _init_exclude_patterns():
    """编译排除路径的正则模式"""
    global EXCLUDE_PATH_REGEX
    patterns = []
    for p in EXCLUDE_PATHS:
        if '*' in p:
            regex = fnmatch.translate(p)
        else:
            regex = f'^{re.escape(p)}$'
        patterns.append(regex)
    EXCLUDE_PATH_REGEX = re.compile('|'.join(patterns)) if patterns else None


def _should_exclude(path: str) -> bool:
    """检查路径是否应被排除"""
    if not EXCLUDE_PATH_REGEX:
        return False
    return bool(EXCLUDE_PATH_REGEX.search(path))


def _should_exclude_ip(ip: str) -> bool:
    """检查 IP 是否在排除范围（内部IP）"""
    try:
        import ipaddress
        ip_obj = ipaddress.ip_address(ip)
        for r in INTERNAL_IP_RANGES:
            try:
                net = ipaddress.ip_network(r, strict=False)
                if ip_obj in net:
                    return True
            except:
                continue
    except:
        pass
    return False


# ─── 中间件类 ──────────────────────────────────────────────────────────────────

class AnalyticsMiddleware:
    """
    Flask 分析中间件

    用法:
        from analytics.middleware import AnalyticsMiddleware
        app = Flask(__name__)
        AnalyticsMiddleware(app, service_name='platform', geoip_enabled=True)
    """

    def __init__(self, app, service_name: str = 'unknown',
                 geoip_enabled: bool = True, sample_rate: float = 1.0):
        global SERVICE_NAME, SAMPLE_RATE
        SERVICE_NAME = service_name
        SAMPLE_RATE = sample_rate

        # 初始化数据库表
        am.init_analytics_tables()

        # 初始化 GeoIP
        if geoip_enabled:
            init_geoip()

        # 编译排除规则
        self._load_privacy_config()
        _init_exclude_patterns()

        # 注册钩子
        app.before_request(self.before_request)
        app.after_request(self.after_request)

        logger.info('Middleware registered [%s] sampling rate=%s', service_name, sample_rate)

    def _load_privacy_config(self):
        """从数据库加载隐私配置"""
        try:
            conn = am.get_db()
            config = am.get_privacy_config(conn)
            if config.get('exclude_paths'):
                global EXCLUDE_PATHS
                try:
                    import json
                    paths = json.loads(config['exclude_paths'])
                    if isinstance(paths, list):
                        EXCLUDE_PATHS = paths
                except Exception:
                    logger.warning('Invalid exclude_paths in privacy config', exc_info=True)
            if config.get('internal_ip_ranges'):
                try:
                    import json
                    ranges = json.loads(config['internal_ip_ranges'])
                    if isinstance(ranges, list):
                        global INTERNAL_IP_RANGES
                        INTERNAL_IP_RANGES = ranges
                except Exception:
                    logger.warning('Invalid internal_ip_ranges in privacy config', exc_info=True)
            conn.close()
        except Exception:
            logger.warning('Failed to load analytics privacy config', exc_info=True)

    def before_request(self):
        """请求开始：记录开始时间"""
        from flask import request, g
        g._analytics_start = time.time()
        g._analytics_skip = False

        # 检查排除条件
        path = request.path
        ip = _get_client_ip()

        should_skip = (
            not ANALYTICS_ENABLED
            or _should_exclude(path)
            or _should_exclude_ip(ip)
        )

        if should_skip:
            g._analytics_skip = True

    def after_request(self, response):
        """请求结束：采集数据 → 匿名化 → 哈希 → 写入"""
        from flask import request, g

        # 跳过标记
        if getattr(g, '_analytics_skip', False):
            return response

        # 采样
        if SAMPLE_RATE < 1.0:
            import random
            if random.random() > SAMPLE_RATE:
                return response

        try:
            start_time = getattr(g, '_analytics_start', None)
            if start_time is None:
                return response

            raw_ip = _get_client_ip()
            data = _collect_log_data(request, response, raw_ip, start_time)

            # 写入原始日志（带重试）
            @_db_write
            def _do_write():
                conn = am.get_db()
                try:
                    am.insert_log(conn, data)

                    # 管理会话（非爬虫）
                    if not data['is_bot']:
                        existing_session = conn.execute(
                            "SELECT id, page_views FROM analytics_visitor_sessions WHERE session_hash=?",
                            (data['session_hash'],)
                        ).fetchone()

                        if existing_session:
                            am.update_session(
                                conn, data['session_hash'],
                                exit_path=data['path'],
                                page_views=existing_session['page_views'] + 1,
                                duration=int(time.time()) - start_time
                            )
                        else:
                            existing_visitor = conn.execute(
                                "SELECT id FROM analytics_visitor_sessions WHERE visitor_hash=? AND date=?",
                                (data['visitor_hash'], data['_date'])
                            ).fetchone()
                            is_new = 1 if existing_visitor is None else 0

                            am.track_session(
                                conn, data['session_hash'], data['visitor_hash'], data['_date'],
                                start_time=data['timestamp'],
                                entry_path=data['path'],
                                referer=data['referer_domain'] or '',
                                browser=data['browser'],
                                os_name=data['os_name'],
                                device_type=data['device_type'],
                                country=data['country'],
                                city=data['city'],
                                is_bot=0,
                                is_new=is_new,
                            )
                finally:
                    conn.close()

            _do_write()
        except Exception as e:
            # 中间件绝不能影响主请求
            logger.warning('Data collection error: %s', e, exc_info=True)

        return response


# ─── 共享采集逻辑 ─────────────────────────────────────────────────────────────

def _collect_log_data(request, response, raw_ip, start_time):
    """共享采集逻辑：AnalyticsMiddleware.after_request 与 capture_after 共用，
    保证字段完全一致（含 utm、content_type、full_url）。
    返回可直接传给 am.insert_log 的 dict（另含 '_date' 供会话管理使用）。
    """
    response_time = int((time.time() - start_time) * 1000)
    now = int(time.time())
    today_str = datetime.now().strftime('%Y-%m-%d')

    user_agent = request.headers.get('User-Agent', '')
    path = request.path
    query_string = am.anonymize_query_string(
        request.query_string.decode('utf-8') if hasattr(request.query_string, 'decode') else (request.query_string or '')
    )
    referer = request.headers.get('Referer', '') or ''
    language = request.headers.get('Accept-Language', '')[:64]
    method = request.method
    status = response.status_code
    content_type = response.headers.get('Content-Type', 'text/html')

    # 构建完整 URL
    host = request.headers.get('Host', '')
    scheme = request.headers.get('X-Forwarded-Proto', 'https')
    full_url = f'{scheme}://{host}{path}'
    if query_string:
        full_url += f'?{query_string}'

    # 立即匿名化 IP（第一步！）
    ip_prefix = am.hash_ip(raw_ip)

    # 检测爬虫
    is_bot = am.is_bot(user_agent)

    # 解析 UA（非爬虫才解析以节省资源）
    browser = ''
    browser_version = ''
    os_name = ''
    device_type = 'desktop'
    if not is_bot and user_agent:
        ua_data = parse_ua(user_agent)
        browser = ua_data.get('browser', '')
        browser_version = ua_data.get('browser_version', '')
        os_name = ua_data.get('os_name', '')
        device_type = ua_data.get('device_type', 'desktop')
        # 如果 UA 解析器也返回 is_bot，采纳
        if ua_data.get('is_bot'):
            is_bot = True

    # 地理定位（仅对真实用户）
    country = ''
    city = ''
    if not is_bot and raw_ip and raw_ip != '127.0.0.1':
        geo = geoip_lookup(raw_ip)
        country = geo.get('country', '')
        city = geo.get('city', '')

    # 访客 / 会话哈希
    visitor_hash = am.make_visitor_hash(ip_prefix, user_agent, today_str)
    session_hash = am.make_session_hash(visitor_hash, now)

    # 来源分类
    source_type, source_name = am.classify_source(referer)
    ref_domain = am.normalize_referer(referer)

    # UTM 参数提取
    utm_source = ''
    utm_medium = ''
    utm_campaign = ''
    if query_string:
        from urllib.parse import parse_qs as pqs
        qs_parsed = pqs(query_string)
        utm_source = qs_parsed.get('utm_source', [''])[0]
        utm_medium = qs_parsed.get('utm_medium', [''])[0]
        utm_campaign = qs_parsed.get('utm_campaign', [''])[0]

    return {
        'timestamp': now,
        '_date': today_str,
        'visitor_hash': visitor_hash,
        'session_hash': session_hash,
        'ip_prefix': ip_prefix,
        'country': country,
        'city': city,
        'user_agent': user_agent[:512],
        'browser': browser,
        'browser_version': browser_version,
        'os_name': os_name,
        'device_type': device_type,
        'is_bot': is_bot,
        'path': path,
        'query_string': query_string[:512],
        'referer': referer[:1024],
        'referer_domain': ref_domain,
        'utm_source': utm_source,
        'utm_medium': utm_medium,
        'utm_campaign': utm_campaign,
        'language': language,
        'status_code': status,
        'response_time': response_time,
        'request_method': method,
        'service_name': SERVICE_NAME,
        'full_url': full_url[:2048],
        'content_type': content_type,
    }


# ─── 快捷函数 ──────────────────────────────────────────────────────────────────

def capture_before():
    """方便手动注册的 before_request 函数"""
    from flask import request, g
    g._analytics_start = time.time()
    g._analytics_skip = False

    path = request.path
    ip = _get_client_ip()
    if not ANALYTICS_ENABLED or _should_exclude(path) or _should_exclude_ip(ip):
        g._analytics_skip = True


def capture_after(response):
    """方便手动注册的 after_request 函数（与 AnalyticsMiddleware.after_request 共用采集逻辑）"""
    from flask import request, g

    if getattr(g, '_analytics_skip', False):
        return response
    if SAMPLE_RATE < 1.0:
        import random
        if random.random() > SAMPLE_RATE:
            return response

    try:
        start_time = getattr(g, '_analytics_start', None)
        if start_time is None:
            return response

        raw_ip = _get_client_ip()
        data = _collect_log_data(request, response, raw_ip, start_time)

        @_db_write
        def _do_write_capture():
            conn = am.get_db()
            try:
                am.insert_log(conn, data)
            finally:
                conn.close()

        _do_write_capture()
    except Exception as e:
        logger.warning('capture_after error: %s', e, exc_info=True)

    return response
