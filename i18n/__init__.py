#!/usr/bin/env python3
"""
i18n — Internationalization Module (DB-first + YAML fallback)

Data priority:
    1. i18n_strings table (hot-reloadable, editable from admin panel)
    2. YAML fallback files
    3. Original text (fallback)

Usage:
    from i18n import _, get_lang

    # In Python code
    return api_err(_('Please enter a valid phone number'))

    # In Jinja2 templates (injected via app.context_processor)
    <h1>{{ _('Log In') }}</h1>

    # Write translations from admin
    from i18n import set_translation, seed_from_yaml
    set_translation('en', '登录', 'Login')
    seed_from_yaml('en')  # sync YAML to DB
"""
import os
import hashlib
import functools
import yaml

_market = os.environ.get('DEPLOY_MARKET', 'cn')
DEPLOY_LANG = os.environ.get('DEPLOY_LANG', 'en')

# ─── YAML fallback 缓存（模块加载时一次性读取） ───
_yaml_cache = {}

def _load_yaml(locale: str = None) -> dict:
    """读取 YAML 翻译文件，返回 dict（文件不存在返回空）"""
    locale = locale or DEPLOY_LANG
    if locale in _yaml_cache:
        return _yaml_cache[locale]
    yml_path = os.path.join(os.path.dirname(__file__), f'{locale}.yml')
    try:
        with open(yml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        _yaml_cache[locale] = data
        return data
    except FileNotFoundError:
        print(f'[i18n] Warning: YAML file not found: {yml_path}')
        return {}
    except Exception as e:
        print(f'[i18n] Error loading YAML: {e}')
        return {}


# ─── DB 连接 ───
def _get_db_path() -> str:
    """获取数据库路径，与环境变量和项目结构一致"""
    return os.environ.get('DB_PATH', os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'x7k2m9a4.db'
    ))


def _get_db():
    """返回 PostgreSQL 数据库连接（与 models.database 配置一致）"""
    import psycopg2
    import psycopg2.extras
    PG_CONFIG = {
        'host': os.environ.get('PG_HOST', 'localhost'),
        'port': int(os.environ.get('PG_PORT', 5432)),
        'dbname': os.environ.get('PG_DB', 'appdb'),
        'user': os.environ.get('PG_USER', 'app'),
        'password': os.environ.get('PG_PASSWORD', ''),
    }
    conn = psycopg2.connect(**PG_CONFIG)
    conn.autocommit = False

    class _Wrapper:
        """Minimal wrapper: provides execute() on a psycopg2 connection."""
        def __init__(self, conn):
            self._conn = conn
            self._cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        def execute(self, sql, params=None):
            if params is not None:
                self._cur.execute(sql, params)
            else:
                self._cur.execute(sql)
            return self
        def fetchone(self): return self._cur.fetchone()
        def fetchall(self): return self._cur.fetchall()
        def commit(self):   self._conn.commit()
        def close(self):
            self._cur.close()
            self._conn.close()
    return _Wrapper(conn)


def _source_hash(text: str) -> str:
    """生成原文的 hash 用于索引查找"""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


# ─── 语言协商（规范 §5） ──────────────────────────────────

SUPPORTED_LOCALES = ('en', 'zh-CN')


def _normalize_locale(value) -> str:
    """将任意语言标识规范化：zh* → 'zh-CN'，en* → 'en'，未知返回 None。"""
    value = (value or '').strip().lower().replace('_', '-')
    if not value:
        return None
    if value.startswith('zh'):
        return 'zh-CN'
    if value.startswith('en'):
        return 'en'
    return None


def resolve_locale(lang_param=None, cookie=None, accept_header=None,
                   user_pref=None, fallback=None) -> str:
    """按规范 §5 优先级解析请求语言：
    ?lang= → Cookie lang → Accept-Language → 用户偏好 → 部署默认。"""
    loc = _normalize_locale(lang_param)
    if loc:
        return loc
    loc = _normalize_locale(cookie)
    if loc:
        return loc
    if accept_header:
        for part in accept_header.split(','):
            loc = _normalize_locale(part.split(';')[0])
            if loc:
                return loc
    loc = _normalize_locale(user_pref)
    if loc:
        return loc
    loc = _normalize_locale(fallback)
    if loc:
        return loc
    return DEPLOY_LANG


def _current_locale() -> str:
    """返回当前语言：请求上下文内优先 g.lang_code，否则部署默认。"""
    try:
        from flask import g, has_request_context
        if has_request_context():
            lang = getattr(g, 'lang_code', None)
            if lang:
                return lang
    except Exception:
        pass
    return DEPLOY_LANG


# ─── 核心翻译函数 ───

