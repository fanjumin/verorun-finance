"""net_proxy 通道注册表 —— 校验、凭据加密、requests proxies 构造。

职责边界（本模块**只做纯函数与校验**，不碰 Flask、不写 DB）：
  * 保存前校验通道字段（protocol / port / region / usage_tags / profile_tags）；
  * 凭据加密委托 crypto.py（fail-closed，绝不明文落库）；
  * 把通道行构造为 requests 的 `proxies` dict 与代理 URL。

分工（方案 §7.3）：
  * models.py  —— 落库/取库（只存 auth_password_enc 密文）
  * channels.py —— 本模块，字段语义与 URL 构造
  * routes.py   —— 校验失败转 400；本模块只抛 ChannelValidationError

安全（方案 §9.4）：
  * auth_password_enc / 解密后明文 **永不出参** 给前端；对外一律 mask_channel()；
  * 明文密码只在本模块内部瞬时存在，用于拼 URL。

连接层注意（plugin-standard-v1.8 §9.1 / §11.2）：
  本模块不建立任何 DB 连接，天然规避"模块级缓存连接"事故。
"""

import json

from . import crypto

__all__ = [
    'ChannelValidationError',
    'VALID_PROTOCOLS',
    'VALID_REGIONS',
    'VALID_USAGE_TAGS',
    'validate_channel_payload',
    'normalize_tags',
    'parse_tags',
    'build_proxy_url',
    'build_proxies',
    'socks5_available',
    'prepare_create_fields',
    'prepare_update_fields',
    'to_public',
]

# ── 枚举（与 migrations/v1.0.0_init.sql 的 DEFAULT 及 §7.3 一致）────────────
VALID_PROTOCOLS = ('http', 'https', 'socks5')
VALID_REGIONS = ('cn', 'os', 'any')

# usage_tags 建议取值（不强制封闭集合：允许登记自定义标签，但已知的做归一化提示）
VALID_USAGE_TAGS = (
    'llm', 'search', 'crawl', 'social', 'push', 'market', 'mail', 'generic',
)

# 端口合法区间
_PORT_MIN = 1
_PORT_MAX = 65535

# 名称长度上限（TEXT 无长度限制，这里做业务护栏）
_NAME_MAX_LEN = 128
_HOST_MAX_LEN = 255


class ChannelValidationError(ValueError):
    """通道字段校验失败。routes.py 捕获后转 400 `{success: false, error: str}`。"""


# ═══════════════════════════════════════════════════════════════════════════
# 标签与基础字段归一化
# ═══════════════════════════════════════════════════════════════════════════

def normalize_tags(value):
    """把标签入参归一化为 list[str]（去重保序、去空白、转小写）。

    接受形态：None / '' / 'a,b' / ['a','b'] / ('a',) / '["a","b"]'（JSON 文本）。
    非法类型抛 ChannelValidationError（不做静默丢弃，避免运维以为存上了）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith('['):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError) as e:
                raise ChannelValidationError('标签不是合法 JSON 数组：%s' % e)
            return normalize_tags(parsed)
        parts = [p for p in raw.replace('，', ',').split(',')]
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        raise ChannelValidationError('标签类型不合法，应为数组或逗号分隔字符串')

    out = []
    for item in parts:
        if item is None:
            continue
        tag = str(item).strip().lower()
        if not tag:
            continue
        if tag not in out:
            out.append(tag)
    return out


def parse_tags(value):
    """解析 DB 中存着的 JSON 文本标签列（'[]' / '["llm"]'）。容错返回 []。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return normalize_tags(value)
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return []
    return normalize_tags(parsed)


def dump_tags(tags):
    """序列化为 DB 列存法（JSON 文本，紧凑无空格）。"""
    return json.dumps(normalize_tags(tags), ensure_ascii=False, separators=(',', ':'))


def _clean_str(value, field, max_len=None, required=False):
    """字符串字段清洗。required=True 时空白 → 报错。"""
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ChannelValidationError('%s 应为字符串' % field)
    out = value.strip()
    if required and not out:
        raise ChannelValidationError('%s 不能为空' % field)
    if max_len and len(out) > max_len:
        raise ChannelValidationError('%s 超长（上限 %d 字符）' % (field, max_len))
    return out


