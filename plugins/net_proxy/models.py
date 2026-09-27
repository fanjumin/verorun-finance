"""net_proxy 数据访问层。

遵循 plugin-standard-v1.8 §9.1 / §11.2：
  * 单库多 Schema —— 独立 PG schema `net_proxy`，禁止独立数据库；
  * 统一走 plugins._base.db.get_pooled_connection() / get_raw_connection()，
    禁止内联 psycopg2.connect()；
  * 连接生命周期：借还成对，with 语句保证归还，**严禁模块级缓存连接**
    （历史事故：有插件模块级缓存连接导致 PG max_connections 打满，
    全站间歇性 401/500）；
  * SQL 一律用 ? 占位符（plugins/_base/db.py::_replace_placeholders 垫片
    转 %s）；**禁止** jsonb ? 运算符（§18.6.2，2026-09-17 商店 500 事故根因）。

迁移机制对齐 plugins/hr_recruit/models.py：
  advisory 锁 + schema_migrations 表 run-once + 逐文件提交。
"""

import os

from plugins._base.db import get_pooled_connection, get_raw_connection

SCHEMA = 'net_proxy'
SCHEMA_VERSION_TABLE = 'schema_migrations'
_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'migrations')

# 迁移 advisory lock 键：事务级 pg_try_advisory_xact_lock，取值须与其他插件错开
# 0x4E505831 == 'NPX1'
_MIGRATE_LOCK_KEY = 0x4E505831

__all__ = [
    'SCHEMA',
    'SCHEMA_VERSION_TABLE',
    'get_net_proxy_db',
    'init_schema',
    'run_migrations',
    'drop_schema',
    'table_exists',
    'LOG_TABLES',
    # 通道
    'list_channels',
    'get_channel',
    'create_channel',
    'update_channel',
    'delete_channel',
    # 规则
    'list_rules',
    'get_rule',
    'create_rule',
    'update_rule',
    'delete_rule',
    # 探活 / 计分
    'record_probe',
    'record_channel_result',
    'pick_healthy_channels',
    'list_channels_for_probe',
    # 审计
    'write_request_log',
    'list_request_log',
    'list_probe_log',
    'request_stats',
    'fused_channel_count',
    'cleanup_logs',
]

# 日志表 → 保留期配置键（保留期取值见 plugin.json settings_schema）
LOG_TABLES = {
    'proxy_request_log': 'request_log_retention_days',
    'proxy_probe_log': 'probe_log_retention_days',
}


class _Conn:
    """上下文管理器：借池 → 设 search_path → 退出归还。

    归还前重置 search_path，避免污染池中其他插件的连接状态。
    注意：get_pooled_connection() 返回的是 PgConnection 包装（有 .execute()），
    而 get_raw_connection() 返回裸 psycopg2 连接（**无 .execute()**，必须显式
    取游标）—— 历史事故：误用 raw 连接的 .execute() 导致插件 setup 直接
    AttributeError、状态卡在 enabled。
    """

    def __init__(self):
        self._conn = None

    def __enter__(self):
        self._conn = get_pooled_connection()
        try:
            cur = self._conn.cursor()
            cur.execute('SET search_path TO %s, public' % SCHEMA)
            cur.close()
        except Exception:
            # schema 不存在时 SET search_path 会失败 → 回退 public，不阻断建表前的查询
            pass
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            try:
                cur = self._conn.cursor()
                cur.execute('SET search_path TO public')
                cur.close()
            except Exception:
                pass
            self._conn.close()
        return False


def get_net_proxy_db():
    """统一借取入口。调用方一律：with get_net_proxy_db() as conn:"""
    return _Conn()


# ═══════════════════════════════════════════════════════════════════════════
# Schema 生命周期
# ═══════════════════════════════════════════════════════════════════════════

