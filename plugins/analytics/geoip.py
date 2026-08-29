#!/usr/bin/env python3
"""
analytics/geoip.py — IP 地理解析（ip2region / MaxMind GeoLite2 / ip-api 回退）

设计:
  - 优先使用 ip2region（中国 IP 城市覆盖最佳）
  - 其次 MaxMind GeoLite2 数据库（国际 IP）
  - 如果都不存在，回退到 ip-api.com 在线查询（带缓存）
  - 绝不存储精确 IP
  - 国家 + 城市级别（不存储经纬度）

依赖:
  pip install geoip2         # MaxMind 客户端
  # ip2region 已 vendor 到 analytics/ip2region/
  # 或使用内置的 ip-api 回退（无需额外依赖）

安装:
  1. ip2region: 下载 ip2region_v4.xdb → analytics/data/
  2. GeoLite2: wget https://git.io/GeoLite2-City.mmdb -O /path/to/GeoLite2-City.mmdb
"""

from i18n import _
import os
import sys
import time
import json
import base64
import logging
import threading
from urllib.request import urlopen, Request, HTTPError
from urllib.parse import urlencode

logger = logging.getLogger('analytics.geoip')

# ─── 配置 ──────────────────────────────────────────────────────────────────────

# ip2region xdb 数据库路径
IP2REGION_DB = os.path.join(os.path.dirname(__file__), 'data', 'ip2region_v4.xdb')

# GeoLite2 数据库路径（自动探测）
_DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
GEOIP_DB_CANDIDATES = [
    os.path.join(_DATA_DIR, 'GeoLite2-City.mmdb'),
    '/usr/share/GeoIP/GeoLite2-City.mmdb',
]

# ip-api 缓存（避免限流）
IPAPI_CACHE = {}
IPAPI_CACHE_TTL = 3600  # 1 小时缓存
IPAPI_CACHE_MAX = 10000  # 最大缓存条目数（防内存无限增长）

# ip-api 失败全局冷却（免费层限流 ~45 req/min，失败后冷却避免高频重试刷屏日志）
_IPAPI_FAIL_COOLDOWN = 60  # 秒
_last_ipapi_fail_ts = 0.0

_geoip_reader = None
_ip2region_searcher = None

# 全局 GeoIP 状态锁：下载/重置与查询并发时防止读到半初始化状态
_geoip_lock = threading.Lock()

# 自动检测：启动时查服务器公网 IP，判断是否境外
# 不再依赖 DEPLOY_MARKET 环境变量
_IS_INTL = None  # None = 尚未检测，False = 境内，True = 境外


def _verify_mmdb(path: str) -> bool:
    """校验 .mmdb 文件头（MaxMind magic bytes: 0xAB 0xCD 0xEF "MaxMind.com"）"""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
        if not head.startswith(b'\xab\xcd\xef'):
            return False
        return b'MaxMind.com' in head
    except Exception:
        return False


def _verify_xdb(path: str) -> bool:
    """校验 ip2region .xdb 文件头（前 8 字节 indexStartPtr / 8-16 字节 indexEndPtr）"""
    try:
        size = os.path.getsize(path)
        # 最小结构: 256 字节头 + 262144 字节向量索引，实际 xdb 约 11MB
        if size < 256 + 262144:
            return False
        with open(path, 'rb') as f:
            start = int.from_bytes(f.read(8), 'big')
            end = int.from_bytes(f.read(8), 'big')
        if not (256 <= start < end < size):
            return False
        return True
    except Exception:
        return False


