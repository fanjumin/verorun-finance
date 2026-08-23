#!/usr/bin/env python3
"""
Vault — Flask API Routes (Phase 1 extended)

API Endpoints:
    Page routes:
        GET   /admin/vault/                    — Dashboard page
        GET   /admin/vault/backups             — Backup list page
        GET   /admin/vault/restore             — Restore wizard page
        GET   /admin/vault/schedules           — Schedule management page
        GET   /admin/vault/storage             — Storage config page
        GET   /admin/vault/settings            — Settings page
        GET   /admin/vault/audit               — Audit log page

    Backup API:
        POST   /admin/vault/api/create              — Trigger backup (legacy, DEPRECATED)
        POST   /admin/vault/api/backup/create       — Trigger backup (new)
        GET    /admin/vault/api/list                — List backups (legacy, DEPRECATED)
        GET    /admin/vault/api/backup/list         — List backups with search/pagination
        GET    /admin/vault/api/backup/detail/<label> — Backup detail + content preview
        GET    /admin/vault/api/download/<label>    — Download backup (legacy)
        GET    /admin/vault/api/backup/download/<label> — Download backup (new)
        DELETE /admin/vault/api/delete/<label>      — Delete backup (legacy)
        DELETE /admin/vault/api/backup/delete/<label> — Delete backup (new)
        DELETE /admin/vault/api/cleanup             — Cleanup old backups

    Health API:
        GET    /admin/vault/api/health              — System health check

    Audit API:
        GET    /admin/vault/api/audit               — Query audit logs

    Schedule API:
        GET    /admin/vault/api/schedule/list       — List schedules
        POST   /admin/vault/api/schedule/create     — Create schedule
        PUT    /admin/vault/api/schedule/<id>       — Update schedule
        DELETE /admin/vault/api/schedule/<id>       — Delete schedule

    Restore API:
        POST   /admin/vault/api/restore             — Execute restore
        POST   /admin/vault/api/restore/preview     — Preview backup contents
"""

import os
import sys
import json
import glob
import shutil
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, jsonify, render_template, send_file, request, session, redirect

from .services.utils import safe_backup_path, safe_join

vault_bp = Blueprint('vault', __name__, url_prefix='/admin/vault',
                     template_folder='templates')

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
BACKUP_DIR = os.path.join(BASE_DIR, 'data', 'vault')


# ══════════════════════════════════════════════════════════════
# CSRF 防护（双重提交 Cookie 模式，与项目 ali_api 插件一致）
# ══════════════════════════════════════════════════════════════

def _generate_csrf_token() -> str:
    """生成 CSRF Token。"""
    return secrets.token_urlsafe(32)


def _validate_csrf() -> bool:
    """验证 CSRF Token（双重提交 Cookie 模式）。

    GET/HEAD/OPTIONS 放行；内部定时任务（X-Internal-Secret 匹配）放行；
    其余状态变更请求需 X-CSRF-Token 头与 csrf_token Cookie 一致。
    """
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return True

    internal_secret = os.environ.get('HEALTH_SECRET', '')
    if internal_secret and request.headers.get('X-Internal-Secret') == internal_secret:
        return True

    header_token = request.headers.get('X-CSRF-Token', '')
    cookie_token = request.cookies.get('csrf_token', '')
    if not header_token or not cookie_token:
        return False
    return secrets.compare_digest(header_token, cookie_token)


# ══════════════════════════════════════════════════════════════
# 权限检查（对齐 plugin.json permissions 声明）
# ══════════════════════════════════════════════════════════════

PERMISSIONS = {
    'vault.read': 'view backups',
    'vault.write': 'create/delete backups',
    'vault.download': 'download backups',
    'vault.admin': 'manage schedules/storage/settings',
}


def _check_permission(permission: str) -> bool:
    """检查当前用户是否具备指定权限。

    后台所有端点均要求 is_admin（与系统管理后台一致）；
    vault.admin 额外要求 role 为 super_admin / admin。
    """
    payload = getattr(request, 'vault_user', None)
    if not payload or not payload.get('is_admin'):
        return False
    if permission in ('vault.read', 'vault.write', 'vault.download'):
        return True
    if permission == 'vault.admin':
        return payload.get('role') in ('super_admin', 'admin')
    return False


def require_permission(permission: str):
    """装饰器：要求指定权限，否则 403。"""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not _check_permission(permission):
                return jsonify({'success': False, 'error': 'Insufficient permissions'}), 403
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ══════════════════════════════════════════════════════════════
# 速率限制（轻量内存滑动窗口，无外部依赖）
# ══════════════════════════════════════════════════════════════