def _clean_int(value, field, minimum=None, maximum=None, default=None):
    """整数字段清洗。非整数（含 '10' 字符串）做一次宽容转换。"""
    if value is None or value == '':
        if default is None:
            raise ChannelValidationError('%s 不能为空' % field)
        return default
    if isinstance(value, bool):
        raise ChannelValidationError('%s 应为整数' % field)
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except (ValueError, AttributeError):
            raise ChannelValidationError('%s 应为整数' % field)
    if not isinstance(value, int):
        raise ChannelValidationError('%s 应为整数' % field)
    if minimum is not None and value < minimum:
        raise ChannelValidationError('%s 不得小于 %d' % (field, minimum))
    if maximum is not None and value > maximum:
        raise ChannelValidationError('%s 不得大于 %d' % (field, maximum))
    return value


def _check_socks_support():
    """socks5 依赖探测（§7.3）：PySocks 不可导入时拒绝保存并给出安装指引。"""
    try:
        import socks  # noqa: F401  (PySocks 的 import 名就是 socks)
    except ImportError:
        raise ChannelValidationError(
            '当前环境未安装 PySocks，无法使用 socks5 通道；'
            '请先执行 pip install requests[socks]')


# ═══════════════════════════════════════════════════════════════════════════
# 校验入口
# ═══════════════════════════════════════════════════════════════════════════

def validate_channel_payload(payload, require_all=True):
    """校验并归一化通道入参，返回干净 dict。

    Args:
        payload: 原始 dict（来自 routes.py 的 request.get_json()）。
        require_all: True（建）/ False（改）—— 改时允许字段缺席。

    Returns:
        dict，键为 DB 列名（含 auth_password_enc 或不含 auth_password 相关键）。

    Raises:
        ChannelValidationError: 任一字段非法；routes.py 转 400。
    """
    if not isinstance(payload, dict):
        raise ChannelValidationError('请求体应为 JSON 对象')

    out = {}

    # ── name ──
    if require_all or 'name' in payload:
        out['name'] = _clean_str(payload.get('name'), 'name',
                                 max_len=_NAME_MAX_LEN, required=True)

    # ── protocol ──
    if require_all or 'protocol' in payload:
        protocol = _clean_str(payload.get('protocol') or 'http', 'protocol').lower()
        if protocol not in VALID_PROTOCOLS:
            raise ChannelValidationError(
                'protocol 非法：%s（可选 %s）'
                % (protocol or '(空)', '/'.join(VALID_PROTOCOLS)))
        if protocol == 'socks5':
            _check_socks_support()
        out['protocol'] = protocol

    # ── host ──
    if require_all or 'host' in payload:
        host = _clean_str(payload.get('host'), 'host',
                          max_len=_HOST_MAX_LEN, required=True)
        if '://' in host:
            raise ChannelValidationError(
                'host 只填主机名或 IP（不含 scheme），当前：%s' % host)
        if any(ch in host for ch in ('/', '?', '#')):
            raise ChannelValidationError(
                'host 不应包含路径/查询串，当前：%s' % host)
        out['host'] = host

    # ── port ──
    if require_all or 'port' in payload:
        out['port'] = _clean_int(payload.get('port'), 'port',
                                 minimum=_PORT_MIN, maximum=_PORT_MAX)

    # ── region ──
    if require_all or 'region' in payload:
        region = _clean_str(payload.get('region') or 'any', 'region').lower()
        if region not in VALID_REGIONS:
            raise ChannelValidationError(
                'region 非法：%s（可选 %s）' % (region or '(空)', '/'.join(VALID_REGIONS)))
        out['region'] = region

    # ── auth_username：随密码一起给才生效 ──
    if require_all or 'auth_username' in payload:
        out['auth_username'] = _clean_str(
            payload.get('auth_username'), 'auth_username', max_len=_NAME_MAX_LEN)

    # ── auth_password：转密文，明文绝不外流 ──
    if 'auth_password' in payload:
        password = payload.get('auth_password')
        if password in (None, ''):
            # 空 = 不改密码（改场景）或未设密码（建场景）；
            # 建场景由 prepare_create_fields 显式置 '' 密文
            if require_all:
                out['auth_password_enc'] = ''
        else:
            if not isinstance(password, str):
                raise ChannelValidationError('auth_password 应为字符串')
            if not out.get('auth_username') and require_all:
                raise ChannelValidationError(
                    '设置了 auth_password 时必须同时提供 auth_username')
            # fail-closed：crypto.encrypt 在密钥不可用时抛 CredentialError，
            # 这里原样上抛，由 routes.py 统一转 503（服务端配置问题，非入参错误）
            # 并提示配置 ENCRYPTION_KEY
            out['auth_password_enc'] = crypto.encrypt(password)

    # ── 标签 ──
    if require_all or 'usage_tags' in payload:
        out['usage_tags'] = dump_tags(payload.get('usage_tags'))
    if require_all or 'profile_tags' in payload:
        out['profile_tags'] = dump_tags(payload.get('profile_tags'))

    # ── weight ──
    if require_all or 'weight' in payload:
        out['weight'] = _clean_int(payload.get('weight'), 'weight',
                                   minimum=1, maximum=100, default=1)

    # ── max_concurrent（0 = 不限）──
    if require_all or 'max_concurrent' in payload:
        out['max_concurrent'] = _clean_int(
            payload.get('max_concurrent'), 'max_concurrent',
            minimum=0, maximum=10000, default=0)

    # ── enabled ──
    if require_all or 'enabled' in payload:
        raw = payload.get('enabled', True)
        if isinstance(raw, bool):
            out['enabled'] = raw
        elif isinstance(raw, str):
            low = raw.strip().lower()
            if low in ('1', 'true', 'yes', 'on'):
                out['enabled'] = True
            elif low in ('0', 'false', 'no', 'off', ''):
                out['enabled'] = False
            else:
                raise ChannelValidationError('enabled 应为布尔值')
        elif isinstance(raw, int) and raw in (0, 1):
            out['enabled'] = bool(raw)
        else:
            raise ChannelValidationError('enabled 应为布尔值')

    return out


