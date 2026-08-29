#!/usr/bin/env python3
"""
License Server — 远程 License 验证 + 生成服务
=================================================
独立子服务，可部署为:
  - 独立 Flask 服务（python license_server/app.py）
  - 挂载到主站作为蓝图

部署方式由环境变量控制:
  SERVER_MODE=standalone  /  SERVER_MODE=blueprint

D3 决策: 独立子服务，与主站逻辑隔离。
"""

import os
import json
import time
import hashlib
import hmac
import base64
import secrets
import psycopg2
import psycopg2.extras
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, List

try:
    from flask import Blueprint, Flask, jsonify, request
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False
    Blueprint = type('Blueprint', (), {'__init__(': lambda s, *a, **kw: None})

# ── 配置 ──────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv(')LICENSE_DATA_DIR', os.path.join(BASE_DIR, 'data'))
DB_PATH = os.path.join(DATA_DIR, 'license_server.db')
SECRET_KEY = os.getenv('LICENSE_SERVER_SECRET')
if not SECRET_KEY:
    raise RuntimeError(
        "LICENSE_SERVER_SECRET environment variable is required. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
TOKEN_TTL_HOURS = int(os.getenv('LICENSE_TOKEN_TTL_HOURS', '8760'))  # 默认 1 年


# ── 数据库 ────────────────────────────────────────────────────────────

class _LSDb:
    """轻量封装，保持 conn.execute(sql, params) 接口兼容"""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if params is not None:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        return cursor

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _get_db() -> _LSDb:
    """获取 PG 数据库连接（线程安全）"""
    conn = psycopg2.connect(
        host=os.environ.get('PG_HOST', 'localhost'),
        port=os.environ.get('PG_PORT', '5432'),
        dbname=os.environ.get('PG_DATABASE', 'license_server'),
        user=os.environ.get('PG_USER', 'postgres'),
        password=os.environ.get('PG_PASSWORD', ''),
    )
    return _LSDb(conn)


def _init_db():
    """初始化表结构"""
    os.makedirs(DATA_DIR, exist_ok=True)
    db = _get_db()
    # 多语句 DDL 用 raw cursor 执行
    raw_conn = db._conn
    cur = raw_conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS license_keys (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            license_key     TEXT NOT NULL UNIQUE,
            plugin_id       TEXT NOT NULL,
            license_type    TEXT NOT NULL DEFAULT 'onetime'
                            CHECK(license_type IN ('free','onetime','sub','trial')),
            customer_email  TEXT DEFAULT '',
            customer_name   TEXT DEFAULT '',
            max_sites       BIGINT NOT NULL DEFAULT 3,
            expires_at      TEXT,
            trial_days      BIGINT DEFAULT 0,
            enabled         BIGINT NOT NULL DEFAULT 1,
            created_at      TEXT DEFAULT NOW(),
            updated_at      TEXT DEFAULT NOW(),
            metadata        TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_lk_plugin ON license_keys(plugin_id);
        CREATE INDEX IF NOT EXISTS idx_lk_key ON license_keys(license_key);

        CREATE TABLE IF NOT EXISTS license_activations (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            license_key     TEXT NOT NULL,
            plugin_id       TEXT NOT NULL,
            site_id         TEXT NOT NULL,
            site_name       TEXT DEFAULT '',
            ip_address      TEXT DEFAULT '',
            activated_at    TEXT DEFAULT NOW(),
            last_seen_at    TEXT DEFAULT NOW(),
            deactivated_at  TEXT,
            status          TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active','deactivated')),
            UNIQUE(license_key, site_id)
        );
        CREATE INDEX IF NOT EXISTS idx_la_license ON license_activations(license_key);

        CREATE TABLE IF NOT EXISTS purchase_orders (
            id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            order_no        TEXT NOT NULL UNIQUE,
            plugin_id       TEXT NOT NULL,
            license_key     TEXT NOT NULL,
            amount_fen      BIGINT NOT NULL,
            channel         TEXT DEFAULT 'alipay',
            status          TEXT DEFAULT 'paid'
                            CHECK(status IN ('paid','refunded','cancelled')),
            customer_email  TEXT DEFAULT '',
            paid_at         TEXT DEFAULT NOW(),
            created_at      TEXT DEFAULT NOW()
        );
    """)
    raw_conn.commit()
    cur.close()
    db.close()


# ── License 核心逻辑 ──────────────────────────────────────────────────

def _hash_license_key(raw: str) -> str:
    """生成 License Key"""
    h = hashlib.sha256(f'{raw}:{SECRET_KEY}'.encode()).hexdigest()[:24].upper()
    return '-'.join(h[i:i+4] for i in range(0, 24, 4))


def _sign_token(payload: dict) -> str:
    """生成离线验证 token（JWT-like，HMAC-SHA256）"""
    header = base64.urlsafe_b64encode(json.dumps({'alg': 'HS256', 'typ': 'LIC'}).encode()).rstrip(b'=').decode()
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).rstrip(b'=').decode()
    sig = hmac.new(SECRET_KEY.encode(), f'{header}.{body}'.encode(), hashlib.sha256).hexdigest()[:32]
    return f'{header}.{body}.{sig}'


def _verify_token(token: str) -> Optional[dict]:
    """验证离线 token"""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        header, body, sig = parts
        expected_sig = hmac.new(SECRET_KEY.encode(), f'{header}.{body}'.encode(), hashlib.sha256).hexdigest()[:32]
        if sig != expected_sig:
            return None
        return json.loads(base64.urlsafe_b64decode(body + '=='))
    except Exception:
        return None


# ── License Service ───────────────────────────────────────────────────

class LicenseService:
    """License 服务端核心逻辑"""

    def __init__(self):
        _init_db()
        self._lock = threading.Lock()

    # ── 生成 License ──────────────────────────────────────────────────

    def generate(self, plugin_id: str, license_type: str = 'onetime',
                 customer_email: str = '', customer_name: str = '',
                 max_sites: int = 3, expires_days: int = 365,
                 trial_days: int = 0) -> dict:
        """生成新 License Key

        Returns:
            {'success': bool, 'license_key': str, 'expires_at': str, ...}
        """
        raw = f'{plugin_id}:{secrets.token_hex(16)}:{time.time()}'
        license_key = _hash_license_key(raw)
        expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat() if expires_days > 0 else None

        with self._lock:
            conn = _get_db()
            try:
                conn.execute("""
                    INSERT INTO license_keys
                        (license_key, plugin_id, license_type, customer_email,
                         customer_name, max_sites, expires_at, trial_days)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (license_key, plugin_id, license_type, customer_email,
                      customer_name, max_sites, expires_at, trial_days))
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                return {'success': False, 'error': 'Duplicate license_key'}
            finally:
                conn.close()

        return {
            'success': True,
            'license_key': license_key,
            'plugin_id': plugin_id,
            'license_type': license_type,
            'expires_at': expires_at,
            'max_sites': max_sites,
        }

    # ── 激活 License ──────────────────────────────────────────────────

    def activate(self, license_key: str, plugin_id: str,
                 site_id: str, site_name: str = '',
                 ip_address: str = '') -> dict:
        """激活 License，生成离线 token"""
        with self._lock:
            conn = _get_db()
            try:
                # 查找 License
                lk_row = conn.execute(
                    "SELECT * FROM license_keys WHERE license_key=%s AND enabled=1",
                    (license_key,)
                ).fetchone()
                if not lk_row:
                    return {'success': False, 'error': 'Invalid license_key'}

                lk = dict(lk_row)

                # 检查插件匹配
                if lk['plugin_id'] != plugin_id:
                    return {'success': False, 'error': 'Plugin mismatch'}

                # 检查过期
                if lk['expires_at']:
                    try:
                        expires = datetime.fromisoformat(lk['expires_at'])
                        if datetime.now() > expires:
                            return {'success': False, 'error': 'License expired'}
                    except ValueError:
                        pass

                # 检查站点数
                active_count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM license_activations WHERE license_key=%s AND status='active'",
                    (license_key,)
                ).fetchone()['cnt']

                # 检查该站点是否已激活
                existing = conn.execute(
                    "SELECT * FROM license_activations WHERE license_key=%s AND site_id=%s",
                    (license_key, site_id)
                ).fetchone()

                if existing:
                    # 重新激活已停用的站点
                    if existing['status'] == 'deactivated':
                        conn.execute(
                            "UPDATE license_activations SET status='active', last_seen_at=NOW() WHERE id=%s",
                            (existing['id'],)
                        )
                    else:
                        # 已激活则更新最后活跃时间
                        conn.execute(
                            "UPDATE license_activations SET last_seen_at=NOW() WHERE id=%s",
                            (existing['id'],)
                        )
                elif active_count >= lk['max_sites']:
                    return {'success': False, 'error': f'Max sites ({lk["max_sites"]}) reached'}
                else:
                    # 新激活
                    conn.execute("""
                        INSERT INTO license_activations
                            (license_key, plugin_id, site_id, site_name, ip_address)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (license_key, plugin_id, site_id, site_name, ip_address))

                # 生成离线 token
                expires_at = lk.get('expires_at') or (datetime.now() + timedelta(days=365)).isoformat()
                token = _sign_token({
                    'p': plugin_id,
                    'k': license_key[-8:],
                    's': site_id,
                    'e': expires_at,
                    'v': 1,
                    'iat': int(time.time()),
                })

                conn.commit()
                return {
                    'success': True,
                    'license_key': license_key,
                    'expires_at': expires_at,
                    'offline_token': token,
                    'max_sites': lk['max_sites'],
                    'active_sites': min(active_count + 1, lk['max_sites']),
                }
            finally:
                conn.close()

    # ── 验证 License ──────────────────────────────────────────────────

    def validate(self, license_key: str, plugin_id: str,
                 site_id: str = '') -> dict:
        """验证 License 有效性"""
        conn = _get_db()
        try:
            lk = conn.execute(
                "SELECT * FROM license_keys WHERE license_key=%s AND enabled=1",
                (license_key,)
            ).fetchone()
            if not lk:
                return {'valid': False, 'error': 'Invalid license_key'}

            lk = dict(lk)

            if lk['plugin_id'] != plugin_id:
                return {'valid': False, 'error': 'Plugin mismatch'}

            expires_at = lk.get('expires_at')
            if expires_at:
                try:
                    expires = datetime.fromisoformat(expires_at)
                    if datetime.now() > expires:
                        return {'valid': False, 'error': 'Expired', 'expires_at': expires_at}
                except ValueError:
                    pass

            # 更新激活记录的最后活跃时间
            if site_id:
                conn.execute(
                    "UPDATE license_activations SET last_seen_at=NOW() WHERE license_key=%s AND site_id=%s",
                    (license_key, site_id)
                )
                conn.commit()

            return {
                'valid': True,
                'plugin_id': plugin_id,
                'license_type': lk['license_type'],
                'expires_at': expires_at or '',
                'max_sites': lk['max_sites'],
            }
        finally:
            conn.close()

    # ── 反激活 ────────────────────────────────────────────────────────

    def deactivate(self, license_key: str, plugin_id: str,
                   site_id: str) -> dict:
        """反激活指定站点的 License"""
        conn = _get_db()
        try:
            conn.execute("""
                UPDATE license_activations SET
                    status='deactivated', deactivated_at=NOW()
                WHERE license_key=%s AND plugin_id=%s AND site_id=%s
            """, (license_key, plugin_id, site_id))
            conn.commit()
            return {'success': True}
        finally:
            conn.close()

    # ── 查询 ──────────────────────────────────────────────────────────

    def list_licenses(self, plugin_id: str = '') -> List[dict]:
        conn = _get_db()
        try:
            if plugin_id:
                rows = conn.execute(
                    "SELECT * FROM license_keys WHERE plugin_id=%s ORDER BY created_at DESC",
                    (plugin_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM license_keys ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_activations(self, license_key: str) -> List[dict]:
        conn = _get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM license_activations WHERE license_key=%s ORDER BY activated_at DESC",
                (license_key,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ── Flask Blueprint ───────────────────────────────────────────────────

bp = None
app = None

if HAS_FLASK:
    bp = Blueprint('license_server', __name__, url_prefix='/api/v1')
    svc = LicenseService()

    @bp.route('/health', methods=['GET'])
    def health():
        return jsonify({'status': 'ok', 'service': 'license-server', 'version': '1.0'})

    @bp.route('/validate', methods=['POST'])
    def api_validate():
        data = request.json or {}
        license_key = data.get('license_key', '')
        plugin_id = data.get('plugin_id', '')
        site_id = data.get('site_id', '')
        if not license_key or not plugin_id:
            return jsonify({'success': False, 'error': 'license_key and plugin_id required'}), 400
        result = svc.validate(license_key, plugin_id, site_id)
        return jsonify({'success': result['valid'], 'data': result})

    @bp.route('/activate', methods=['POST'])
    def api_activate():
        data = request.json or {}
        license_key = data.get('license_key', '')
        plugin_id = data.get('plugin_id', '')
        site_id = data.get('site_id', '')
        site_name = data.get('site_name', '')
        ip_address = data.get('ip_address', '')
        customer_email = data.get('customer_email', '')
        if not license_key or not plugin_id or not site_id:
            return jsonify({'success': False, 'error': 'license_key, plugin_id, site_id required'}), 400
        result = svc.activate(license_key, plugin_id, site_id, site_name, ip_address)
        result['email'] = customer_email
        return jsonify({'success': result.get('success', False), 'data': result})

    @bp.route('/deactivate', methods=['POST'])
    def api_deactivate():
        data = request.json or {}
        license_key = data.get('license_key', '')
        plugin_id = data.get('plugin_id', '')
        site_id = data.get('site_id', '')
        if not license_key or not plugin_id or not site_id:
            return jsonify({'success': False, 'error': 'license_key, plugin_id, site_id required'}), 400
        result = svc.deactivate(license_key, plugin_id, site_id)
        return jsonify({'success': result['success'], 'data': result})

    @bp.route('/generate', methods=['POST'])
    def api_generate():
        data = request.json or {}
        plugin_id = data.get('plugin_id', '')
        license_type = data.get('license_type', 'onetime')
        customer_email = data.get('customer_email', '')
        customer_name = data.get('customer_name', '')
        max_sites = int(data.get('max_sites', 3))
        expires_days = int(data.get('expires_days', 365))
        trial_days = int(data.get('trial_days', 0))
        if not plugin_id:
            return jsonify({'success': False, 'error': 'plugin_id required'}), 400
        result = svc.generate(plugin_id, license_type, customer_email,
                              customer_name, max_sites, expires_days, trial_days)
        return jsonify(result)

    @bp.route('/licenses', methods=['GET'])
    def api_list_licenses():
        plugin_id = request.args.get('plugin_id', '')
        licenses = svc.list_licenses(plugin_id)
        return jsonify({'success': True, 'data': {'plugins': licenses}})

    @bp.route('/plugins/<identifier>/download', methods=['GET'])
    def api_download(identifier: str):
        return jsonify({'success': True, 'data': {
            'download_url': f'/api/v1/plugins/{identifier}/package',
            'version': 'latest',
        }})

    def create_app() -> Flask:
        """创建独立 Flask 应用"""
        app = Flask(__name__)
        app.register_blueprint(bp, url_prefix='/api/v1')
        return app


# ── 独立启动入口 ──────────────────────────────────────────────────────

def main():
    if not HAS_FLASK:
        print('Error: Flask is required for standalone mode')
        return

    _init_db()
    port = int(os.getenv('PORT', 8089))
    # VR-SEC-002: debug 默认关闭（仅 FLASK_DEBUG=1 显式开启）；host 默认仅本机回环
    debug = os.getenv('FLASK_DEBUG', '').strip() == '1'
    host = os.getenv('HOST', '127.0.0.1')
    app = create_app()
    print(f'[LicenseServer] Starting on {host}:{port} (debug={debug})')
    print(f'[LicenseServer] API: http://localhost:{port}/api/v1')
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    main()