_RATE_LIMIT_WINDOW = 60          # 窗口：60 秒
_RATE_LIMIT_MAX = 30             # 窗口内最大请求数
_RATE_LIMIT_KEYS = {}            # key -> 请求时间戳列表
_RATE_LIMIT_LAST_CLEAN = time.time()


def _rate_limited(f):
    """装饰器：按 IP + 端点滑动窗口限流，超限返回 429。"""
    @wraps(f)
    def wrapped(*args, **kwargs):
        global _RATE_LIMIT_LAST_CLEAN
        now = time.time()

        # 周期性清理过期记录，防止内存膨胀
        if now - _RATE_LIMIT_LAST_CLEAN > 300:
            _RATE_LIMIT_LAST_CLEAN = now
            cutoff = now - _RATE_LIMIT_WINDOW
            for k in [k for k, v in _RATE_LIMIT_KEYS.items() if not v or v[-1] < cutoff]:
                _RATE_LIMIT_KEYS.pop(k, None)

        key = (request.remote_addr or 'unknown', request.path)
        hits = [t for t in _RATE_LIMIT_KEYS.get(key, []) if t >= now - _RATE_LIMIT_WINDOW]
        if len(hits) >= _RATE_LIMIT_MAX:
            return jsonify({'success': False, 'error': 'Too many requests, please try later'}), 429
        hits.append(now)
        _RATE_LIMIT_KEYS[key] = hits
        return f(*args, **kwargs)
    return wrapped


def _err(e) -> str:
    """记录异常到日志并返回通用错误消息，避免向前端泄露内部路径/堆栈。"""
    try:
        import traceback
        print(f'[Vault] Error: {e}')
        traceback.print_exc()
    except Exception:
        pass
    return 'Internal server error'


@vault_bp.after_request
def _set_security_headers(resp):
    """统一设置 CSRF Cookie（双重提交）与安全响应头。"""
    # 首次访问时种下 CSRF Cookie（HttpOnly=False 以便 JS 读取，SameSite=Lax）
    # path='/' 确保后续所有 /admin/vault/* API 请求都能携带该 Cookie
    if not request.cookies.get('csrf_token'):
        try:
            resp.set_cookie('csrf_token', _generate_csrf_token(),
                            max_age=3600, httponly=False,
                            samesite='Lax', secure=request.is_secure,
                            path='/')
        except Exception:
            pass
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    resp.headers['X-XSS-Protection'] = '1; mode=block'
    resp.headers['Referrer-Policy'] = 'same-origin'
    return resp


# Static file routes (explicit, more reliable than Blueprint static_url_path)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')


@vault_bp.before_request
def _vault_ensure_schema():
    """Idempotently ensure vault_* tables exist before handling any vault request."""
    try:
        from .services.utils import ensure_schema
        ensure_schema()
    except Exception as e:
        print('[Vault] schema ensure skipped on request: %s' % e)


# ══════════════════════════════════════════════════════════════
# Auth Decorator
# ══════════════════════════════════════════════════════════════