def _(text: str, locale: str = None, **kwargs) -> str:
    """
    翻译一个字符串。
    优先级：DB → YAML fallback → 原文
    支持参数替换：_('Hello {name}', name='World')
    """
    if not text:
        return ''

    locale = locale or _current_locale()

    # 从内存缓存字典取（get_all_translations 带 lru_cache，
    # 内部已按 YAML 打底 + DB 覆盖，结果与逐条查 DB 等价）。
    # 一个 locale 仅首次触发 1 次 DB 全量读取，之后全部命中内存，
    # 避免每个 {{ _() }} 都新建 SQLite 连接导致的严重性能问题。
    translations = get_all_translations(locale)
    result = translations.get(text)
    if result:  # 空串/None 时回退原文，与原 DB 优先逻辑一致
        if kwargs:
            return result.format(**kwargs)
        return result

    # 回退原文
    if kwargs:
        return text.format(**kwargs)
    return text


def _t(text: str, locale: str = None, **kwargs) -> str:
    """别名，同 _()"""
    return _(text, locale, **kwargs)


# ─── 复数与区域化格式（规范 §6） ──────────────────────────

def _plural(singular: str, plural: str, count) -> str:
    """按 count 选择单/复数形态并替换 {count} 占位符。

    zh-CN 无复数形态恒用 singular；en 中 count==1 用 singular，其余用 plural。
    用法：_plural('{count} item', '{count} items', count)
    """
    text = singular
    if _current_locale() == 'en' and count != 1:
        text = plural
    return text.format(count=count) if '{count}' in text else text


def format_number(value, locale: str = None, decimals: int = None) -> str:
    """按 locale 输出数字（千分位逗号分隔）。

    format_number(1234567.891, decimals=2) -> '1,234,567.89'
    """
    locale = locale or _current_locale()
    try:
        if decimals is not None:
            value = round(float(value), decimals)
            if decimals == 0:
                value = int(value)
        return f'{value:,}'
    except Exception:
        return str(value)


def format_date(value, locale: str = None, fmt: str = '%Y-%m-%d') -> str:
    """按 locale 格式化日期（支持 datetime/date/ISO 字符串）。

    zh-CN -> '2026年08月18日'；en（或自定义 fmt）-> '2026-08-18'
    """
    locale = locale or _current_locale()
    from datetime import datetime
    from datetime import date as _date_cls
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except Exception:
            return value
    if isinstance(value, (_date_cls, datetime)):
        if locale == 'zh-CN':
            return value.strftime('%Y年%m月%d日')
        return value.strftime(fmt)
    return str(value)


def format_currency(value, locale: str = None, currency: str = None) -> str:
    """按 locale/currency 输出货币金额。

    locale 决定默认币种（zh-CN→CNY ¥，其余→USD $）；currency 显式指定时优先。
    format_currency(1234.5, 'zh-CN') -> '¥1,234.50'
    format_currency(1234.5, 'en')    -> '$1,234.50'
    """
    locale = locale or _current_locale()
    try:
        if currency is None:
            currency = 'CNY' if locale == 'zh-CN' else 'USD'
        s = f'{float(value):,.2f}'
        if currency in ('CNY', 'RMB'):
            return f'¥{s}'
        return f'${s}'
    except Exception:
        return str(value)


# ─── 管理函数 ───

def set_translation(locale: str, source: str, translation: str,
                    is_auto: int = 0) -> bool:
    """
    写入/更新一条翻译到 DB。
    用于管理后台编辑。
    """
    if not source:
        return False
    s_hash = _source_hash(source)
    try:
        conn = _get_db()
        conn.execute(
            '''INSERT INTO i18n_strings (locale, source_hash, source, translation, is_auto)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT(locale, source_hash) DO UPDATE SET
                   translation=excluded.translation,
                   is_auto=excluded.is_auto,
                   updated_at=NOW()''',
            (locale, s_hash, source, translation, is_auto)
        )
        conn.commit()
        conn.close()
        get_all_translations.cache_clear()
        return True
    except Exception as e:
        print(f'[i18n] set_translation error: {e}')
        return False


def delete_translation(translation_id: int) -> bool:
    """删除一条翻译"""
    try:
        conn = _get_db()
        conn.execute('DELETE FROM i18n_strings WHERE id=%s', (translation_id,))
        conn.commit()
        conn.close()
        get_all_translations.cache_clear()
        return True
    except Exception as e:
        print(f'[i18n] delete_translation error: {e}')
        return False


@functools.lru_cache(maxsize=4)
def get_all_translations(locale: str = None) -> dict:
    """
    返回完整翻译字典（用于前端注入）。
    优先从 DB 读，缺失的从 YAML 补。
    """
    locale = locale or DEPLOY_LANG
    result = {}

    # YAML 基础
    result.update(_load_yaml(locale))

    # DB 覆盖
    try:
        conn = _get_db()
        rows = conn.execute(
            'SELECT source, translation FROM i18n_strings WHERE locale=%s',
            (locale,)
        ).fetchall()
        conn.close()
        for row in rows:
            result[row['source']] = row['translation']
    except Exception:
        pass

    return result