def _detect_server_location() -> bool:
    """
    判断服务器是否在境外。
    优先级: DEPLOY_MARKET 环境变量 > ip-api.com 自动检测（仅启动时一次）
    首次调用后缓存结果。
    """
    global _IS_INTL
    with _geoip_lock:
        cached = _IS_INTL
    if cached is not None:
        return cached

    # 1. 优先读取环境变量（部署时常量，零延迟，可靠）
    market_env = os.environ.get('DEPLOY_MARKET', '').lower()
    if market_env in ('intl', 'international', 'global'):
        with _geoip_lock:
            _IS_INTL = True
        logger.info('Market set via DEPLOY_MARKET=%s → intl', market_env)
        return _IS_INTL
    if market_env in ('cn', 'china'):
        with _geoip_lock:
            _IS_INTL = False
        logger.info('Market set via DEPLOY_MARKET=%s → cn', market_env)
        return _IS_INTL

    # 2. 回退到 ip-api 自动检测（仅启动时一次）
    try:
        import json
        from urllib.request import urlopen
        resp = urlopen('https://ip-api.com/json/?fields=countryCode', timeout=5)
        data = json.loads(resp.read().decode())
        cc = (data.get('countryCode') or '').upper()
        detected = (cc != 'CN')
        with _geoip_lock:
            _IS_INTL = detected
        logger.info('Market auto-detected via ip-api: %s', 'intl' if detected else 'cn')
    except Exception:
        with _geoip_lock:
            _IS_INTL = False  # 默认国内
        logger.warning('Market auto-detection via ip-api failed, defaulting to cn', exc_info=True)
    return _IS_INTL


def is_server_international() -> bool:
    """对外公开：判断服务器是否在境外"""
    return _detect_server_location()


def get_market() -> str:
    """对外公开：返回 'intl' 或 'cn'"""
    return 'intl' if _detect_server_location() else 'cn'


def detect_client_market(ip: str) -> str:
    """
    根据客户端 IP 判断市场：'cn' 或 'intl'
    用于前端根据访客 IP 动态切换中国/国际视图。
    """
    if not ip or ip.startswith('127.') or ip.startswith('192.168.') or ip.startswith('10.'):
        return get_market()  # 内网 IP 回退到服务器市场

    # 优先用 ip2region（本地，零延迟）
    result = _ip2region_lookup(ip)
    if result.get('country'):
        return 'cn' if result['country'] == 'CN' else 'intl'

    # 回退到 ip-api
    try:
        api_result = _ipapi_lookup(ip)
        if api_result.get('country'):
            return 'cn' if api_result['country'] == 'CN' else 'intl'
    except Exception:
        logger.warning('ip-api fallback lookup failed for client market', exc_info=True)

    return get_market()  # 最终回退到服务器市场


# ─── ip2region ───────────────────────────────────────────────────────────────────

def _init_ip2region() -> bool:
    """初始化 ip2region（优先于 MaxMind）"""
    global _ip2region_searcher
    if not os.path.exists(IP2REGION_DB):
        logger.info('ip2region database not found: %s', IP2REGION_DB)
        return False
    if not os.access(IP2REGION_DB, os.R_OK):
        logger.warning('ip2region database not readable (bad permissions): %s', IP2REGION_DB)
        return False
    try:
        sys.path.append(os.path.dirname(__file__))
        from ip2region import util
        from ip2region.searcher import new_with_buffer
        with open(IP2REGION_DB, 'rb') as f:
            searcher = new_with_buffer(util.IPv4, f.read())
        with _geoip_lock:
            _ip2region_searcher = searcher
        logger.info('ip2region loaded: %s', IP2REGION_DB)
        return True
    except Exception as e:
        logger.warning('ip2region loading failed: %s', e, exc_info=True)
        return False