def _require_vault_auth(f):
    """Decorator: require admin login for API access.
    
    - 仅接受 HttpOnly Cookie（sso_token）或 Authorization Bearer 头，禁止 URL 参数传 token
    - 校验 CSRF（状态变更方法）
    - 放行内部定时任务（X-Internal-Secret 匹配 HEALTH_SECRET）
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        # 内部定时任务（orchestrator cron_jobs）放行，避免破坏定时备份
        internal_secret = os.environ.get('HEALTH_SECRET', '')
        if internal_secret and request.headers.get('X-Internal-Secret') == internal_secret:
            # 注入内部特权身份，供 require_permission 放行
            request.vault_user = {'is_admin': True, 'role': 'super_admin', 'internal': True}
            return f(*args, **kwargs)

        token = request.cookies.get('sso_token')
        if not token:
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:]

        if not token:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or \
               request.content_type == 'application/json':
                return jsonify({'success': False, 'error': 'Authentication required'}), 401
            return redirect('/admin/login')

        try:
            from services.jwt_service import validate_token
            payload = validate_token(token)
            if not payload or not payload.get('is_admin'):
                raise ValueError('Invalid admin token')
        except Exception:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or \
               request.content_type == 'application/json':
                return jsonify({'success': False, 'error': 'Authentication required'}), 401
            return redirect('/admin/login')

        # CSRF 防护（状态变更方法）
        if not _validate_csrf():
            return jsonify({'success': False, 'error': 'CSRF validation failed'}), 403

        request.vault_user = payload
        return f(*args, **kwargs)
    return wrapped


@vault_bp.route('/static/<path:filename>')
@_require_vault_auth
def vault_static(filename):
    """Serve vault static files (CSS, JS). Requires admin auth, path-traversal safe."""
    try:
        filepath = safe_join(STATIC_DIR, filename)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid path'}), 400
    if not os.path.isfile(filepath):
        return jsonify({'success': False, 'error': 'Not found'}), 404
    return send_file(filepath)


def _get_backup_dir():
    """Get absolute backup directory path."""
    return BACKUP_DIR


def _list_backup_archives():
    """List all backup archives sorted by time (newest first)."""
    backup_dir = _get_backup_dir()
    archives = []
    for f in sorted(glob.glob(os.path.join(backup_dir, 'vault_*.tar.gz')), reverse=True):
        fname = os.path.basename(f)
        label = fname.replace('.tar.gz', '')
        stat = os.stat(f)
        archives.append({
            'label': label,
            'filename': fname,
            'size_mb': round(stat.st_size / (1024 * 1024), 1),
            'backup_type': 'full',
            'status': 'success',
            'created_at': datetime.utcfromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
        })
    return archives


# ══════════════════════════════════════════════════════════════
# Page Routes
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/')
@_require_vault_auth
def dashboard():
    """Render vault dashboard page."""
    return render_template('vault.html')


@vault_bp.route('/backups')
@_require_vault_auth
def backups_page():
    return render_template('vault.html')


@vault_bp.route('/restore')
@_require_vault_auth
def restore_page():
    return render_template('vault_restore.html')


@vault_bp.route('/schedules')
@_require_vault_auth
def schedules_page():
    return render_template('vault_schedules.html')


@vault_bp.route('/storage')
@_require_vault_auth
def storage_page():
    return render_template('vault_storage.html')


@vault_bp.route('/settings')
@_require_vault_auth
def settings_page():
    return render_template('vault_settings.html')


# ══════════════════════════════════════════════════════════════
# Settings API — 持久化插件配置（加密敏感字段）
# ══════════════════════════════════════════════════════════════

def _load_plugin_config() -> dict:
    """从 plugin_registry 读取 vault 插件配置。"""
    from plugins._base.db import get_raw_connection
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute("SELECT config FROM plugin_registry WHERE identifier = 'vault'")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and row[0]:
        return json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return {}


def _save_plugin_config(cfg: dict) -> bool:
    """将配置写入 plugin_registry（存在则更新，不存在则插入）。"""
    from plugins._base.db import get_raw_connection
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE plugin_registry SET config = %s, updated_at = NOW() "
        "WHERE identifier = 'vault'",
        (json.dumps(cfg, ensure_ascii=False),),
    )
    if cur.rowcount == 0:
        cur.execute(
            "INSERT INTO plugin_registry (identifier, config, status, created_at, updated_at) "
            "VALUES ('vault', %s, 'active', NOW(), NOW())",
            (json.dumps(cfg, ensure_ascii=False),),
        )
    conn.commit()
    cur.close()
    conn.close()
    return True


@vault_bp.route('/api/settings', methods=['GET'])
@_require_vault_auth
@require_permission('vault.admin')
def api_get_settings():
    """获取插件设置（敏感字段脱敏返回）。"""
    try:
        cfg = _load_plugin_config()
        # 通知渠道中的敏感字段脱敏
        notify = cfg.get('notifications', {})
        for key in ('email', 'webhook', 'feishu', 'dingtalk'):
            if key in notify and isinstance(notify[key], dict):
                from .services.utils import mask_config_secrets
                notify[key] = mask_config_secrets(notify[key])
        cfg['notifications'] = notify
        return jsonify({'success': True, 'config': cfg})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/settings', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.admin')
def api_save_settings():
    """保存插件设置（敏感字段加密后入库）。"""
    try:
        data = request.get_json(silent=True) or {}
        cfg = _load_plugin_config()

        # 白名单合并，避免覆盖未知字段
        for section in ('encryption', 'retention', 'notifications', 'storage'):
            if section in data and isinstance(data[section], dict):
                cfg[section] = data[section]

        # 通知渠道敏感字段加密
        notify = cfg.get('notifications', {})
        for key in ('email', 'webhook', 'feishu', 'dingtalk'):
            if key in notify and isinstance(notify[key], dict):
                from .services.utils import encrypt_config_secrets
                notify[key] = encrypt_config_secrets(notify[key])
        cfg['notifications'] = notify

        _save_plugin_config(cfg)

        try:
            from .services.audit import log_audit
            log_audit('settings.update', 'plugin', 'vault', {'sections': list(data.keys())})
        except Exception:
            pass

        return jsonify({'success': True, 'message': 'Settings saved'})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/audit')
@_require_vault_auth
def audit_page():
    return render_template('vault_audit.html')


# ══════════════════════════════════════════════════════════════
# Backup API — Legacy (backward compatible)
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/create', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_create_backup_legacy():
    """Trigger a full backup (legacy endpoint)."""
    return _handle_backup_create()


@vault_bp.route('/api/list', methods=['GET'])
@_require_vault_auth
@require_permission('vault.read')
def api_list_backups_legacy():
    """List all backup archives (legacy endpoint)."""
    try:
        archives = _list_backup_archives()
        return jsonify({'success': True, 'backups': archives, 'total': len(archives)})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/download/<label>', methods=['GET'])
@_require_vault_auth
@require_permission('vault.download')
def api_download_backup_legacy(label):
    """Download a backup archive (legacy endpoint)."""
    return _handle_backup_download(label)


@vault_bp.route('/api/delete/<label>', methods=['DELETE'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_delete_backup_legacy(label):
    """Delete a backup archive (legacy endpoint)."""
    return _handle_backup_delete(label)


@vault_bp.route('/api/cleanup', methods=['DELETE'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_cleanup_backups():
    """Cleanup backups older than configured keep_days."""
    try:
        keep_days = 30
        try:
            from plugins._base.db import get_raw_connection
            conn = get_raw_connection()
            cur = conn.cursor()
            cur.execute("SELECT config FROM plugin_registry WHERE identifier = 'vault'")
            row = cur.fetchone()
            if row and row[0]:
                cfg = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                keep_days = int(cfg.get('keep_days', 30))
            cur.close()
            conn.close()
        except Exception:
            pass

        cutoff = datetime.utcnow() - timedelta(days=keep_days)
        backup_dir = _get_backup_dir()
        deleted = 0
        for f in glob.glob(os.path.join(backup_dir, 'vault_*.tar.gz')):
            mtime = datetime.utcfromtimestamp(os.stat(f).st_mtime)
            if mtime < cutoff:
                os.remove(f)
                deleted += 1

        return jsonify({'success': True, 'deleted': deleted, 'keep_days': keep_days})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


# ══════════════════════════════════════════════════════════════
# Backup API — New (Phase 1)
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/backup/create', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_create_backup():
    """Create backup (supports full / incremental / differential)."""
    return _handle_backup_create()


@vault_bp.route('/api/backup/list', methods=['GET'])
@_require_vault_auth
@require_permission('vault.read')
def api_list_backups():
    """List backups with search, filtering, and pagination."""
    try:
        search = request.args.get('search', '').strip().lower()
        backup_type = request.args.get('type', '').strip()
        status = request.args.get('status', '').strip()
        page = max(1, int(request.args.get('page', 1)))
        per_page = max(1, min(100, int(request.args.get('per_page', 20))))

        archives = _list_backup_archives()

        # Apply filters
        if search:
            archives = [a for a in archives if search in a['label'].lower()]
        if backup_type:
            archives = [a for a in archives if a.get('backup_type') == backup_type]
        if status:
            archives = [a for a in archives if a.get('status') == status]

        total = len(archives)
        start = (page - 1) * per_page
        end = start + per_page
        items = archives[start:end]

        return jsonify({
            'success': True,
            'backups': items,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': max(1, (total + per_page - 1) // per_page),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/backup/detail/<label>', methods=['GET'])
@_require_vault_auth
@require_permission('vault.read')
def api_backup_detail(label):
    """Get backup detail with content preview."""
    try:
        archive_path = safe_backup_path(label)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid backup label'}), 400
    if not os.path.isfile(archive_path):
        return jsonify({'success': False, 'error': 'Backup not found'}), 404

    import tarfile
    stat = os.stat(archive_path)
    content_preview = []
    try:
        with tarfile.open(archive_path, 'r:gz') as tar:
            for member in tar.getmembers()[:50]:
                content_preview.append({
                    'name': member.name,
                    'size': member.size,
                    'type': 'dir' if member.isdir() else 'file',
                })
    except Exception:
        pass

    return jsonify({
        'success': True,
        'backup': {
            'label': label,
            'size_mb': round(stat.st_size / (1024 * 1024), 1),
            'created_at': datetime.utcfromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            'content_preview': content_preview,
        },
    })


@vault_bp.route('/api/backup/download/<label>', methods=['GET'])
@_require_vault_auth
@require_permission('vault.download')
def api_download_backup(label):
    """Download a backup archive."""
    return _handle_backup_download(label)


@vault_bp.route('/api/backup/delete/<label>', methods=['DELETE'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_delete_backup(label):
    """Delete a backup (requires confirmation)."""
    return _handle_backup_delete(label)


# ══════════════════════════════════════════════════════════════
# Health API
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/health', methods=['GET'])
@_require_vault_auth
@require_permission('vault.read')
def api_health_check():
    """System health: backup status, storage usage, last backup time."""
    try:
        archives = _list_backup_archives()

        # Disk usage
        backup_dir = _get_backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        total_bytes, used_bytes, free_bytes = shutil.disk_usage(backup_dir)

        # Health score
        score = 100
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        recent_backups = [a for a in archives if a['created_at'] >= today_start.strftime('%Y-%m-%d %H:%M:%S')]
        if not recent_backups:
            score -= 30

        if total_bytes > 0 and (free_bytes / total_bytes) < 0.1:
            score -= 20

        failed = [a for a in archives if a.get('status') == 'failed']
        if len(failed) > 3:
            score -= 15

        # Next schedule (check vault_schedules first, then cron_jobs)
        next_schedule = None
        try:
            from .services.utils import get_vault_conn
            conn = get_vault_conn()
            cur = conn.cursor()
            # Check vault_schedules table
            cur.execute("""
                SELECT MIN(next_run_at) FROM vault_schedules
                WHERE enabled = TRUE AND next_run_at > NOW()
            """)
            row = cur.fetchone()
            if row and row[0]:
                next_schedule = row[0].strftime('%Y-%m-%d %H:%M:%S')
            else:
                # Fallback: check cron_jobs table
                cur.execute("""
                    SELECT MIN(next_run_at) FROM cron_jobs
                    WHERE name LIKE 'Vault%' AND is_active = 1
                """)
                row = cur.fetchone()
                if row and row[0]:
                    next_schedule = row[0].strftime('%Y-%m-%d %H:%M:%S')
            cur.close()
            conn.close()
        except Exception:
            pass

        return jsonify({
            'success': True,
            'health': {
                'score': max(score, 0),
                'total_backups': len(archives),
                'last_backup': archives[0]['created_at'] if archives else None,
                'next_schedule': next_schedule,
                'storage': {
                    'total_gb': round(total_bytes / (1024 ** 3), 1),
                    'free_gb': round(free_bytes / (1024 ** 3), 1),
                    'used_percent': round((total_bytes - free_bytes) / total_bytes * 100, 1) if total_bytes > 0 else 0,
                },
            },
        })
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


# ══════════════════════════════════════════════════════════════
# Audit API
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/audit', methods=['GET'])
@_require_vault_auth
@require_permission('vault.admin')
def api_audit_logs():
    """Query audit logs."""
    try:
        action = request.args.get('action', '')
        resource_type = request.args.get('resource_type', '')
        operator = request.args.get('operator', '')
        limit = max(1, min(500, int(request.args.get('limit', 100))))
        offset = max(0, int(request.args.get('offset', 0)))

        from .services.audit import get_audit_logs
        logs = get_audit_logs(
            action=action or None,
            resource_type=resource_type or None,
            operator=operator or None,
            limit=limit,
            offset=offset,
        )

        # Convert datetime objects to strings
        for log in logs:
            if isinstance(log.get('created_at'), datetime):
                log['created_at'] = log['created_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'logs': logs, 'count': len(logs)})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


# ══════════════════════════════════════════════════════════════
# Schedule API (CRUD)
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/schedule/list', methods=['GET'])
@_require_vault_auth
@require_permission('vault.admin')
def api_list_schedules():
    """List all backup schedules."""
    try:
        from .services.scheduler import VaultScheduler
        scheduler = VaultScheduler()
        schedules = scheduler.get_all_schedules()
        for s in schedules:
            for key in ('last_run_at', 'next_run_at', 'created_at'):
                if isinstance(s.get(key), datetime):
                    s[key] = s[key].strftime('%Y-%m-%d %H:%M:%S')
        return jsonify({'success': True, 'schedules': schedules})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/schedule/create', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.admin')
def api_create_schedule():
    """Create a new backup schedule."""
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        cron_expr = data.get('cron_expression', '').strip()
        if not name or not cron_expr:
            return jsonify({'success': False, 'error': 'name and cron_expression are required'}), 400

        from .services.scheduler import VaultScheduler
        scheduler = VaultScheduler()
        result = scheduler.create_schedule(
            name=name,
            cron_expr=cron_expr,
            backup_type=data.get('backup_type', 'full'),
            retention_days=data.get('retention_days'),
            retention_count=data.get('retention_count'),
            storage_targets=data.get('storage_targets'),
            backup_window=data.get('backup_window'),
            pre_hook=data.get('pre_hook'),
            post_hook=data.get('post_hook'),
        )

        try:
            from .services.audit import log_audit
            log_audit('schedule.create', 'schedule', str(result.get('id', '')), {'name': name})
        except Exception:
            pass

        return jsonify({'success': True, 'schedule': result})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/schedule/<int:schedule_id>', methods=['PUT', 'DELETE'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.admin')
def api_schedule_crud(schedule_id):
    """Update or delete a schedule."""
    try:
        from .services.scheduler import VaultScheduler
        scheduler = VaultScheduler()

        if request.method == 'DELETE':
            result = scheduler.delete_schedule(schedule_id)
            try:
                from .services.audit import log_audit
                log_audit('schedule.delete', 'schedule', str(schedule_id))
            except Exception:
                pass
            return jsonify(result)

        # PUT
        data = request.get_json(silent=True) or {}
        result = scheduler.update_schedule(schedule_id, **data)
        try:
            from .services.audit import log_audit
            log_audit('schedule.update', 'schedule', str(schedule_id))
        except Exception:
            pass
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/schedule/<int:schedule_id>/toggle', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.admin')
def api_toggle_schedule(schedule_id):
    """Enable or disable a schedule."""
    try:
        data = request.get_json(silent=True) or {}
        enabled = data.get('enabled', True)

        from .services.scheduler import VaultScheduler
        scheduler = VaultScheduler()
        result = scheduler.toggle_schedule(schedule_id, enabled)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


# ══════════════════════════════════════════════════════════════
# Storage API (CRUD)
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/storage/list', methods=['GET'])
@_require_vault_auth
@require_permission('vault.admin')
def api_list_storage():
    """List all storage targets."""
    try:
        from .services.storage.base import StorageRouter
        router = StorageRouter()
        targets = router.list_targets()
        return jsonify({'success': True, 'targets': targets})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/storage/create', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.admin')
def api_create_storage():
    """Create a new storage target."""
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        stype = data.get('storage_type', '').strip()
        config = data.get('config', {})
        if not name or not stype:
            return jsonify({'success': False, 'error': 'name and storage_type are required'}), 400

        from .services.storage.base import StorageRouter
        router = StorageRouter()
        result = router.create_target(
            name=name,
            storage_type=stype,
            config=config,
            is_default=data.get('is_default', False),
        )

        try:
            from .services.audit import log_audit
            log_audit('storage.create', 'storage', str(result.get('id', '')), {'name': name, 'type': stype})
        except Exception:
            pass

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/storage/<int:target_id>', methods=['PUT', 'DELETE'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.admin')
def api_storage_crud(target_id):
    """Update or delete a storage target."""
    try:
        from .services.storage.base import StorageRouter
        router = StorageRouter()

        if request.method == 'DELETE':
            result = router.delete_target(target_id)
            try:
                from .services.audit import log_audit
                log_audit('storage.delete', 'storage', str(target_id))
            except Exception:
                pass
            return jsonify(result)

        # PUT
        data = request.get_json(silent=True) or {}
        result = router.update_target(target_id, **data)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/storage/<int:target_id>/test', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.admin')
def api_test_storage(target_id):
    """Test connection for a storage target."""
    try:
        from .services.storage.base import StorageRouter
        router = StorageRouter()
        result = router.test_target(target_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


# ══════════════════════════════════════════════════════════════
# Compliance API
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/compliance/report', methods=['GET'])
@_require_vault_auth
@require_permission('vault.admin')
def api_compliance_report():
    """Generate compliance report."""
    try:
        from .services.compliance import ComplianceReporter
        report = ComplianceReporter.generate_report()
        return jsonify({'success': True, 'report': report})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


# ══════════════════════════════════════════════════════════════
# Rotation & Tiering API
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/storage/rotate', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.admin')
def api_rotate_upload():
    """Trigger 3-2-1 rotation upload for latest backup."""
    try:
        import os as _os, glob as _glob
        base_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', '..')
        backup_dir = _os.path.join(base_dir, 'data', 'vault')
        archives = sorted(_glob.glob(_os.path.join(backup_dir, 'vault_*.tar.gz')),
                          key=_os.path.getmtime, reverse=True)
        if not archives:
            return jsonify({'success': False, 'error': 'No backups to rotate'}), 400

        from .services.storage.base import StorageRouter
        router = StorageRouter()
        latest = archives[0]
        obj_name = _os.path.basename(latest)
        result = router.rotate_upload(latest, obj_name)
        return jsonify({'success': True, 'rotation': result})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/storage/tier/report', methods=['GET'])
@_require_vault_auth
@require_permission('vault.admin')
def api_tier_report():
    """Get storage tier distribution report."""
    try:
        from .services.storage.base import StorageRouter
        router = StorageRouter()
        return jsonify({'success': True, 'tiers': router.tier_report()})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


# ══════════════════════════════════════════════════════════════
# Signature & Verification API
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/backup/sign/<label>', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_sign_backup(label):
    """Sign a backup with HMAC-SHA256 for tamper-proofing."""
    try:
        secret = os.environ.get('VAULT_SIGNING_KEY', '')
        if not secret:
            return jsonify({'success': False, 'error': 'VAULT_SIGNING_KEY not set'}), 400

        try:
            filepath = safe_backup_path(label)
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid backup label'}), 400
        if not os.path.isfile(filepath):
            return jsonify({'success': False, 'error': 'Backup not found'}), 404

        from .services.validator import VaultValidator
        sig_path = VaultValidator.sign_file(filepath, secret)
        return jsonify({'success': True, 'signature': os.path.basename(sig_path)})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/backup/verify/<label>', methods=['GET'])
@_require_vault_auth
@require_permission('vault.read')
def api_verify_backup(label):
    """Verify a backup's integrity and signature."""
    try:
        secret = os.environ.get('VAULT_SIGNING_KEY', '')
        if not secret:
            return jsonify({'success': False, 'error': 'VAULT_SIGNING_KEY not set'}), 400

        try:
            filepath = safe_backup_path(label)
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid backup label'}), 400
        if not os.path.isfile(filepath):
            return jsonify({'success': False, 'error': 'Backup not found'}), 404

        from .services.validator import VaultValidator
        result = VaultValidator.verify_file(filepath, secret)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