def init_schema():
    """幂等建 schema。

    用一次性裸连接先建 schema，避免 SET search_path 在 schema 不存在时失败
    （risk_control/models.py::init_schema 同款先例）。
    """
    conn = get_raw_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute('CREATE SCHEMA IF NOT EXISTS %s' % SCHEMA)
        finally:
            cur.close()
        conn.commit()
    finally:
        conn.close()


def run_migrations():
    """按文件名顺序执行 migrations/*.sql；advisory 锁 + schema_migrations 保证并发与幂等。

    规则（对应 hr_recruit 批次 B R-7）：
      * 每个 SQL 文件只执行一次，文件名记入 net_proxy.schema_migrations；
      * 整个迁移过程持有事务级 advisory lock（pg_try_advisory_xact_lock），
        多 worker 并发时仅放行一个执行者，其余本次跳过；
      * 逐文件提交：单个文件失败即回滚该文件，已成功文件不受影响；
      * schema_migrations 随插件 schema 一起 DROP，重装后自然重新执行。

    Returns:
        list[str]: 本次实际执行的迁移文件名列表。
    """
    applied = []
    if not os.path.isdir(_MIGRATIONS_DIR):
        return applied
    files = sorted(f for f in os.listdir(_MIGRATIONS_DIR) if f.endswith('.sql'))
    if not files:
        return applied

    # 先确保 schema 存在（search_path 依赖它）
    init_schema()

    conn = get_raw_connection()
    try:
        cur = conn.cursor()
        cur.execute('SET search_path TO %s, public' % SCHEMA)
        cur.execute('SELECT pg_try_advisory_xact_lock(%s)', (_MIGRATE_LOCK_KEY,))
        if not cur.fetchone()[0]:
            cur.close()
            return applied  # 其他进程正在迁移
        cur.execute(
            'CREATE TABLE IF NOT EXISTS %s.%s ('
            '  filename varchar(255) PRIMARY KEY,'
            '  applied_at timestamptz NOT NULL DEFAULT now())' % (SCHEMA, SCHEMA_VERSION_TABLE)
        )
        cur.execute('SELECT filename FROM %s.%s' % (SCHEMA, SCHEMA_VERSION_TABLE))
        done = {row[0] for row in cur.fetchall()}
        cur.close()

        for fn in files:
            if fn in done:
                continue
            with open(os.path.join(_MIGRATIONS_DIR, fn), 'r', encoding='utf-8') as f:
                sql = f.read()
            cur = conn.cursor()
            try:
                cur.execute(sql)
                cur.execute(
                    # 裸 psycopg2 游标（非 _base 垫片）→ 占位符须原生 %s。
                    # 此处经 % 格式化：%%s 转义后落到 SQL 即 %s。
                    'INSERT INTO %s.%s (filename) VALUES (%%s)'
                    % (SCHEMA, SCHEMA_VERSION_TABLE),
                    (fn,),
                )
                conn.commit()
                applied.append(fn)
            finally:
                cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return applied


def drop_schema():
    """卸载零残留：删除插件 schema（§12.5 / §4.2）。

    注意：仅 DROP 插件 schema，绝不 CASCADE 到 public。
    **调用方须先经用户明确同意**（铁律：破坏性操作先方案后执行）。
    """
    conn = get_raw_connection()
    try:
        cur = conn.cursor()
        cur.execute('DROP SCHEMA IF EXISTS %s CASCADE' % SCHEMA)
        conn.commit()
        cur.close()
    finally:
        conn.close()


def table_exists(table: str) -> bool:
    """判断本插件 schema 下某表是否存在。"""
    with get_net_proxy_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_schema = ? AND table_name = ?",
            (SCHEMA, table),
        ).fetchone()
    return bool(row)


# ═══════════════════════════════════════════════════════════════════════════
# 通道 CRUD（proxy_channels）
# ═══════════════════════════════════════════════════════════════════════════

_CHANNEL_COLS = (
    'id, name, protocol, host, port, auth_username, auth_password_enc,'
    ' region, profile_tags, usage_tags, weight, max_concurrent, enabled,'
    ' fused_until, consecutive_failures, last_latency_ms, last_probe_at,'
    ' created_at, updated_at'
)