def prepare_create_fields(payload):
    """建通道：校验后补齐必填键，返回可直接展开给 models.create_channel 的 dict。"""
    fields = validate_channel_payload(payload, require_all=True)
    fields.setdefault('auth_username', '')
    fields.setdefault('auth_password_enc', '')
    fields.setdefault('region', 'any')
    fields.setdefault('profile_tags', '[]')
    fields.setdefault('usage_tags', '[]')
    fields.setdefault('weight', 1)
    fields.setdefault('max_concurrent', 0)
    fields.setdefault('enabled', True)
    return fields


def prepare_update_fields(payload):
    """改通道：只返回本次确实要改的键。

    关键约定（§8 PUT 契约）：`auth_password` 缺席或为空串 = **不变更密码**，
    绝不用空值覆盖已有密文。
    """
    fields = validate_channel_payload(payload, require_all=False)
    # 用户显式清空用户名但没给新密码 → 一并清掉密码，避免留下孤儿凭据
    if fields.get('auth_username') == '' and 'auth_password_enc' not in fields:
        fields['auth_password_enc'] = ''
    return fields


# ═══════════════════════════════════════════════════════════════════════════
# proxies 构造
# ═══════════════════════════════════════════════════════════════════════════

def _quote(s):
    """URL userinfo 转义：RFC 3986 保留字符必须百分号编码，否则密码含 @/:/? 会切错 URL。"""
    from urllib.parse import quote
    return quote(str(s), safe='')