# ══════════════════════════════════════════════════════════════
# Restore API
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/restore/preview', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_restore_preview():
    """Preview backup contents before restore."""
    try:
        data = request.get_json(silent=True) or {}
        label = data.get('label', '')
        if not label:
            return jsonify({'success': False, 'error': 'Backup label is required'}), 400

        try:
            safe_backup_path(label)
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid backup label'}), 400

        from .services.restore_engine import RestoreEngine
        engine = RestoreEngine()
        result = engine.preview(label)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/restore', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_restore():
    """Execute restore with optional scope."""
    try:
        data = request.get_json(silent=True) or {}
        label = data.get('label', '')
        if not label:
            return jsonify({'success': False, 'error': 'Backup label is required'}), 400

        try:
            safe_backup_path(label)
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid backup label'}), 400

        scope = data.get('scope', {})
        target_db = data.get('target_db')
        target_host = data.get('target_host')

        from .services.restore_engine import RestoreEngine
        engine = RestoreEngine()
        result = engine.restore(label, scope=scope or None, target_db=target_db,
                                target_host=target_host)

        # Audit log
        try:
            from .services.audit import log_audit
            log_audit(
                action='restore.execute',
                resource_type='backup',
                resource_id=label,
                details={'scope': scope, 'success': result.get('success')},
            )
        except Exception:
            pass

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/restore/pitr', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_restore_pitr():
    """Point-in-time recovery to a specific timestamp."""
    try:
        data = request.get_json(silent=True) or {}
        target_time = data.get('target_time', '')
        if not target_time:
            return jsonify({'success': False, 'error': 'target_time is required (ISO format)'}), 400

        from .services.restore_engine import RestoreEngine
        engine = RestoreEngine()
        result = engine.restore_pitr(target_time)

        try:
            from .services.audit import log_audit
            log_audit(
                action='restore.pitr',
                resource_type='database',
                resource_id=target_time,
                details={'success': result.get('success')},
            )
        except Exception:
            pass

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