def list_channels(enabled_only: bool = False):
    """列出全部通道。enabled_only=True 时仅返回 enabled=TRUE 的。"""
    sql = 'SELECT %s FROM proxy_channels' % _CHANNEL_COLS
    if enabled_only:
        sql += ' WHERE enabled = TRUE'
    sql += ' ORDER BY id ASC'
    with get_net_proxy_db() as conn:
        return conn.execute(sql).fetchall()


def get_channel(channel_id: int):
    """按 id 取单条通道，不存在返回 None。"""
    with get_net_proxy_db() as conn:
        return conn.execute(
            'SELECT %s FROM proxy_channels WHERE id = ?' % _CHANNEL_COLS,
            (channel_id,),
        ).fetchone()


def create_channel(name: str, protocol: str, host: str, port: int, **fields):
    """新建通道，返回新行 id。

    端点与凭据全部运行时登记，代码零预置。加密在调用方（channels.py）
    完成 —— 本层只负责落库。
    """
    cols = ['name', 'protocol', 'host', 'port']
    vals = [name, protocol, host, port]
    for key in ('auth_username', 'auth_password_enc', 'region', 'profile_tags',
                'usage_tags', 'weight', 'max_concurrent', 'enabled'):
        if key in fields and fields[key] is not None:
            cols.append(key)
            vals.append(fields[key])
    placeholders = ', '.join(['?'] * len(cols))
    sql = 'INSERT INTO proxy_channels (%s) VALUES (%s) RETURNING id' % (
        ', '.join(cols), placeholders)
    with get_net_proxy_db() as conn:
        return conn.execute(sql, tuple(vals)).fetchone()['id']


def update_channel(channel_id: int, **fields):
    """局部更新通道。返回受影响行数。

    允许更新的字段白名单；updated_at 自动刷新。
    """
    allowed = ('name', 'protocol', 'host', 'port', 'auth_username',
               'auth_password_enc', 'region', 'profile_tags', 'usage_tags',
               'weight', 'max_concurrent', 'enabled')
    sets, vals = [], []
    for key in allowed:
        if key in fields and fields[key] is not None:
            sets.append('%s = ?' % key)
            vals.append(fields[key])
    if not sets:
        return 0
    sets.append('updated_at = NOW()')
    vals.append(channel_id)
    sql = 'UPDATE proxy_channels SET %s WHERE id = ?' % ', '.join(sets)
    with get_net_proxy_db() as conn:
        return conn.execute(sql, tuple(vals)).rowcount


def delete_channel(channel_id: int) -> int:
    """删除通道。返回受影响行数。"""
    with get_net_proxy_db() as conn:
        return conn.execute(
            'DELETE FROM proxy_channels WHERE id = ?', (channel_id,)).rowcount


# ═══════════════════════════════════════════════════════════════════════════
# 规则 CRUD（proxy_rules）
# ═══════════════════════════════════════════════════════════════════════════

_RULE_COLS = ('id, priority, target_pattern, caller, region, profile_tags,'
              ' usage_tags, action, note, enabled, created_at, updated_at')


def list_rules(enabled_only: bool = False):
    """列出规则，按 priority 升序（RuleEngine L2 命中顺序）。"""
    sql = 'SELECT %s FROM proxy_rules' % _RULE_COLS
    if enabled_only:
        sql += ' WHERE enabled = TRUE'
    sql += ' ORDER BY priority ASC, id ASC'
    with get_net_proxy_db() as conn:
        return conn.execute(sql).fetchall()


def get_rule(rule_id: int):
    """按 id 取单条规则，不存在返回 None。"""
    with get_net_proxy_db() as conn:
        return conn.execute(
            'SELECT %s FROM proxy_rules WHERE id = ?' % _RULE_COLS,
            (rule_id,),
        ).fetchone()