def _ip2region_lookup(ip: str) -> dict:
    """
    ip2region 查询，解析 _("Country|Region|Province|City|ISP") 格式
    返回: {'country': _('China'), 'city': _('Nanjing')}
    """
    with _geoip_lock:
        searcher = _ip2region_searcher
    if not searcher:
        return {}
    try:
        result = searcher.search(ip)
        if not result:
            return {}
        # 实测格式: "国家|省份|城市|ISP|国家码"，如 '中国|江苏省|南京市|0|CN'
        parts = result.split('|')
        country = parts[0] if len(parts) > 0 else ''
        city = parts[2] if len(parts) > 2 and parts[2] != '0' else ''
        # 国家码优先取 xdb 第 5 字段（已是 ISO 3166-1 alpha-2，如 'CN'/'US'/'AU'）；
        # 无效时回退中文/英文国家名查表映射（国际 IP 为英文名，_COUNTRY_MAP 可命中）
        cc = parts[4] if len(parts) > 4 else ''
        if not (cc and len(cc) == 2 and cc.isalpha() and cc.isupper()):
            cc = _country_name_to_code(country)
        # 只有合法的 ISO 3166-1 alpha-2 国家码才视为有效
        if cc and len(cc) == 2 and cc.isalpha() and cc.isupper():
            return {'country': cc, 'city': city}
        return {}
    except Exception:
        logger.warning('ip2region lookup failed for %r', ip, exc_info=True)
        return {}


# 常用国家名 → ISO 代码映射（中文 + 英文）
_COUNTRY_MAP = {
    _('China'): 'CN', 'China': 'CN',
    _('United States'): 'US', 'United States': 'US',
    _('Japan'): 'JP', 'Japan': 'JP',
    _('South Korea'): 'KR', 'South Korea': 'KR', 'Korea': 'KR',
    _('United Kingdom'): 'GB', 'United Kingdom': 'GB',
    _('Germany'): 'DE', 'Germany': 'DE',
    _('France'): 'FR', 'France': 'FR',
    _('Russia'): 'RU', 'Russia': 'RU',
    _('India'): 'IN', 'India': 'IN',
    _('Brazil'): 'BR', 'Brazil': 'BR',
    _('Canada'): 'CA', 'Canada': 'CA',
    _('Australia'): 'AU', 'Australia': 'AU',
    _('Singapore'): 'SG', 'Singapore': 'SG',
    _('Malaysia'): 'MY', 'Malaysia': 'MY',
    _('Thailand'): 'TH', 'Thailand': 'TH',
    _('Vietnam'): 'VN', 'Vietnam': 'VN',
    _('Indonesia'): 'ID', 'Indonesia': 'ID',
    _('Philippines'): 'PH', 'Philippines': 'PH',
    _('Netherlands'): 'NL', 'Netherlands': 'NL',
    _('Italy'): 'IT', 'Italy': 'IT',
    _('Spain'): 'ES', 'Spain': 'ES',
    _('Sweden'): 'SE', 'Sweden': 'SE',
    _('Switzerland'): 'CH', 'Switzerland': 'CH',
    _('Hong Kong'): 'HK', 'Hong Kong': 'HK',
    _('Taiwan'): 'TW', 'Taiwan': 'TW',
    _('Macau'): 'MO', 'Macau': 'MO',
    _('United Arab Emirates'): 'AE', 'United Arab Emirates': 'AE',
    _('Saudi Arabia'): 'SA', 'Saudi Arabia': 'SA',
}

def _country_name_to_code(name: str) -> str:
    """中文国家名 → ISO 代码"""
    return _COUNTRY_MAP.get(name, name) if name else ''

def _find_db() -> str:
    """查找本地 GeoLite2 数据库"""
    for p in GEOIP_DB_CANDIDATES:
        if os.path.exists(p):
            return p
    return ''


def init_geoip():
    """初始化 GeoIP（ip2region + MaxMind）"""
    global _geoip_reader, _ip2region_searcher

    # 初始化 ip2region（优先级最高）
    _init_ip2region()
    with _geoip_lock:
        has_ip2r = _ip2region_searcher is not None

    # 初始化 MaxMind
    db_path = _find_db()
    if not db_path:
        if not has_ip2r:
            logger.info('GeoIP databases not found, using ip-api as fallback')
        else:
            logger.info('GeoLite2 database not found, ip2region + ip-api available')
        return has_ip2r
    try:
        import geoip2.database
        reader = geoip2.database.Reader(db_path)
        with _geoip_lock:
            _geoip_reader = reader
        logger.info('GeoIP loaded: %s', db_path)
        return True
    except Exception as e:
        logger.warning('GeoIP loading failed: %s', e, exc_info=True)
        return has_ip2r