def list_translations(locale: str = None, search: str = '',
                      offset: int = 0, limit: int = 50) -> dict:
    """
    从 DB 列出翻译（分页+搜索），用于管理后台。
    返回: {total, items: [{id, locale, source, translation, is_auto, updated_at}]}
    """
    locale = locale or DEPLOY_LANG
    try:
        conn = _get_db()
        where = 'WHERE locale=%s'
        params = [locale]
        if search:
            where += ' AND (source LIKE %s OR translation LIKE %s)'
            s = f'%{search}%'
            params.extend([s, s])

        total = conn.execute(
            f'SELECT COUNT(*) as c FROM i18n_strings {where}', params
        ).fetchone()['c']

        rows = conn.execute(
            f'SELECT id, locale, source, translation, is_auto, updated_at '
            f'FROM i18n_strings {where} ORDER BY updated_at DESC LIMIT %s OFFSET %s',
            params + [limit, offset]
        ).fetchall()
        conn.close()

        return {
            'total': total,
            'items': [dict(r) for r in rows],
        }
    except Exception as e:
        print(f'[i18n] list_translations error: {e}')
        return {'total': 0, 'items': []}


def seed_from_yaml(locale: str = None) -> int:
    """
    将 YAML 文件中的翻译导入到 DB（已存在的跳过）。
    使用 pg_try_advisory_lock 避免多进程阻塞，finally 确保锁释放。
    返回本次导入的数量。
    """
    locale = locale or DEPLOY_LANG
    yml = _load_yaml(locale)
    if not yml:
        return 0

    count = 0
    conn = None
    lock_id = int(hashlib.md5(f'i18n_yaml_{locale}'.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF
    try:
        conn = _get_db()
        acquired = conn.execute(
            'SELECT pg_try_advisory_lock(%s)', (lock_id,)
        ).fetchone()
        if not acquired or not list(acquired.values())[0]:
            return 0  # 其他进程正在播种，跳过
        for source, translation in yml.items():
            if not source or not translation or source == translation:
                continue  # 跳过无效条目和源=译的条目
            s_hash = _source_hash(source)
            exist = conn.execute(
                'SELECT id FROM i18n_strings WHERE locale=%s AND source_hash=%s',
                (locale, s_hash)
            ).fetchone()
            if not exist:
                conn.execute(
                    'INSERT INTO i18n_strings (locale, source_hash, source, translation, is_auto) VALUES (%s,%s,%s,%s,%s)',
                    (locale, s_hash, source, translation, 1)
                )
                count += 1
        conn.commit()
        if count:
            get_all_translations.cache_clear()
        print(f'[i18n] Seeded {count} translations from {locale}.yml')
    except Exception as e:
        print(f'[i18n] seed_from_yaml error: {e}')
    finally:
        if conn:
            try:
                conn.execute('SELECT pg_advisory_unlock(%s)', (lock_id,))
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
    return count


def get_lang() -> str:
    """返回当前语言代码（请求上下文内优先 g.lang_code，否则部署默认）"""
    return _current_locale()


# ─── 插件翻译支持 ──────────────────────────────────────────

def seed_plugin_translations(plugin_id: str, locale_dir: str) -> int:
    """
    将插件 locale 目录下的 YAML 翻译文件写入 i18n DB。
    每次调用都会 UPSERT（根据 locale+source_hash 去重），幂等安全。
    返回本次写入的条目数。
    """
    count = 0
    for locale in ('zh-CN', 'en'):
        yml_path = os.path.join(locale_dir, f'{locale}.yml')
        if not os.path.isfile(yml_path):
            continue
        try:
            with open(yml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f'[i18n] plugin {plugin_id} {locale} load error: {e}')
            continue
        if not data:
            continue
        conn = None
        lock_id = int(hashlib.md5('i18n_plugin_seed'.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF
        try:
            conn = _get_db()
            acquired = conn.execute(
                'SELECT pg_try_advisory_lock(%s)', (lock_id,)
            ).fetchone()
            if not acquired or not list(acquired.values())[0]:
                continue  # 其他进程正在播种，跳过
            for source, translation in data.items():
                if not source or not translation:
                    continue
                s_hash = _source_hash(source)
                conn.execute(
                    '''INSERT INTO i18n_strings
                       (locale, source_hash, source, translation, is_auto)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT(locale, source_hash) DO UPDATE SET
                           translation=EXCLUDED.translation,
                           is_auto=EXCLUDED.is_auto''',
                    (locale, s_hash, source, translation, 1)
                )
                count += 1
            conn.commit()
        except Exception as e:
            print(f'[i18n] plugin {plugin_id} {locale} db error: {e}')
        finally:
            if conn:
                try:
                    conn.execute('SELECT pg_advisory_unlock(%s)', (lock_id,))
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
    if count:
        print(f'[i18n] plugin {plugin_id}: seeded {count} translations')
    get_all_translations.cache_clear()  # 无论播种/跳过都清缓存，防多 worker 竞态缓存空字典
    return count