def create_rule(priority: int, target_pattern: str, action: str, **fields):
    """新建规则，返回新行 id。"""
    cols = ['priority', 'target_pattern', 'action']
    vals = [priority, target_pattern, action]
    for key in ('caller', 'region', 'profile_tags', 'usage_tags', 'note', 'enabled'):
        if key in fields and fields[key] is not None:
            cols.append(key)
            vals.append(fields[key])
    placeholders = ', '.join(['?'] * len(cols))
    sql = 'INSERT INTO proxy_rules (%s) VALUES (%s) RETURNING id' % (
        ', '.join(cols), placeholders)
    with get_net_proxy_db() as conn:
        return conn.execute(sql, tuple(vals)).fetchone()['id']


def update_rule(rule_id: int, **fields):
    """局部更新规则。返回受影响行数。"""
    allowed = ('priority', 'target_pattern', 'caller', 'region', 'profile_tags',
               'usage_tags', 'action', 'note', 'enabled')
    sets, vals = [], []
    for key in allowed:
        if key in fields and fields[key] is not None:
            sets.append('%s = ?' % key)
            vals.append(fields[key])
    if not sets:
        return 0
    sets.append('updated_at = NOW()')
    vals.append(rule_id)
    sql = 'UPDATE proxy_rules SET %s WHERE id = ?' % ', '.join(sets)
    with get_net_proxy_db() as conn:
        return conn.execute(sql, tuple(vals)).rowcount


def delete_rule(rule_id: int) -> int:
    """删除规则。返回受影响行数。"""
    with get_net_proxy_db() as conn:
        return conn.execute(
            'DELETE FROM proxy_rules WHERE id = ?', (rule_id,)).rowcount


# ═══════════════════════════════════════════════════════════════════════════
# 探活与计分（proxy_probe_log + proxy_channels 运行态）
# ═══════════════════════════════════════════════════════════════════════════

def list_channels_for_probe():
    """探活 job 取候选：全部 enabled 通道（含已熔断的，探活可使其恢复）。"""
    with get_net_proxy_db() as conn:
        return conn.execute(
            'SELECT id, name, protocol, host, port, auth_username,'
            ' auth_password_enc FROM proxy_channels WHERE enabled = TRUE'
            ' ORDER BY id ASC'
        ).fetchall()


def record_probe(channel_id: int, ok: bool, latency_ms=None, detail: str = ''):
    """写一条探活日志，并刷新通道的 last_probe_at / last_latency_ms。

    两条语句均为单行操作，无读-改-写，多 worker 并发安全。
    """
    with get_net_proxy_db() as conn:
        conn.execute(
            'INSERT INTO proxy_probe_log (channel_id, ok, latency_ms, detail)'
            ' VALUES (?, ?, ?, ?)',
            (channel_id, bool(ok), latency_ms, detail or ''),
        )
        conn.execute(
            'UPDATE proxy_channels'
            '   SET last_probe_at = NOW(),'
            '       last_latency_ms = COALESCE(?, last_latency_ms)'
            ' WHERE id = ?',
            (latency_ms, channel_id),
        )