def geoip_lookup(ip: str) -> dict:
    """
    IP 地理查询（ip2region → MaxMind → ip-api）
    返回: {'country': 'CN', 'city': _('Nanjing')}
    失败返回空 dict
    """
    with _geoip_lock:
        reader = _geoip_reader
    if not ip or ip == '127.0.0.1' or ip == '0.0.0.x' or ip.startswith('192.168.'):
        return {'country': '', 'city': ''}

    # 1. 始终优先 ip2region（中国 IP 城市级精准，国际 IP 给国家码）
    result = _ip2region_lookup(ip)
    if result.get('city'):
        return result  # 有城市数据，直接返回
    if result.get('country') and not reader:
        return result  # ip2region 有国家码 + 没有 MaxMind → 直接返回

    # 2. MaxMind 补充（国际 IP 城市数据）
    if reader:
        try:
            response = reader.city(ip)
            mm_city = response.city.name or ''
            mm_country = response.country.iso_code or ''
            if mm_country:
                # 若 ip2region 已给 country，用 MaxMind 补 city
                if result.get('country'):
                    if mm_country == result.get('country'):
                        return {'country': result.get('country'), 'city': mm_city or result.get('city', '')}
                    return {'country': mm_country, 'city': mm_city}
                return {'country': mm_country, 'city': mm_city}
        except Exception:
            logger.warning('MaxMind lookup failed for %r', ip, exc_info=True)

    # 3. ip2region 至少给了 country
    if result.get('country'):
        return result

    # 4. 回退到 ip-api
    return _ipapi_lookup(ip)


def _ipapi_lookup(ip: str) -> dict:
    """通过 ip-api.com 在线查询（带缓存）"""
    global _last_ipapi_fail_ts
    now = time.time()

    # 失败冷却：ip-api 最近失败过，静默降级，避免每次请求都打 warning 日志
    if now - _last_ipapi_fail_ts < _IPAPI_FAIL_COOLDOWN:
        return {'country': '', 'city': ''}

    # 清理过期缓存
    global IPAPI_CACHE
    expired = [k for k, v in IPAPI_CACHE.items() if now - v['ts'] > IPAPI_CACHE_TTL]
    for k in expired:
        del IPAPI_CACHE[k]
    # 大小上限保护：超过上限时清理最早插入的键
    if len(IPAPI_CACHE) > IPAPI_CACHE_MAX:
        overflow = len(IPAPI_CACHE) - IPAPI_CACHE_MAX
        for k in list(IPAPI_CACHE.keys())[:overflow]:
            del IPAPI_CACHE[k]

    # 缓存命中
    if ip in IPAPI_CACHE:
        return IPAPI_CACHE[ip]['data']

    try:
        url = f'https://ip-api.com/json/{ip}?fields=countryCode,city'
        resp = urlopen(url, timeout=3)
        data = json.loads(resp.read().decode())
        if data.get('status') == 'success':
            result = {
                'country': data.get('countryCode', ''),
                'city': data.get('city', ''),
            }
            IPAPI_CACHE[ip] = {'ts': now, 'data': result}
            return result
    except Exception as e:
        logger.warning('ip-api lookup failed for %r: %s', ip, e)
        # 记录失败时间戳，进入全局冷却窗口（避免日志刷屏）
        _last_ipapi_fail_ts = time.time()

    return {'country': '', 'city': ''}


# ─── 维护工具 ──────────────────────────────────────────────────────────────────