@vault_bp.route('/api/restore/drill', methods=['POST'])
@_require_vault_auth
@_rate_limited
@require_permission('vault.write')
def api_restore_drill():
    """Execute a restore drill — restore to sandbox, verify, cleanup."""
    try:
        data = request.get_json(silent=True) or {}
        label = data.get('label', '')
        if label:
            try:
                safe_backup_path(label)
            except ValueError:
                return jsonify({'success': False, 'error': 'Invalid backup label'}), 400

        from .services.restore_engine import RestoreEngine
        engine = RestoreEngine()
        result = engine.drill_restore(label or None)

        try:
            from .services.audit import log_audit
            log_audit(
                action='restore.drill',
                resource_type='backup',
                resource_id=label or 'latest',
                details={'success': result.get('success')},
            )
        except Exception:
            pass

        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


# ══════════════════════════════════════════════════════════════
# Trend API (Dashboard charts)
# ══════════════════════════════════════════════════════════════

@vault_bp.route('/api/trend', methods=['GET'])
@_require_vault_auth
@require_permission('vault.read')
def api_trend():
    """Return backup size trend data for dashboard charts."""
    try:
        from .services.utils import get_vault_conn
        conn = get_vault_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT label, backup_type, size_bytes, status,
                   COALESCE(completed_at, created_at) as ts
            FROM vault_backups
            WHERE status = 'success'
            ORDER BY ts DESC
            LIMIT 90
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        trend_data = []
        for row in reversed(rows):
            label, btype, size_bytes, status, ts = row
            if ts and size_bytes:
                trend_data.append({
                    'label': label,
                    'type': btype,
                    'size_mb': round(size_bytes / (1024 * 1024), 1),
                    'date': ts.strftime('%Y-%m-%d') if hasattr(ts, 'strftime') else str(ts)[:10],
                })

        return jsonify({'success': True, 'trend': trend_data})
    except Exception as e:
        # Fallback: compute from backup archives
        try:
            archives = _list_backup_archives()
            trend_data = [{
                'label': a['label'],
                'type': a.get('backup_type', 'full'),
                'size_mb': a['size_mb'],
                'date': a['created_at'][:10],
            } for a in reversed(archives[-90:])]
            return jsonify({'success': True, 'trend': trend_data})
        except Exception as e2:
            return jsonify({'success': False, 'error': _err(e2)}), 500