def record_channel_result(channel_id: int, ok: bool, fuse_threshold: int = 3,
                          fuse_cooldown_minutes: int = 10):
    """**原子单行 UPDATE** 更新通道健康计分（§7.6）。

    - 失败：consecutive_failures + 1；**首次**达 fuse_threshold 时置
      fused_until = now() + fuse_cooldown_minutes；
    - 成功：consecutive_failures = 0，fused_until = NULL。

    无读-改-写，多 worker 并发安全（竞争最坏是计数多加/复位，无害）。

    熔断口径（对齐 §7.6「达 fuse_threshold 置 fused_until」的「达」= 触发点）：
    仅当 `fused_until IS NULL`（尚未熔断）且计数跨过阈值时才设置，
    **不重复顺延** —— 否则持续失败会把冷却期无限后推、通道永不恢复。
    `fused_until <= NOW()`（冷却已过期）同样视为未熔断，可再次触发。
    """
    with get_net_proxy_db() as conn:
        if ok:
            conn.execute(
                'UPDATE proxy_channels'
                '   SET consecutive_failures = 0, fused_until = NULL, updated_at = NOW()'
                ' WHERE id = ?',
                (channel_id,),
            )
        else:
            conn.execute(
                'UPDATE proxy_channels'
                '   SET consecutive_failures = consecutive_failures + 1,'
                '       fused_until = CASE'
                '         WHEN consecutive_failures + 1 >= ?'
                '          AND (fused_until IS NULL OR fused_until <= NOW())'
                "         THEN NOW() + (? * INTERVAL '1 minute')"
                '         ELSE fused_until END,'
                '       updated_at = NOW()'
                ' WHERE id = ?',
                (int(fuse_threshold), int(fuse_cooldown_minutes), channel_id),
            )


def pick_healthy_channels(usage_tags=None):
    """候选 = enabled 且未熔断（fused_until IS NULL OR <= now()）的通道。

    usage_tags 非空时按 LIKE 做「JSON 文本包含」判定 ——
    **禁止**用 jsonb ? 运算符（§18.6.2）。加权随机在调用方（fuse.py）完成。
    """
    sql = ('SELECT id, name, protocol, host, port, auth_username,'
           ' auth_password_enc, weight, usage_tags'
           ' FROM proxy_channels'
           ' WHERE enabled = TRUE'
           '   AND (fused_until IS NULL OR fused_until <= NOW())')
    params = []
    if usage_tags:
        # usage_tags 存 JSON 数组文本（如 ["stock","ai"]），逐标签 LIKE 匹配。
        # 注意：模式里的双引号需经 % 字面量转义（%%"..."%%）—— 直接写 %"..."
        # 会被 str.__mod__ 当成格式化序列而抛
        # `ValueError: unsupported format character '"'`。
        for tag in usage_tags:
            sql += ' AND usage_tags LIKE ?'
            params.append('%%"%s"%%' % tag)
    sql += ' ORDER BY id ASC'
    with get_net_proxy_db() as conn:
        if params:
            return conn.execute(sql, tuple(params)).fetchall()
        return conn.execute(sql).fetchall()


# ═══════════════════════════════════════════════════════════════════════════
# 审计（proxy_request_log）
# ═══════════════════════════════════════════════════════════════════════════

def write_request_log(caller: str, target_host: str, scheme: str, action: str,
                      channel_id=None, status_code=None, latency_ms=None,
                      bytes_up: int = 0, bytes_down: int = 0, error: str = ''):
    """写一条出站审计。

    脱敏口径（§6.3）：仅记 target_host，**不记**完整 URL / query / 凭据。
    """
    with get_net_proxy_db() as conn:
        return conn.execute(
            'INSERT INTO proxy_request_log'
            ' (caller, target_host, scheme, action, channel_id, status_code,'
            '  latency_ms, bytes_up, bytes_down, error)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (caller or '', target_host or '', scheme or '', action or '',
             channel_id, status_code, latency_ms, int(bytes_up or 0),
             int(bytes_down or 0), error or ''),
        ).rowcount