def download_geolite2_auto(license_key: str, account_id: str = '', edition: str = 'GeoLite2-City') -> dict:
    """
    自动从 MaxMind 下载数据库到 analytics/data/ 目录。
    2024+ 新版 API 要求 Basic Auth（account_id:license_key）。

    参数:
      license_key — MaxMind License Key
      account_id  — MaxMind Account ID（新版必须）
      edition     — 'GeoLite2-City'（免费，默认）或 'GeoIP2-City'（付费）

    返回: {'success': True, 'path': '...', 'size_mb': 12.3}
         或 {'success': False, 'error': '...'}
    """
    import tarfile
    import tempfile
    import shutil

    target_dir = os.path.join(os.path.dirname(__file__), 'data')
    os.makedirs(target_dir, exist_ok=True)
    mmdb_name = edition + '.mmdb'
    target_path = os.path.join(target_dir, mmdb_name)

    # 新版 URL（2024+）
    url = (
        'https://download.maxmind.com/geoip/databases'
        f'/{edition}/download?suffix=tar.gz'
    )

    try:
        req = Request(url)

        # Basic Auth: account_id:license_key
        if account_id:
            credentials = base64.b64encode(f'{account_id}:{license_key}'.encode()).decode()
            req.add_header('Authorization', f'Basic {credentials}')
        elif license_key:
            # 兼容旧版：仅有 license_key 时用 query param（部分版本仍支持）
            url_with_key = url + f'&license_key={license_key}'
            req = Request(url_with_key)

        resp = urlopen(req, timeout=120)

        # 写入临时文件
        with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
            tmp.write(resp.read())
            tmp_path = tmp.name

        try:
            with tarfile.open(tmp_path, 'r:gz') as tar:
                mmdb_member = None
                for member in tar.getmembers():
                    if member.name.endswith('.mmdb'):
                        mmdb_member = member
                        break
                if not mmdb_member:
                    return {
                        'success': False,
                        'error': f'{edition}.mmdb not found in downloaded archive',
                    }

                extracted = tar.extractfile(mmdb_member)
                if extracted:
                    with open(target_path, 'wb') as f:
                        shutil.copyfileobj(extracted, f)
        finally:
            os.unlink(tmp_path)

        # 完整性校验：检查 MaxMind magic bytes，防止下载到损坏/伪造文件
        if not _verify_mmdb(target_path):
            try:
                os.unlink(target_path)
            except OSError:
                pass
            return {
                'success': False,
                'error': f'{mmdb_name} integrity check failed (invalid MaxMind file header)',
            }

        size_mb = round(os.path.getsize(target_path) / (1024 * 1024), 1)

        # 重置 reader，下次 geoip_lookup 自动重新加载
        with _geoip_lock:
            _geoip_reader = None

        # 确保路径在探测列表中
        if target_path not in GEOIP_DB_CANDIDATES:
            GEOIP_DB_CANDIDATES.insert(0, target_path)

        logger.info('%s downloaded (%.1f MB) → %s', mmdb_name, size_mb, target_path)
        return {'success': True, 'path': target_path, 'size_mb': size_mb}

    except HTTPError as e:
        if e.code == 451:
            return {
                'success': False,
                'error': (
                    f'HTTP 451: MaxMind requires signing the Data Processing Agreement (DPA). '
                    f'Please log in to maxmind.com → Account → Data Processing Agreements → sign the DPA. '
                    f'Alternatively, download {mmdb_name} manually from your MaxMind account and upload it below.'
                ),
            }
        if e.code == 401:
            return {
                'success': False,
                'error': (
                    f'HTTP 401: Authentication failed. '
                    f'Verify your Account ID and License Key. '
                    f'Note: MaxMind now requires both Account ID and License Key with Basic Auth.'
                ),
            }
        return {'success': False, 'error': f'HTTP {e.code}: {str(e)}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def install_geolite2_file(source_path: str) -> dict:
    """
    手动安装 .mmdb 文件到 analytics/data/ 目录。
    用户在 MaxMind 官网下载后通过后台手动上传。

    返回: {'success': True, 'path': '...', 'size_mb': 12.3}
         或 {'success': False, 'error': '...'}
    """
    import shutil

    if not os.path.exists(source_path):
        return {'success': False, 'error': f'File not found: {source_path}'}

    if not source_path.endswith('.mmdb'):
        return {'success': False, 'error': 'Only .mmdb files are supported'}

    target_dir = os.path.join(os.path.dirname(__file__), 'data')
    os.makedirs(target_dir, exist_ok=True)
    basename = os.path.basename(source_path)
    target_path = os.path.join(target_dir, basename)

    try:
        shutil.copy2(source_path, target_path)

        # 完整性校验：检查 MaxMind magic bytes，防止上传损坏/伪造文件
        if not _verify_mmdb(target_path):
            try:
                os.unlink(target_path)
            except OSError:
                pass
            return {
                'success': False,
                'error': f'{basename} integrity check failed (invalid MaxMind file header)',
            }

        size_mb = round(os.path.getsize(target_path) / (1024 * 1024), 1)

        # 重置 reader
        with _geoip_lock:
            _geoip_reader = None

        # 确保路径在探测列表中
        if target_path not in GEOIP_DB_CANDIDATES:
            GEOIP_DB_CANDIDATES.insert(0, target_path)

        logger.info('%s installed (%.1f MB) → %s', basename, size_mb, target_path)
        return {'success': True, 'path': target_path, 'size_mb': size_mb}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def install_ip2region_file(source_path: str) -> dict:
    """
    手动安装 ip2region .xdb 数据库到 analytics/data/。
    中国区用户可离线下载 ip2region_v4.xdb 后通过后台上传，无需访问 GitHub/Gitee。

    返回: {'success': True, 'path': '...', 'size_mb': 11.2}
         或 {'success': False, 'error': '...'}
    """
    import shutil

    if not os.path.exists(source_path):
        return {'success': False, 'error': f'File not found: {source_path}'}

    if not source_path.lower().endswith('.xdb'):
        return {'success': False, 'error': 'Only .xdb files are supported'}

    # 先校验源文件头（损坏/伪造文件不落盘）
    if not _verify_xdb(source_path):
        return {'success': False, 'error': 'Invalid .xdb file header'}

    target_dir = os.path.dirname(IP2REGION_DB)
    os.makedirs(target_dir, exist_ok=True)
    target_path = IP2REGION_DB

    try:
        shutil.copy2(source_path, target_path)

        # 再校验落盘文件，防止复制过程损坏
        if not _verify_xdb(target_path):
            try:
                os.unlink(target_path)
            except OSError:
                pass
            return {'success': False, 'error': 'xdb integrity check failed after copy'}

        size_mb = round(os.path.getsize(target_path) / (1024 * 1024), 1)

        # 重置 searcher 并立即重载，无需重启进程
        with _geoip_lock:
            _ip2region_searcher = None
        _init_ip2region()

        logger.info('ip2region xdb installed (%.1f MB) → %s', size_mb, target_path)
        return {'success': True, 'path': target_path, 'size_mb': size_mb}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def download_geolite2_cdn() -> dict:
    """
    从 jsDelivr CDN 免费镜像下载 GeoLite2-City.mmdb（无需注册 MaxMind）。
    文件每周自动更新两次（周二/周五 UTC 06:00）。

    返回: {'success': True, 'path': '...', 'size_mb': 12.3}
         或 {'success': False, 'error': '...'}
    """
    import shutil
    import gzip

    target_dir = os.path.join(os.path.dirname(__file__), 'data')
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, 'GeoLite2-City.mmdb')

    url = 'https://cdn.jsdelivr.net/npm/geolite2-city/GeoLite2-City.mmdb.gz'

    try:
        req = Request(url)
        resp = urlopen(req, timeout=120)

        # CDN 返回的是 .mmdb.gz，需要解压
        with gzip.GzipFile(fileobj=resp) as gz:
            with open(target_path, 'wb') as f:
                shutil.copyfileobj(gz, f)

        # 完整性校验：检查 MaxMind magic bytes，防止下载到损坏/伪造文件
        if not _verify_mmdb(target_path):
            try:
                os.unlink(target_path)
            except OSError:
                pass
            return {
                'success': False,
                'error': 'GeoLite2-City.mmdb integrity check failed (invalid MaxMind file header)',
            }

        size_mb = round(os.path.getsize(target_path) / (1024 * 1024), 1)

        # 重置 reader
        with _geoip_lock:
            _geoip_reader = None

        # 确保路径在探测列表中
        if target_path not in GEOIP_DB_CANDIDATES:
            GEOIP_DB_CANDIDATES.insert(0, target_path)

        # 立即重新加载 GeoIP reader
        init_geoip()

        logger.info('GeoLite2-City.mmdb downloaded from CDN (%.1f MB) → %s', size_mb, target_path)
        return {'success': True, 'path': target_path, 'size_mb': size_mb}

    except Exception as e:
        return {'success': False, 'error': str(e)}