def build_proxy_url(channel, password=None):
    """构造代理 URL。

    Args:
        channel: 通道 dict 或 PgRow；至少含 protocol / host / port，
                 可选 auth_username / auth_password_enc。
        password: 已解密的明文密码；None 时尝试就地解密 auth_password_enc。
                  显式传 '' 表示明确不用认证。

    Returns:
        str，形如 'http://user:pass@host:port' 或 'socks5://host:1080'。

    Raises:
        ChannelValidationError: 缺 protocol/host/port，或解密失败。
    """
    if not channel:
        raise ChannelValidationError('通道数据为空，无法构造代理 URL')
    data = dict(channel)

    protocol = (data.get('protocol') or '').strip().lower()
    if protocol not in VALID_PROTOCOLS:
        raise ChannelValidationError('通道 protocol 非法：%s' % (protocol or '(空)'))
    host = (data.get('host') or '').strip()
    if not host:
        raise ChannelValidationError('通道 host 为空，无法构造代理 URL')
    port = data.get('port')
    if port is None:
        raise ChannelValidationError('通道 port 为空，无法构造代理 URL')
    try:
        port = int(port)
    except (ValueError, TypeError):
        raise ChannelValidationError('通道 port 非整数：%r' % (data.get('port'),))

    username = (data.get('auth_username') or '').strip()

    if password is None:
        enc = data.get('auth_password_enc') or ''
        password = crypto.decrypt(enc) if enc else ''
    password = password or ''

    if username and password:
        userinfo = '%s:%s@' % (_quote(username), _quote(password))
    elif username:
        # 仅用户名（少数代理允许空密码）
        userinfo = '%s@' % _quote(username)
    else:
        userinfo = ''

    return '%s://%s%s:%d' % (protocol, userinfo, host, port)


def socks5_available() -> bool:
    """运行时探测 PySocks 是否可用（§7.3 socks5 前提）。

    requests 走 socks5 需要 PySocks 提供 SOCKS 支持；缺失时 requests 会在
    `socks5://` scheme 上抛 InvalidSchema，报错晦涩（"Missing dependencies for
    SOCKS support"）。这里提前探测，便于上层给出可操作的提示。
    """
    try:
        import socks  # noqa: F401  PySocks
    except ImportError:
        return False
    return True


def build_proxies(channel, password=None):
    """构造 requests 的 proxies dict（§7.3）。

    requests 不区分 http/https 代理池时两键同值；socks5 用 socks5:// scheme。
    通道不可构造时返回 `{}`（调用方按 DIRECT 处理），但**不吞掉解密错误**
    —— 解密失败是配置事故，必须让上层看到。

    socks5 依赖（2026-09-22 缺口处理）：
      本仓库两个 Python 环境（core-win\\venv、payload\\runtime\\python）默认都
      **没有 PySocks**。没有它时 socks5 通道 100% 失败且报错难懂。此处显式
      探测，缺失则抛 ChannelValidationError 给出明确指引，而不是让 requests
      抛 "Missing dependencies for SOCKS support"。
    """
    try:
        url = build_proxy_url(channel, password=password)
    except crypto.CredentialError:
        raise
    except ChannelValidationError:
        return {}

    protocol = (channel.get('protocol') if hasattr(channel, 'get')
                else getattr(channel, 'protocol', None))
    if protocol == 'socks5' and not socks5_available():
        raise ChannelValidationError(
            'socks5 通道需要 PySocks 支持，当前 Python 环境未安装。'
            '请安装 PySocks 后重试（pip install PySocks），'
            '或改用 http/https 协议的代理通道。')
    return {'http': url, 'https': url}


# ═══════════════════════════════════════════════════════════════════════════
# 对外出参
# ═══════════════════════════════════════════════════════════════════════════

def to_public(channel):
    """通道行 → 前端可见形态。凭据只出掩码，密文与明文永不出参。

    额外补充：把 DB 里的 JSON 文本标签解析成数组，便于前端直接渲染。
    """
    if not channel:
        return channel
    out = crypto.mask_channel(dict(channel))
    out['usage_tags'] = parse_tags(out.get('usage_tags'))
    out['profile_tags'] = parse_tags(out.get('profile_tags'))
    # 显式清除任何可能残留的凭据字段
    out.pop('auth_password', None)
    for key in ('fused_until', 'last_probe_at', 'created_at', 'updated_at'):
        if out.get(key) is not None and not isinstance(out.get(key), str):
            out[key] = out[key].isoformat()
    out['fused'] = _is_fused(channel)
    return out


def _is_fused(channel):
    """该通道当前是否处于熔断冷却期。"""
    fused_until = None
    try:
        fused_until = channel.get('fused_until') if hasattr(channel, 'get') else None
    except Exception:
        return False
    if not fused_until:
        return False
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    if isinstance(fused_until, datetime.datetime):
        moment = fused_until
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
    else:
        try:
            moment = datetime.datetime.fromisoformat(str(fused_until))
        except (ValueError, TypeError):
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment > now