def list_request_log(limit: int = 100, offset: int = 0, caller: str = None,
                     channel_id: int = None):
    """分页查询出站审计，按时间倒序。"""
    sql = ('SELECT id, ts, caller, target_host, scheme, channel_id, action,'
           ' status_code, latency_ms, bytes_up, bytes_down, error'
           ' FROM proxy_request_log WHERE 1=1')
    params = []
    if caller:
        sql += ' AND caller = ?'
        params.append(caller)
    if channel_id is not None:
        sql += ' AND channel_id = ?'
        params.append(channel_id)
    sql += ' ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?'
    params.extend([int(limit), int(offset)])
    with get_net_proxy_db() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def list_probe_log(limit: int = 100, offset: int = 0, channel_id: int = None):
    """分页查询探活日志，按时间倒序（供 GET /admin/logs?kind=probe 消费）。

    注意：`proxy_probe_log.channel_id` 无外键，删除通道后本表会残留孤儿行
    （已在方案评审中报告，修复方式待拍板）。此处 LEFT JOIN 通道名，孤儿行
    的 channel_name 为 NULL，不影响读取。
    """
    sql = ('SELECT p.id, p.ts, p.channel_id, c.name AS channel_name, p.ok,'
           ' p.latency_ms, p.detail'
           ' FROM proxy_probe_log p'
           ' LEFT JOIN proxy_channels c ON c.id = p.channel_id'
           ' WHERE 1=1')
    params = []
    if channel_id is not None:
        sql += ' AND p.channel_id = ?'
        params.append(int(channel_id))
    sql += ' ORDER BY p.ts DESC, p.id DESC LIMIT ? OFFSET ?'
    params.extend([int(limit), int(offset)])
    with get_net_proxy_db() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def request_stats(hours: int = 24):
    """近 N 小时出站统计（供 Dashboard / 巡检 / admin status 消费）。

    success_rate：status_code < 500 且 error 为空计成功；
    成功率在无请求时返回 100.0（无请求不代表通道不健康）。
    direct_ratio：action='DIRECT' 占比（%），无请求时为 0.0 ——
    口径为「实际直连」，不含 DENY（DENY 被拦截，未产生任何出站）。
    """
    with get_net_proxy_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN (status_code IS NULL OR status_code < 500)"
            "           AND COALESCE(error, '') = '' THEN 1 ELSE 0 END) AS ok,"
            " SUM(CASE WHEN action = 'DIRECT' THEN 1 ELSE 0 END) AS direct,"
            " AVG(latency_ms) AS avg_latency"
            " FROM proxy_request_log"
            " WHERE ts >= NOW() - (? * INTERVAL '1 hour')",
            (int(hours),),
        ).fetchone()
    total = int((row or {}).get('total') or 0)
    ok = int((row or {}).get('ok') or 0)
    direct = int((row or {}).get('direct') or 0)
    avg_latency = (row or {}).get('avg_latency')
    return {
        'total': total,
        'ok': ok,
        'direct': direct,
        'success_rate': round(ok * 100.0 / total, 1) if total > 0 else 100.0,
        'direct_ratio': round(direct * 100.0 / total, 1) if total > 0 else 0.0,
        'avg_latency_ms': int(avg_latency) if avg_latency is not None else None,
    }


def fused_channel_count() -> int:
    """当前熔断中的通道数（供巡检 net_proxy.fused_channels 消费）。"""
    with get_net_proxy_db() as conn:
        row = conn.execute(
            'SELECT COUNT(*) AS n FROM proxy_channels'
            ' WHERE enabled = TRUE AND fused_until IS NOT NULL AND fused_until > NOW()'
        ).fetchone()
    return int((row or {}).get('n') or 0)


def cleanup_logs(retention_days: dict = None) -> dict:
    """按保留期清理日志表（§7.8 job2）。

    Args:
        retention_days: {'proxy_request_log': 30, 'proxy_probe_log': 7}
                        缺省用 LOG_TABLES 默认值 30 / 7。

    Returns:
        dict: {表名: 删除行数}
    """
    defaults = {'proxy_request_log': 30, 'proxy_probe_log': 7}
    conf = dict(defaults)
    if retention_days:
        conf.update({k: v for k, v in retention_days.items() if v})
    deleted = {}
    with get_net_proxy_db() as conn:
        for table, days in conf.items():
            try:
                n = conn.execute(
                    "DELETE FROM %s WHERE ts < NOW() - (? * INTERVAL '1 day')" % table,
                    (int(days),),
                ).rowcount
                deleted[table] = int(n or 0)
            except Exception:
                deleted[table] = 0
    return deleted