def get_geoip_status() -> dict:
    """返回 GeoIP 数据库安装状态（ip2region + MaxMind）"""
    result = {}

    # ip2region
    if os.path.exists(IP2REGION_DB):
        stat = os.stat(IP2REGION_DB)
        result['ip2region'] = {
            'installed': True,
            'path': IP2REGION_DB,
            'size_mb': round(stat.st_size / (1024 * 1024), 1),
            'mtime': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime)),
        }
    else:
        result['ip2region'] = {'installed': False, 'path': None}

    # MaxMind GeoLite2
    mmdb_path = _find_db()
    if mmdb_path:
        stat = os.stat(mmdb_path)
        result['geolite2'] = {
            'installed': True,
            'path': mmdb_path,
            'size_mb': round(stat.st_size / (1024 * 1024), 1),
            'mtime': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime)),
        }
    else:
        result['geolite2'] = {'installed': False, 'path': None}

    return result


def download_ip2region_auto() -> dict:
    """
    从 GitHub 下载 ip2region_v4.xdb 到 analytics/data/ 目录。
    开源免费，无需注册。

    返回: {'success': True, 'path': '...', 'size_mb': 11.2}
         或 {'success': False, 'error': '...'}
    """
    import shutil

    target_dir = os.path.join(os.path.dirname(__file__), 'data')
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, 'ip2region_v4.xdb')

    # GitHub / Gitee 镜像（国内优先 Gitee，境外优先 GitHub）
    # 可用环境变量 IP2REGION_MIRROR 追加自定义镜像（如自建代理）
    urls = [
        'https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region.xdb',
        'https://gitee.com/mirrors/ip2region/raw/master/data/ip2region.xdb',
        'https://ghproxy.com/https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region.xdb',
    ]
    extra_mirror = os.environ.get('IP2REGION_MIRROR', '')
    if extra_mirror:
        urls.insert(0, extra_mirror)

    last_error = ''
    for url in urls:
        try:
            resp = urlopen(url, timeout=120)
            if resp.status == 200:
                with open(target_path, 'wb') as f:
                    shutil.copyfileobj(resp, f)

                # 完整性校验：检查 xdb 头部索引指针，防止下载到损坏/伪造文件
                if not _verify_xdb(target_path):
                    try:
                        os.unlink(target_path)
                    except OSError:
                        pass
                    last_error = f'Invalid xdb header from {url}'
                    continue

                size_mb = round(os.path.getsize(target_path) / (1024 * 1024), 1)

                # 重置 searcher，下次 geoip_lookup 自动重新加载
                with _geoip_lock:
                    _ip2region_searcher = None

                logger.info('ip2region.xdb downloaded (%.1f MB) → %s', size_mb, target_path)
                return {'success': True, 'path': target_path, 'size_mb': size_mb}
            else:
                last_error = f'HTTP {resp.status} from {url}'
        except Exception as e:
            last_error = f'{url}: {e}'

    return {'success': False, 'error': f'All mirrors failed: {last_error}'}