# ══════════════════════════════════════════════════════════════
# Internal handlers (shared by legacy and new endpoints)
# ══════════════════════════════════════════════════════════════

def _handle_backup_create():
    """Internal handler for backup creation with compress → encrypt → upload → notify pipeline."""
    try:
        from .services.backup_engine import create_full_backup
        result = create_full_backup()

        if result.get('archive') and result.get('success'):
            archive_path = result['archive']

            # 1. Compress
            try:
                from .services.compressor import VaultCompressor
                compressor = VaultCompressor(algorithm='gzip', level=6)
                archive_path = compressor.compress(archive_path)
                result['compressed_size_mb'] = round(os.path.getsize(archive_path) / (1024**2), 1)
            except Exception as e:
                print(f'[Vault] Compression skipped: {e}')

            # 2. Encrypt (if configured)
            try:
                from .services.encryptor import VaultEncryptor
                encryptor = VaultEncryptor()
                archive_path = encryptor.encrypt_stream(archive_path)
                result['encrypted'] = True
            except ValueError:
                pass  # encryption not configured
            except Exception as e:
                print(f'[Vault] Encryption skipped: {e}')

            # 3. Upload to remote storage
            try:
                from .services.uploader import upload_backup
                upload_result = upload_backup(result['archive'],
                                              os.path.basename(result['archive']))
                result['remote'] = upload_result
            except Exception as e:
                result['remote'] = {'uploaded': False, 'error': _err(e)}

            # 4. Notify
            try:
                from .services.notifier import VaultNotifier
                notifier = VaultNotifier()
                event = 'backup.success' if result.get('success') else 'backup.failed'
                notifier.send(
                    event=event,
                    message='Backup %s (%s MB)' % (result['label'], result.get('size_mb', 0)),
                    level='info' if result.get('success') else 'error',
                    details=result,
                )
            except Exception:
                pass

        # Write to vault_backups table
        try:
            from .services.utils import get_vault_conn
            conn = get_vault_conn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO vault_backups
                    (label, backup_type, status, size_bytes, checksum_sha256,
                     content_summary, started_at, completed_at, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                result['label'],
                result.get('backup_type', 'full'),
                'success' if result.get('success') else 'failed',
                int(result.get('size_mb', 0) * 1024 * 1024),
                result.get('checksum_sha256'),
                json.dumps({'files': len(result.get('files', []))}),
                datetime.utcnow(),
                datetime.utcnow(),
                session.get('user', {}).get('username', 'system'),
            ))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f'[Vault] Failed to write backup record to DB: {e}')

        # Audit log
        try:
            from .services.audit import log_audit
            log_audit(
                action='backup.full.create',
                resource_type='backup',
                resource_id=result.get('label', ''),
                details={
                    'type': 'full',
                    'size_mb': result.get('size_mb'),
                    'checksum': result.get('checksum_sha256'),
                },
            )
        except Exception:
            pass

        return jsonify(result), 200 if result.get('success') else 500
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500


def _handle_backup_download(label):
    """Internal handler for backup download."""
    try:
        filepath = safe_backup_path(label)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid backup label'}), 400
    if not os.path.isfile(filepath):
        return jsonify({'success': False, 'error': 'Backup not found'}), 404
    return send_file(filepath, as_attachment=True, download_name=f'{label}.tar.gz')


def _handle_backup_delete(label):
    """Internal handler for backup deletion. Confirmation via POST body."""
    data = request.get_json(silent=True) or {}
    if not data.get('confirm'):
        return jsonify({
            'success': False,
            'error': 'Confirmation required',
            'confirm_required': True,
            'message': 'Are you sure you want to delete backup "%s"? This action cannot be undone.' % label,
        }), 400

    try:
        filepath = safe_backup_path(label)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid backup label'}), 400
    if not os.path.isfile(filepath):
        return jsonify({'success': False, 'error': 'Backup not found'}), 404

    try:
        os.remove(filepath)
        # Audit log
        try:
            from .services.audit import log_audit
            log_audit('backup.delete', 'backup', label)
        except Exception:
            pass
        return jsonify({'success': True, 'message': f'Backup {label} deleted'})
    except Exception as e:
        return jsonify({'success': False, 'error': _err(e)}), 500
