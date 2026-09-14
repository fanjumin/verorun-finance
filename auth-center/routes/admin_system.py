# -*- coding: utf-8 -*-
"""Auto-generated split from admin.py"""
from .admin import admin_bp, _require_admin, _log, _cached_get
from i18n import _
from datetime import datetime, timedelta
from flask import Response, jsonify, request
from models import get_db
import os
import json

@admin_bp.route('/api-keys', methods=['GET'])
@_cached_get(ttl=3)
def api_key_list():
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)
    offset = (page - 1) * limit
    with get_db() as conn:
        total = conn.execute('SELECT COUNT(*) as c FROM api_keys').fetchone()
        rows = conn.execute("SELECT k.id, k.name, k.key_prefix, k.calls_today, k.calls_total, k.active, k.created_at, COALESCE(u.display_name, u.username, '') as user_name, u.id as user_id FROM api_keys k LEFT JOIN users u ON k.user_id=u.id ORDER BY k.created_at DESC LIMIT %s OFFSET %s", (limit, offset)).fetchall()
    return jsonify({'success': True, 'data': {'total': total['c'], 'page': page, 'limit': limit, 'keys': [dict(r) for r in rows]}})


@admin_bp.route('/api-keys/<int:kid>', methods=['DELETE'])
def revoke_key(kid):
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        # D-22: 存在性检查，不存在返回 404，避免"假成功"
        row = conn.execute('SELECT id FROM api_keys WHERE id=%s', (kid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Key does not exist')}), 404
        conn.execute('UPDATE api_keys SET active=0 WHERE id=%s', (kid,))
        conn.commit()
    _log(admin['user_id'], 'revoke_api_key', 'api_key', str(kid))
    return jsonify({'success': True, 'message': _('Key has been revoked')})

@admin_bp.route('/logs', methods=['GET'])
@_cached_get(ttl=3)
def admin_logs():
    admin, err = _require_admin()
    if err:
        return err
    limit = request.args.get('limit', 50, type=int)
    with get_db() as conn:
        rows = conn.execute('SELECT l.id, l.action, l.target_type, l.target_id, l.detail, l.ip_address, l.created_at, COALESCE(u.display_name, u.username) as admin_name FROM admin_logs l LEFT JOIN users u ON l.admin_id=u.id ORDER BY l.created_at DESC LIMIT %s', (limit,)).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})

@admin_bp.route('/agent-matrix', methods=['GET'])
def agent_matrix_list():
    admin, err = _require_admin()
    if err:
        return err
    type_filter = request.args.get('type', chr(39)+chr(39))
    with get_db() as conn:
        if type_filter:
            rows = conn.execute('SELECT * FROM agents WHERE type=%s ORDER BY type, id', (type_filter,)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM agents ORDER BY type, id').fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@admin_bp.route('/agent-matrix', methods=['POST'])
def agent_matrix_create():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    # D-23: 必填字段校验 —— alias 为 NOT NULL 核心字段，缺失/空值直接 400，防未知字段静默建空行
    alias = (data.get('alias') or '').strip()
    mission = (data.get('mission') or '').strip()
    prompt = (data.get('system_prompt') or '').strip()
    if not alias:
        return jsonify({'success': False, 'error': _('Alias is required')}), 400
    alias = alias[:12]
    mission = mission[:64]
    prompt = prompt[:3000]
    model_provider_id = data.get('provider_model_id')  # new field name
    if model_provider_id is None:
        model_provider_id = data.get('model_provider_id')  # backward compat
    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO agents (type, alias, mission, system_prompt, provider_model_id) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (data.get('type', 'child'), alias, mission, prompt, model_provider_id)
        ).fetchone()
        conn.commit()
        aid = row['id']
    _log(admin['user_id'], 'create_agent', 'agent', str(aid), alias)
    return jsonify({'success': True, 'message': _('Agent has been created'), 'id': aid})


@admin_bp.route('/agent-matrix/<int:aid>', methods=['PUT'])
def agent_matrix_update(aid):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    fields = []
    values = []
    for key in ['type', 'alias', 'mission', 'system_prompt', 'provider_model_id', 'is_active']:
        if key in data:
            fields.append(key + '=%s')
            values.append(data[key])
    if not fields:
        return jsonify({'success': False, 'error': _('No fields to update')}), 400
    fields.append("updated_at=NOW()")
    values.append(aid)
    with get_db() as conn:
        conn.execute('UPDATE agents SET ' + ','.join(fields) + ' WHERE id=%s', values)
        conn.commit()
    _log(admin['user_id'], 'update_agent', 'agent', str(aid))
    return jsonify({'success': True, 'message': _('Agent has been updated')})


@admin_bp.route('/agent-matrix/<int:aid>', methods=['DELETE'])
def agent_matrix_delete(aid):
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        conn.execute('DELETE FROM agents WHERE id=%s', (aid,))
        conn.commit()
    _log(admin['user_id'], 'delete_agent', 'agent', str(aid))
    return jsonify({'success': True, 'message': _('Agent has been deleted')})


@admin_bp.route('/agent-matrix/<int:aid>/test', methods=['POST'])
def agent_matrix_test(aid):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    query = data.get('query', chr(39)+chr(39))
    if not query:
        return jsonify({'success': False, 'error': _('Please enter a test message (cannot be empty)')}), 400
    with get_db() as conn:
        row = conn.execute('SELECT * FROM agents WHERE id=%s', (aid,)).fetchone()
    if not row:
        return jsonify({'success': False, 'error': _('Agent does not exist')}), 404
    from services.agent_engine import UniversalAgentEngine
    engine = UniversalAgentEngine(dict(row))
    result = engine.ask(query)
    return jsonify({'success': True, 'data': {'response': result}})



# ══════════════════════════════════════════════
# 子域名管理 API
# ══════════════════════════════════════════════

_PLAN_DOMAIN_LIMITS = {
    'deploy_basic': 20,
    'deploy_pro': 20,
    'deploy_enterprise': 20,
}

_NGINX_CONF_DIR = os.environ.get(
    'NGINX_SNIPPETS_DIR'
) or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'nginx-domains', 'sites-enabled'
)


def _resolve_cert_dir(full_domain):
    """审计 M2：按域名实查证书目录（full_domain → 主域 DEPLOY_DOMAIN → None）。
    修复原占位符 'your-domain.com-0001' 永不匹配真实证书的问题。"""
    candidates = [full_domain]
    main_domain = os.environ.get('DEPLOY_DOMAIN', '')
    if main_domain and main_domain != full_domain:
        candidates.append(main_domain)
    for d in candidates:
        p = os.path.join('/etc/letsencrypt/live', d)
        if os.path.isfile(os.path.join(p, 'fullchain.pem')) and os.path.isfile(os.path.join(p, 'privkey.pem')):
            return p
    return None


def _validate_service_port(port):
    """审计 D2：service_port 严格校验。
    None/空 → content 模式（不生成 nginx 配置）；
    否则必须为 1024-65535 整数，非法返回错误信息（调用方应拒绝请求）。"""
    if port is None or str(port).strip() == '':
        return None, None
    try:
        p = int(port)
    except (TypeError, ValueError):
        return None, _('service_port must be an integer')
    if not (1024 <= p <= 65535):
        return None, _('service_port must be between 1024 and 65535')
    return p, None


def _generate_domain_nginx_config(subdomain, full_domain, port):
    """生成本地 Nginx server block 配置文件"""
    # 审计 D2 兜底：生成配置前强制校验端口，非法直接拒绝（防路由层绕过）
    port, port_err = _validate_service_port(port)
    if port_err is not None:
        print(f'[Nginx Config Warning] invalid service_port: {port_err}', flush=True)
        return None
    if not port:
        return None
    os.makedirs(_NGINX_CONF_DIR, exist_ok=True)
    # 审计 M2：证书实查；无证书则纯 HTTP（不再生成指向不存在证书的 443 配置）
    cert_dir = _resolve_cert_dir(full_domain)
    if cert_dir:
        _ssl_listen = (
            f"    listen 443 ssl http2;\n"
            f"    ssl_certificate     {cert_dir}/fullchain.pem;\n"
            f"    ssl_certificate_key {cert_dir}/privkey.pem;\n"
            f"    ssl_protocols TLSv1.2 TLSv1.3;\n"
            f"    ssl_ciphers HIGH:!aNULL:!MD5;"
        )
        _http_redir = '    return 301 https://$host$request_uri;'
    else:
        _ssl_listen = '    # no certificate found — serving plain HTTP (审计 M2)'
        _http_redir = ''
    conf = f"""# Auto-generated site domain — {datetime.now().strftime('%Y-%m-%d %H:%M')}
# subdomain={subdomain}  port={port}

server {{
    listen 80;
    server_name {full_domain};
    {_http_redir}
    {_ssl_listen}

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;  # 审计 M1：XFF 覆盖为直连 IP，不追加客户端伪造值
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""
    filepath = os.path.join(_NGINX_CONF_DIR, f'{full_domain}.conf')
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(conf)
    return filepath


def _remove_domain_nginx_config(full_domain):
    """删除本地 Nginx 配置文件"""
    filepath = os.path.join(_NGINX_CONF_DIR, f'{full_domain}.conf')
    if os.path.exists(filepath):
        os.remove(filepath)
        return True
    return False


def _reload_nginx():
    """生产环境：reload Nginx 使配置生效"""
    if 'NGINX_SNIPPETS_DIR' not in os.environ:
        return  # 本地开发不执行
    import subprocess
    try:
        subprocess.run(['sudo', '/usr/sbin/nginx', '-s', 'reload'], check=True,
                       capture_output=True, timeout=10)
    except Exception as e:
        print(f'[Nginx Reload Warning] {e}', flush=True)


def _check_domain_quota(user_id):
    """检查用户是否还能添加子域名

    免费：3 域名（www/agent/platform 开箱即用）。
    开通 site_domains 插件订阅，或持有包含 site_domains 20 域名权益的
    Pro 版本包订阅（plugin_subscriptions.status='active'）→ 20 域名。
    """
    with get_db() as conn:
        sub = conn.execute(
            "SELECT plan_key FROM user_subscriptions WHERE user_id=%s AND status='active'",
            (user_id,)
        ).fetchone()
        if not sub:
            limit = 3  # 无订阅（免费）限额 3 个域名
        else:
            limit = _PLAN_DOMAIN_LIMITS.get(sub['plan_key'], 20)
        # 插件订阅权益：site_domains 订阅或版本包订阅 → 20 域名
        try:
            site_row = conn.execute(
                "SELECT 1 FROM plugin_subscriptions "
                "WHERE plugin_id='site_domains' AND status='active' LIMIT 1"
            ).fetchone()
            bundle_row = conn.execute(
                "SELECT 1 FROM plugin_subscriptions "
                "WHERE bundle_id<>'' AND bundle_id<>'site_domains' AND status='active' LIMIT 1"
            ).fetchone()
            if site_row or bundle_row:
                limit = max(limit, 20)
        except Exception as e:
            print(f'[DomainQuota] plugin subscription check failed: {e}', flush=True)
            # 查询失败会使当前事务进入 aborted 状态，必须回滚，否则后续查询
            # 全部抛 InFailedSqlTransaction，导致整个接口 500。
            try:
                conn.rollback()
            except Exception:
                pass
        used = conn.execute(
            "SELECT COUNT(*) as c FROM site_domains"
        ).fetchone()['c']
        allowed = max(limit - used, 0)
        return {
            'allowed': allowed,
            'used': used,
            'limit': limit,
            'can_add': allowed > 0,
        }


@admin_bp.route('/domains', methods=['GET'])
def admin_domains_page():
    admin, err = _require_admin()
    if err:
        return err
    return jsonify({'success': True, 'page': 'domains'})


@admin_bp.route('/api/domains', methods=['GET'])
def admin_list_domains():
    admin, err = _require_admin()
    if err:
        return err
    quota = _check_domain_quota(admin['user_id'])
    with get_db() as conn:
        rows = conn.execute(
            "SELECT sd.*, sc.name as site_name, sc.theme_color, sc.accent_color "
            "FROM site_domains sd "
            "JOIN site_configs sc ON sc.id = sd.site_config_id "
            "ORDER BY sd.sort_order, sd.id"
        ).fetchall()
    return jsonify({
        'success': True,
        'data': [dict(r) for r in rows],
        'quota': quota,
    })


@admin_bp.route('/api/domains', methods=['POST'])
def admin_create_domain():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    subdomain = data.get('subdomain', '').strip().lower()
    display_name = data.get('display_name', '').strip()
    template = data.get('template', 'default')
    # 审计 D2：service_port 写入前严格校验（1024-65535），非法拒绝
    service_port, port_err = _validate_service_port(data.get('service_port'))
    if port_err:
        return jsonify({'success': False, 'error': port_err}), 400

    if not subdomain or not display_name:
        return jsonify({'success': False, 'error': _('Subdomain and Display Name Cannot Be Empty')}), 400

    # 校验子域名格式（只允许字母数字连字符）
    import re
    if not re.match(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$', subdomain):
        return jsonify({'success': False, 'error': _('Subdomain Format Invalid: Only Letters, Numbers, and Hyphens Allowed')}), 400

    # 校验配额
    quota = _check_domain_quota(admin['user_id'])
    if not quota['can_add']:
        return jsonify({'success': False, 'error': f'Quota used up ({quota["used"]}/{quota["limit"]})'}), 400

    deploy_domain = os.environ.get('DEPLOY_DOMAIN', 'localhost')
    full_domain = f'{subdomain}.{deploy_domain}'

    with get_db() as conn:
        # 检查是否已存在
        exists = conn.execute(
            "SELECT id FROM site_domains WHERE full_domain=%s", (full_domain,)
        ).fetchone()
        if exists:
            return jsonify({'success': False, 'error': f'Subdomain {full_domain} Already Exists'}), 400

        conn.execute(
            "INSERT INTO site_domains (site_config_id, subdomain, full_domain, display_name, template, service_port) "
            "VALUES (1, %s, %s, %s, %s, %s)",
            (subdomain, full_domain, display_name, template, service_port)
        )
        conn.commit()

    # 独立服务 → 生成 Nginx 配置
    nginx_path = _generate_domain_nginx_config(subdomain, full_domain, service_port)
    _reload_nginx()

    _log(admin['user_id'], 'create_domain', detail=f'{full_domain} ({display_name}) port={service_port or "content"}')
    msg = f'Subdomain {full_domain} Created'
    if nginx_path:
        msg += f', Nginx configuration has been generated'
    return jsonify({'success': True, 'message': msg, 'nginx_config_path': nginx_path})


@admin_bp.route('/api/domains/<int:did>', methods=['PUT'])
def admin_update_domain(did):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    allowed = ['display_name', 'template', 'is_published', 'page_keys_json', 'sort_order', 'service_port']
    updates = {k: data[k] for k in allowed if k in data}
    if not updates:
        return jsonify({'success': False, 'error': _('No Valid Update Fields')}), 400

    # 审计 D2：service_port 写入前严格校验（1024-65535），非法拒绝
    if 'service_port' in updates:
        updates['service_port'], port_err = _validate_service_port(updates['service_port'])
        if port_err:
            return jsonify({'success': False, 'error': port_err}), 400

    # 先读取旧 full_domain
    with get_db() as conn:
        old_row = conn.execute("SELECT full_domain, subdomain FROM site_domains WHERE id=%s", (did,)).fetchone()

    sets = ', '.join(f'{k}=%s' for k in updates)
    vals = list(updates.values()) + [did]
    with get_db() as conn:
        conn.execute(
            f'UPDATE site_domains SET {sets}, updated_at=NOW() WHERE id=%s',
            vals
        )
        conn.commit()

    # 更新 Nginx 配置
    if old_row:
        old_domain = old_row['full_domain']
        subdomain = old_row['subdomain']
        new_port = updates.get('service_port')
        if new_port is not None:
            _generate_domain_nginx_config(subdomain, old_domain, new_port)
        else:
            _remove_domain_nginx_config(old_domain)
        _reload_nginx()

    _log(admin['user_id'], 'update_domain', detail=f'domain_id={did}')
    return jsonify({'success': True, 'message': _('Updated')})


@admin_bp.route('/api/domains/<int:did>', methods=['DELETE'])
def admin_delete_domain(did):
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute(
            "SELECT full_domain, service_port FROM site_domains WHERE id=%s", (did,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Subdomain Does Not Exist')}), 404
        full_domain = row['full_domain']
        conn.execute("DELETE FROM site_domains WHERE id=%s", (did,))
        conn.commit()
    # 删除 Nginx 配置文件
    _remove_domain_nginx_config(full_domain)
    _reload_nginx()
    _log(admin['user_id'], 'delete_domain', detail=full_domain)
    return jsonify({'success': True, 'message': f'{full_domain} has been deleted'})


@admin_bp.route('/api/domains/quota', methods=['GET'])
def admin_domain_quota():
    admin, err = _require_admin()
    if err:
        return err
    return jsonify({'success': True, 'data': _check_domain_quota(admin['user_id'])})


@admin_bp.route('/api/domains/<int:did>/nginx-config', methods=['GET'])
def admin_domain_nginx_config(did):
    """返回子域名对应的 Nginx 配置文本"""
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute(
            "SELECT full_domain, subdomain, service_port FROM site_domains WHERE id=%s", (did,)
        ).fetchone()
    if not row:
        return jsonify({'success': False, 'error': _('Subdomain Does Not Exist')}), 404
    if not row['service_port']:
        return jsonify({'success': False, 'error': _('Content site does not require Nginx configuration')}), 400
    config_path = os.path.join(_NGINX_CONF_DIR, f'{row["full_domain"]}.conf')
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            config_text = f.read()
    else:
        # 动态生成（文件不存在时）
        config_text = _generate_domain_nginx_config(
            row['subdomain'], row['full_domain'], row['service_port']
        )
        if config_text:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_text = f.read()
    return jsonify({
        'success': True,
        'data': {
            'full_domain': row['full_domain'],
            'service_port': row['service_port'],
            'config_text': config_text,
            'server_path': f'/etc/nginx/snippets/site-domains/{row["full_domain"]}.conf',
        }
    })
# ══════════════════════════════════════════════

@admin_bp.route('/notifications/templates', methods=['GET'])
def admin_notif_templates_list():
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM notification_templates ORDER BY sort_order, id'
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@admin_bp.route('/notifications/templates', methods=['POST'])
def admin_notif_templates_create():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    event_type = (data.get('event_type') or '').strip()
    title_tmpl = (data.get('title_template') or '').strip()
    content_tmpl = (data.get('content_template') or '').strip()
    link_url_tmpl = (data.get('link_url_template') or '').strip()
    ntype = data.get('type', 'system')
    if not event_type or not title_tmpl or not content_tmpl:
        return jsonify({'success': False, 'error': _('Event_type, title_template, content_template are required')}), 400
    with get_db() as conn:
        try:
            tid = conn.execute(
                'INSERT INTO notification_templates (event_type, title_template, content_template, link_url_template, type) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                (event_type, title_tmpl, content_tmpl, link_url_tmpl, ntype)
            ).fetchone()['id']
            conn.commit()
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
    _log(admin['user_id'], 'create_notif_template', detail=f'{event_type}')
    return jsonify({'success': True, 'id': tid})


@admin_bp.route('/notifications/templates/<int:tid>', methods=['PUT'])
def admin_notif_templates_update(tid):
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    fields = []
    vals = []
    for key in ('event_type', 'title_template', 'content_template', 'link_url_template', 'type', 'is_active'):
        if key in data:
            fields.append(f'{key}=%s')
            vals.append(data[key])
    if not fields:
        return jsonify({'success': False, 'error': _('No Update Fields')}), 400
    vals.append(tid)
    with get_db() as conn:
        conn.execute(f'UPDATE notification_templates SET {", ".join(fields)}, updated_at=NOW() WHERE id=%s', vals)
        conn.commit()
    _log(admin['user_id'], 'update_notif_template', detail=f'{tid}')
    return jsonify({'success': True})


@admin_bp.route('/notifications/templates/<int:tid>', methods=['DELETE'])
def admin_notif_templates_delete(tid):
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        # D-22: 存在性检查，不存在返回 404，避免"假成功"
        row = conn.execute('SELECT id FROM notification_templates WHERE id=%s', (tid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Notification template does not exist')}), 404
        conn.execute('DELETE FROM notification_templates WHERE id=%s', (tid,))
        conn.commit()
    _log(admin['user_id'], 'delete_notif_template', detail=f'{tid}')
    return jsonify({'success': True})


@admin_bp.route('/notifications/send', methods=['POST'])
def admin_notif_send():
    """手动推送通知。支持：全体用户 / 指定用户ID列表 / 用户类型筛选"""
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    target_type = data.get('target_type', 'all')  # all / user_ids
    user_ids = data.get('user_ids', [])
    title = (data.get('title') or '').strip()
    content = (data.get('content') or '').strip()
    link_url = (data.get('link_url') or '').strip()
    ntype = data.get('type', 'system')
    schedule_at = data.get('schedule_at', '')  # ISO timestamp or empty = immediate

    if not title:
        return jsonify({'success': False, 'error': _('Title cannot be empty')}), 400
    if not content:
        return jsonify({'success': False, 'error': _('Content cannot be empty')}), 400

    target_users = []
    with get_db() as conn:
        if target_type == 'all':
            rows = conn.execute('SELECT id FROM users WHERE active=1 ORDER BY id').fetchall()
            target_users = [r['id'] for r in rows]
        elif target_type == 'user_ids' and user_ids:
            target_users = [int(uid) for uid in user_ids]
        else:
            return jsonify({'success': False, 'error': _('Invalid target type')}), 400

    from services.notification_service import create_notification
    sent = 0
    errors = []
    for uid in target_users:
        try:
            nid = create_notification(uid, ntype, title, content, link_url)
            if nid:
                sent += 1
        except Exception as e:
            errors.append(str(e))

    _log(admin['user_id'], 'notif_send', detail=f'target={target_type} count={sent}')
    return jsonify({'success': True, 'sent': sent, 'total': len(target_users), 'errors': errors[:5]})


@admin_bp.route('/notifications/test', methods=['POST'])
def admin_notif_test():
    """发送测试通知给当前管理员"""
    admin, err = _require_admin()
    if err: return err
    from services.notification_service import create_notification
    nid = create_notification(
        admin['user_id'], 'system',
        _('This is a test notification'),
        _('The system is running normally. This is a test message to confirm the system is ready.'),
        link_url=''
    )
    if nid:
        return jsonify({'success': True, 'notification_id': nid})
# ══════════════════════════════════════════════

@admin_bp.route('/tickets', methods=['GET'])
def admin_tickets_list():
    """管理员查看工单列表，支持 %sstatus=open&type=complaint 多维筛选"""
    admin, err = _require_admin()
    if err: return err
    status = request.args.get('status', '').strip()
    ttype = request.args.get('type', '').strip()
    with get_db() as conn:
        # 构建查询条件
        where = []
        params = []
        if status:
            where.append("t.status=%s")
            params.append(status)
        if ttype and ttype in ("presale","aftersale","complaint","suggestion"):
            where.append("t.type=%s")
            params.append(ttype)
        where_clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f'SELECT t.*, u.username, u.phone FROM user_tickets t LEFT JOIN users u ON t.user_id=u.id {where_clause} ORDER BY CASE t.status WHEN \'open\' THEN 0 WHEN \'replied\' THEN 1 ELSE 2 END, t.updated_at DESC',
            tuple(params)
        ).fetchall() if params else conn.execute(
            f'SELECT t.*, u.username, u.phone FROM user_tickets t LEFT JOIN users u ON t.user_id=u.id {where_clause} ORDER BY CASE t.status WHEN \'open\' THEN 0 WHEN \'replied\' THEN 1 ELSE 2 END, t.updated_at DESC'
        ).fetchall()
        total = conn.execute('SELECT COUNT(*) as c FROM user_tickets').fetchone()['c']
        open_count = conn.execute('SELECT COUNT(*) as c FROM user_tickets WHERE status=\'open\'').fetchone()['c']
        replied_count = conn.execute('SELECT COUNT(*) as c FROM user_tickets WHERE status=\'replied\'').fetchone()['c']
        # 各类型计数
        cnt_presale = conn.execute("SELECT COUNT(*) as c FROM user_tickets WHERE type='presale'").fetchone()['c']
        cnt_aftersale = conn.execute("SELECT COUNT(*) as c FROM user_tickets WHERE type='aftersale'").fetchone()['c']
        cnt_complaint = conn.execute("SELECT COUNT(*) as c FROM user_tickets WHERE type='complaint'").fetchone()['c']
        cnt_suggestion = conn.execute("SELECT COUNT(*) as c FROM user_tickets WHERE type='suggestion'").fetchone()['c']
    return jsonify({
        'success': True, 'data': [dict(r) for r in rows],
        'total': total, 'open': open_count, 'replied': replied_count,
        'cnt_presale': cnt_presale, 'cnt_aftersale': cnt_aftersale,
        'cnt_complaint': cnt_complaint, 'cnt_suggestion': cnt_suggestion
    })


@admin_bp.route('/tickets/<int:tid>', methods=['PUT'])
def admin_tickets_update(tid):
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    action = data.get('action', 'reply')  # reply / close / reopen
    with get_db() as conn:
        if action == 'reply':
            reply = (data.get('admin_reply') or '').strip()
            if not reply:
                return jsonify({'success': False, 'error': _('Reply content cannot be empty')}), 400
            conn.execute(
                "UPDATE user_tickets SET admin_reply=%s, status='replied', replied_at=NOW(), updated_at=NOW() WHERE id=%s",
                (reply, tid)
            )
        elif action == 'close':
            conn.execute(
                "UPDATE user_tickets SET status='closed', updated_at=NOW() WHERE id=%s",
                (tid,)
            )
        elif action == 'reopen':
            conn.execute(
                "UPDATE user_tickets SET status='open', updated_at=NOW() WHERE id=%s",
                (tid,)
            )
        conn.commit()
    _log(admin['user_id'], 'ticket_update', detail=f'ticket={tid} action={action}')


# ══════════════════════════════════════════════
# 完成度奖励规则 CRUD
# ══════════════════════════════════════════════

@admin_bp.route('/reward-rules', methods=['GET'])
def admin_reward_rules_list():
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM reward_rules ORDER BY sort_order, id').fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@admin_bp.route('/reward-rules', methods=['POST'])
def admin_reward_rules_create():
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': _('Rule name cannot be empty')}), 400
    with get_db() as conn:
        rid = conn.execute(
            'INSERT INTO reward_rules (name, condition_key, condition_value, reward_type, reward_id, reward_name, sort_order, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (name, data.get('condition_key', ''), data.get('condition_value', ''),
             data.get('reward_type', 'coupon'), data.get('reward_id'), data.get('reward_name', ''),
             data.get('sort_order', 0), 1 if data.get('is_active', True) else 0)
        ).fetchone()['id']
    _log(admin['user_id'], 'create_reward_rule', detail=name)
    return jsonify({'success': True, 'data': {'id': rid}})


@admin_bp.route('/reward-rules/<int:rid>', methods=['PUT'])
def admin_reward_rules_update(rid):
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    allowed = ['name', 'condition_key', 'condition_value', 'reward_type', 'reward_id', 'reward_name', 'sort_order', 'is_active']
    updates = {}
    for k in allowed:
        if k in data:
            updates[k] = data[k]
    if not updates:
        return jsonify({'success': False, 'error': _('No fields to update')}), 400
    sets = ', '.join(f'{k}=%s' for k in updates.keys())
    vals = list(updates.values()) + [rid]
    with get_db() as conn:
        conn.execute(f'UPDATE reward_rules SET {sets} WHERE id=%s', vals)
        conn.commit()
    _log(admin['user_id'], 'update_reward_rule', detail=f'id={rid}')
    return jsonify({'success': True})


@admin_bp.route('/reward-rules/<int:rid>', methods=['DELETE'])
def admin_reward_rules_delete(rid):
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        conn.execute('DELETE FROM reward_rules WHERE id=%s', (rid,))
        conn.execute('DELETE FROM reward_claims WHERE rule_id=%s', (rid,))
        conn.commit()
    _log(admin['user_id'], 'delete_reward_rule', detail=f'id={rid}')
    return jsonify({'success': True})


@admin_bp.route('/reward-claims', methods=['GET'])
def admin_reward_claims_list():
    admin, err = _require_admin()
    if err: return err
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('pageSize', 50, type=int)
    offset = (page - 1) * page_size
    with get_db() as conn:
        rows = conn.execute("""
            SELECT rc.*, r.name AS rule_name, u.display_name AS user_name
            FROM reward_claims rc
            LEFT JOIN reward_rules r ON rc.rule_id = r.id
            LEFT JOIN users u ON rc.user_id = u.id
            ORDER BY rc.id DESC LIMIT %s OFFSET %s
        """, (page_size, offset)).fetchall()
        total = conn.execute('SELECT COUNT(*) as c FROM reward_claims').fetchone()['c']
    return jsonify({'success': True, 'data': [dict(r) for r in rows], 'total': total})

@admin_bp.route('/providers', methods=['GET'])
def list_providers():
    """列出所有提供商（含模型列表）"""
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        providers = [dict(r) for r in conn.execute(
            'SELECT * FROM providers ORDER BY id'
        ).fetchall()]
        for p in providers:
            p['models'] = [dict(r) for r in conn.execute(
                'SELECT * FROM provider_models WHERE provider_id=%s ORDER BY sort_order, id',
                (p['id'],)
            ).fetchall()]
    return jsonify({'success': True, 'data': providers})


@admin_bp.route('/providers/<int:pid>', methods=['PUT'])
def update_provider(pid):
    """更新提供商"""
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    with get_db() as conn:
        row = conn.execute('SELECT * FROM providers WHERE id=%s', (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Does not exist')}), 404
        name = (data.get('name') or row['name']).strip()
        desc = data.get('description', row['description'])
        is_active = data.get('is_active', row['is_active'])
        conn.execute(
            "UPDATE providers SET name=%s, description=%s, is_active=%s, updated_at=NOW() WHERE id=%s",
            (name, desc, int(is_active) if is_active is not None else 1, pid))
        conn.commit()
        _log(admin['user_id'], 'update', 'provider', str(pid), f'Update provider: {name}')
    return jsonify({'success': True})


# ── Provider Models CRUD ──

@admin_bp.route('/provider-models', methods=['GET'])
def list_provider_models():
    """列出所有模型（可按 provider_id 筛选）"""
    admin, err = _require_admin()
    if err: return err
    pid = request.args.get('provider_id')
    with get_db() as conn:
        if pid:
            rows = conn.execute(
                'SELECT pm.*, p.name as provider_name, p.slug as provider_slug FROM provider_models pm JOIN providers p ON p.id=pm.provider_id WHERE pm.provider_id=%s ORDER BY pm.sort_order, pm.id',
                (pid,)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT pm.*, p.name as provider_name, p.slug as provider_slug FROM provider_models pm JOIN providers p ON p.id=pm.provider_id ORDER BY p.id, pm.sort_order, pm.id'
            ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@admin_bp.route('/provider-models', methods=['POST'])
def create_provider_model():
    """新增模型"""
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    provider_id = data.get('provider_id')
    model_name = (data.get('model_name') or '').strip()
    endpoint_url = (data.get('endpoint_url') or '').strip()
    api_key_ref = (data.get('api_key_ref') or '').strip()
    capabilities = (data.get('capabilities') or 'text').strip()
    if not name or not provider_id:
        return jsonify({'success': False, 'error': _('Name and Provider cannot be empty')}), 400
    with get_db() as conn:
        mid = conn.execute(
            'INSERT INTO provider_models (provider_id, name, model_name, endpoint_url, api_key_ref, capabilities) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id',
            (provider_id, name, model_name, endpoint_url, api_key_ref, capabilities)).fetchone()['id']
        conn.commit()
        _log(admin['user_id'], 'create', 'provider_model', str(mid), f'Add Model: {name}')
    return jsonify({'success': True, 'data': {'id': mid}})


@admin_bp.route('/provider-models/<int:mid>', methods=['PUT'])
def update_provider_model(mid):
    """更新模型"""
    admin, err = _require_admin()
    if err: return err
    data = request.get_json(force=True) or {}
    with get_db() as conn:
        row = conn.execute('SELECT * FROM provider_models WHERE id=%s', (mid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Does not exist')}), 404
        name = (data.get('name') or row['name']).strip()
        provider_id = data.get('provider_id', row['provider_id'])
        model_name = data.get('model_name', row['model_name'])
        endpoint_url = data.get('endpoint_url', row['endpoint_url'])
        api_key_ref = data.get('api_key_ref', row['api_key_ref'])
        capabilities = data.get('capabilities', row['capabilities'])
        is_active = data.get('is_active', row['is_active'])
        sort_order = data.get('sort_order', row['sort_order'])
        conn.execute(
            '''UPDATE provider_models SET provider_id=%s, name=%s, model_name=%s, endpoint_url=%s,
               api_key_ref=%s, capabilities=%s, is_active=%s, sort_order=%s,
               updated_at=NOW() WHERE id=%s''',
            (provider_id, name, model_name, endpoint_url, api_key_ref, capabilities,
             int(is_active) if is_active is not None else 1, sort_order, mid))
        conn.commit()
        _log(admin['user_id'], 'update', 'provider_model', str(mid), f'Update model: {name}')
    return jsonify({'success': True})


@admin_bp.route('/provider-models/<int:mid>', methods=['DELETE'])
def delete_provider_model(mid):
    """删除模型"""
    admin, err = _require_admin()
    if err: return err
    with get_db() as conn:
        row = conn.execute('SELECT name FROM provider_models WHERE id=%s', (mid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Does not exist')}), 404
        conn.execute('DELETE FROM provider_models WHERE id=%s', (mid,))
        conn.commit()
        _log(admin['user_id'], 'delete', 'provider_model', str(mid), f'Delete Model: {row["name"]}')
        return jsonify({'success': True})

@admin_bp.route('/quota/stats', methods=['GET'])
def quota_stats():
    """API配额概览统计数据"""
    admin, err = _require_admin()
    if err:
        return err
    from models import TIERS
    with get_db() as conn:
        total_keys = conn.execute('SELECT COUNT(*) as c FROM api_keys').fetchone()['c']
        active_keys = conn.execute('SELECT COUNT(*) as c FROM api_keys WHERE active=1').fetchone()['c']
        today_calls = conn.execute("SELECT COALESCE(SUM(calls_today),0) as c FROM api_keys WHERE last_reset=CURRENT_DATE").fetchone()['c']
        total_calls = conn.execute('SELECT COALESCE(SUM(calls_total),0) as c FROM api_keys').fetchone()['c']
        user_tiers = conn.execute(
            "SELECT a.tier, COUNT(DISTINCT a.user_id) as count FROM app_authorizations a WHERE a.active=1 GROUP BY a.tier"
        ).fetchall()
    tier_breakdown = {}
    for t in ['free', 'standard', 'pro']:
        tier_breakdown[t] = {'name': TIERS.get(t, {}).get('name', t), 'daily_limit': TIERS.get(t, {}).get('daily_limit', 0), 'count': 0}
    for r in user_tiers:
        if r['tier'] in tier_breakdown:
            tier_breakdown[r['tier']]['count'] = r['count']
    return jsonify({'success': True, 'data': {
        'total_keys': total_keys, 'active_keys': active_keys,
        'today_calls': today_calls, 'total_calls': total_calls,
        'tier_breakdown': tier_breakdown
    }})


@admin_bp.route('/quota/users', methods=['GET'])
def quota_users():
    """查询所有用户的配额信息"""
    admin, err = _require_admin()
    if err:
        return err
    from models import TIERS
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)
    offset = (page - 1) * limit
    search = request.args.get('search', '').strip()

    with get_db() as conn:
        where = ''
        params = []
        if search:
            where = "WHERE (u.username LIKE %s OR u.display_name LIKE %s)"
            params = [f'%{search}%', f'%{search}%']
        total = conn.execute(f'SELECT COUNT(*) as c FROM users u {where}', params).fetchone()['c']
        rows = conn.execute(f"""
            SELECT u.id, u.username, u.display_name, u.created_at,
                   COALESCE(a.tier, 'free') as tier,
                   COALESCE(a.calls_today, 0) as calls_today,
                   COALESCE(a.calls_total, 0) as calls_total,
                   (SELECT COUNT(*) FROM api_keys k WHERE k.user_id=u.id AND k.active=1) as active_keys
            FROM users u
            LEFT JOIN app_authorizations a ON u.id=a.user_id AND a.active=1
            {where}
            ORDER BY u.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset]).fetchall()
        users = [dict(r) for r in rows]
        for u in users:
            tier_info = TIERS.get(u['tier'], TIERS['free'])
            u['daily_limit'] = tier_info['daily_limit']
            u['tier_name'] = tier_info['name']
    return jsonify({'success': True, 'data': {
        'total': total, 'page': page, 'limit': limit, 'users': users
    }})


@admin_bp.route('/quota/users/<int:uid>/tier', methods=['POST'])
def quota_set_user_tier(uid):
    """设置用户的API配额等级"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    tier = data.get('tier', '').strip()
    from models import TIERS
    if tier not in TIERS:
        return jsonify({'success': False, 'error': f'Invalid tier: {tier}'}), 400
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM app_authorizations WHERE user_id=%s AND active=1", (uid,)
        ).fetchone()
        if existing:
            conn.execute("UPDATE app_authorizations SET tier=%s, last_reset=CURRENT_DATE WHERE id=%s", (tier, existing['id']))
        else:
            conn.execute(
                "INSERT INTO app_authorizations (user_id, app_name, tier, active) VALUES (%s, 'main', %s, 1)",
                (uid, tier)
            )
        conn.commit()
    _log(admin['user_id'], 'set_user_tier', 'user', str(uid), f'tier→{tier}')
    return jsonify({'success': True, 'message': f'User level updated to {TIERS[tier]["name"]}'})


@admin_bp.route('/quota/keys', methods=['GET'])
def quota_keys():
    """查询所有API Key的配额使用情况"""
    admin, err = _require_admin()
    if err:
        return err
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 20, type=int)
    offset = (page - 1) * limit
    with get_db() as conn:
        total = conn.execute('SELECT COUNT(*) as c FROM api_keys').fetchone()['c']
        rows = conn.execute("""
            SELECT k.id, k.name, k.key_prefix, k.calls_today, k.calls_total,
                   k.last_reset, k.last_used, k.active, k.created_at,
                   COALESCE(u.display_name, u.username, '') as user_name, u.id as user_id,
                   COALESCE(a.tier, 'free') as tier
            FROM api_keys k
            LEFT JOIN users u ON k.user_id=u.id
            LEFT JOIN app_authorizations a ON u.id=a.user_id AND a.active=1
            ORDER BY k.created_at DESC
            LIMIT %s OFFSET %s
        """, (limit, offset)).fetchall()
    return jsonify({'success': True, 'data': {
        'total': total, 'page': page, 'limit': limit, 'keys': [dict(r) for r in rows]
    }})


@admin_bp.route('/quota/keys/<int:kid>/reset', methods=['POST'])
def quota_reset_key(kid):
    """重置单个API Key的日调用量"""
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        conn.execute("UPDATE api_keys SET calls_today=0, last_reset=CURRENT_DATE WHERE id=%s", (kid,))
        conn.commit()
    _log(admin['user_id'], 'reset_key_quota', 'api_key', str(kid))
    return jsonify({'success': True, 'message': _('Daily call count for the key has been reset')})


@admin_bp.route('/quota/overview', methods=['GET'])
def quota_overview():
    """详细配额使用报表（最近7天趋势）"""
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        # 每日总调用量最近7天
        daily = conn.execute("""
            SELECT last_reset as date, SUM(calls_today) as calls_count
            FROM api_keys WHERE last_reset >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY last_reset ORDER BY last_reset
        """).fetchall()
        daily_stats = [dict(r) for r in daily]
        # 超出阈值（calls_today >= tier daily_limit * 0.8）的key
        from models import TIERS
        near_limit = conn.execute("""
            SELECT k.id, k.name, k.key_prefix, k.calls_today,
                   COALESCE(u.display_name, u.username, '') as user_name
            FROM api_keys k
            LEFT JOIN users u ON k.user_id=u.id
            LEFT JOIN app_authorizations a ON u.id=a.user_id AND a.active=1
            WHERE k.active=1
        """).fetchall()
        near_limit_list = []
        for r in near_limit:
            tier_key = 'free'
            with get_db() as conn2:
                tr = conn2.execute(
                    "SELECT tier FROM app_authorizations WHERE user_id=%s AND active=1",
                    (r['user_id'],)
                ).fetchone()
                if tr:
                    tier_key = tr['tier']
            limit_val = TIERS.get(tier_key, TIERS['free'])['daily_limit']
            if limit_val > 0 and r['calls_today'] >= limit_val * 0.8:
                nr = dict(r)
                nr['daily_limit'] = limit_val
                nr['usage_pct'] = round(r['calls_today'] / limit_val * 100, 1)
                near_limit_list.append(nr)
    return jsonify({'success': True, 'data': {
        'daily_stats': daily_stats,
        'near_limit_keys': near_limit_list
    }})

@admin_bp.route('/i18n/translations', methods=['GET'])
def admin_i18n_list():
    """列出翻译（分页+搜索）"""
    admin, err = _require_admin()
    if err:
        return err

    locale = request.args.get('locale', 'en')
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    offset = (page - 1) * limit

    from i18n import list_translations
    data = list_translations(locale=locale, search=search, offset=offset, limit=limit)
    return jsonify({'success': True, 'data': data})


@admin_bp.route('/i18n/translations', methods=['POST'])
def admin_i18n_create():
    """新增一条翻译"""
    admin, err = _require_admin()
    if err:
        return err

    data = request.get_json(force=True) or {}
    locale = data.get('locale', 'en')
    source = (data.get('source') or '').strip()
    translation = (data.get('translation') or '').strip()

    if not source:
        return jsonify({'success': False, 'error': _(_('Original text cannot be empty'))}), 400

    from i18n import set_translation
    ok = set_translation(locale, source, translation, is_auto=0)
    # D-06: 原 return jsonify(...),\n 201 if ok else 400, 被 AST 解析为 1 元组 → Flask 500；
    # 合并为单次返回 (response, status)
    status = 201 if ok else 400
    return jsonify({'success': ok, 'error': '' if ok else _('Write failed')}), status


@admin_bp.route('/i18n/translations/<int:tid>', methods=['PUT'])
def admin_i18n_update(tid):
    """编辑一条翻译"""
    admin, err = _require_admin()
    if err:
        return err

    data = request.get_json(force=True) or {}
    translation = (data.get('translation') or '').strip()
    is_auto = data.get('is_auto', 0)

    with get_db() as conn:
        exist = conn.execute('SELECT id FROM i18n_strings WHERE id=%s', (tid,)).fetchone()
        if not exist:
            return jsonify({'success': False, 'error': _(_('Translation does not exist'))}), 404
        conn.execute(
            "UPDATE i18n_strings SET translation=%s, is_auto=%s, updated_at=NOW() WHERE id=%s",
            (translation, is_auto, tid)
        )
        conn.commit()

    return jsonify({'success': True, 'message': _('Updated')})


@admin_bp.route('/i18n/translations/<int:tid>', methods=['DELETE'])
def admin_i18n_delete(tid):
    """删除一条翻译"""
    admin, err = _require_admin()
    if err:
        return err

    from i18n import delete_translation
    ok = delete_translation(tid)
    return jsonify({'success': ok, 'error': '' if ok else _('Delete failed')})


@admin_bp.route('/i18n/seed', methods=['POST'])
def admin_i18n_seed():
    """从 YAML 同步翻译到 DB"""
    admin, err = _require_admin()
    if err:
        return err

    locale = request.args.get('locale', 'en')
    from i18n import seed_from_yaml
    count = seed_from_yaml(locale)
    return jsonify({'success': True, 'message': f'Synchronized {count} records to DB'})
@admin_bp.route('/provider-api-keys', methods=['GET'])
def provider_api_key_list():
    """列出所有 Provider API Key（key_value_enc 脱敏，返回 key_preview）"""
    admin, err = _require_admin()
    if err:
        return err
    from services.crypto import decrypt as _decrypt
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, provider, description, is_active, key_value_enc, "
            "CASE WHEN key_value_enc != '' THEN 1 ELSE 0 END AS has_value, "
            "created_at, updated_at "
            "FROM provider_api_keys ORDER BY id"
        ).fetchall()
        data = []
        for r in rows:
            item = dict(r)
            preview = ''
            if item.get('key_value_enc'):
                try:
                    raw = _decrypt(item['key_value_enc'])
                    if len(raw) > 10:
                        preview = raw[:6] + '***' + raw[-3:]
                    elif raw:
                        preview = raw[:3] + '***'
                except Exception:
                    preview = '***'
            item['key_preview'] = preview
            item.pop('key_value_enc', None)
            data.append(item)
        return jsonify({'success': True, 'data': data})


@admin_bp.route('/provider-api-keys', methods=['POST'])
def provider_api_key_create():
    """新增 Provider API Key（value 加密存储）"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    key_value = (data.get('key_value') or '').strip()
    provider = (data.get('provider') or '').strip()
    if not name or not key_value:
        return jsonify({'success': False, 'error': _('Name and Key cannot be empty')}), 400

    try:
        from services.crypto import encrypt
        encrypted = encrypt(key_value)
    except Exception:
        encrypted = key_value  # fallback: store as plaintext if encryption unavailable

    with get_db() as conn:
        row = conn.execute(
            'INSERT INTO provider_api_keys (name, key_value_enc, provider, description) '
            'VALUES (%s,%s,%s,%s) RETURNING id',
            (name, encrypted, provider, data.get('description', ''))
        ).fetchone()
        conn.commit()
        kid = row['id']
    _log(admin['user_id'], 'create_provider_key', 'provider_api_key', str(kid), name)
    return jsonify({'success': True, 'data': {'id': kid}})


@admin_bp.route('/provider-api-keys/<int:kid>', methods=['PUT'])
def provider_api_key_update(kid):
    """更新 Provider API Key（value 可选更新）"""
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    with get_db() as conn:
        row = conn.execute('SELECT * FROM provider_api_keys WHERE id=%s', (kid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Not found')}), 404

        updates = []
        params = []
        for field in ['name', 'provider', 'description']:
            if field in data and data[field] is not None:
                updates.append(f'{field}=%s')
                params.append(data[field].strip() if isinstance(data[field], str) else data[field])
        if 'is_active' in data:
            updates.append('is_active=%s')
            params.append(1 if data['is_active'] else 0)
        if data.get('key_value', '').strip():
            try:
                from services.crypto import encrypt
                key_enc = encrypt(data['key_value'].strip())
            except Exception:
                key_enc = data['key_value'].strip()
            updates.append('key_value_enc=%s')
            params.append(key_enc)
        if not updates:
            return jsonify({'success': True, 'message': _('No changes')})

        updates.append('updated_at=NOW()')
        params.append(kid)
        conn.execute(
            f"UPDATE provider_api_keys SET {','.join(updates)} WHERE id=%s",
            params
        )
        conn.commit()
    _log(admin['user_id'], 'update_provider_key', 'provider_api_key', str(kid))
    return jsonify({'success': True})


@admin_bp.route('/provider-api-keys/<int:kid>', methods=['DELETE'])
def provider_api_key_delete(kid):
    """删除 Provider API Key（检查引用）"""
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        # 检查是否被 provider_models 引用（api_key_id）
        refs = conn.execute(
            'SELECT COUNT(*) as cnt FROM provider_models WHERE api_key_id=%s', (kid,)
        ).fetchone()
        if refs['cnt'] > 0:
            return jsonify({
                'success': False,
                'error': _('Key is referenced by %(count)s model(s), please unlink first', count=refs['cnt'])
            }), 400

        # 检查 provider_models 旧字段引用
        key_row = conn.execute('SELECT name FROM provider_api_keys WHERE id=%s', (kid,)).fetchone()
        if key_row:
            refs2 = conn.execute(
                "SELECT COUNT(*) as cnt FROM provider_models "
                "WHERE api_key_ref = %s",
                (key_row['name'],)
            ).fetchone()
            if refs2 and refs2['cnt'] > 0:
                return jsonify({
                    'success': False,
                    'error': f'Key is referenced by {refs2["cnt"]} model(s) via api_key_ref'
                }), 409

            # 检查 agent_matrix 引用
            refs3 = conn.execute(
                "SELECT COUNT(*) as cnt FROM agent_matrix WHERE api_key_ref = %s",
                (str(kid),)
            ).fetchone()
            if refs3 and refs3['cnt'] > 0:
                return jsonify({
                    'success': False,
                    'error': f'Key is referenced by {refs3["cnt"]} agent(s)'
                }), 409

        row = conn.execute('SELECT name FROM provider_api_keys WHERE id=%s', (kid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Not found')}), 404
        conn.execute('DELETE FROM provider_api_keys WHERE id=%s', (kid,))
        conn.commit()
    _log(admin['user_id'], 'delete_provider_key', 'provider_api_key', str(kid), row['name'])
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════
# LLM Quota 管理（按用户/模型/模块的精细化配额）
# ═══════════════════════════════════════════════════════

@admin_bp.route('/llm-quotas', methods=['GET'])
def llm_quota_list():
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM llm_quotas ORDER BY target_type, target_id'
        ).fetchall()
        return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@admin_bp.route('/llm-quotas', methods=['POST'])
def llm_quota_create():
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    target_type = data.get('target_type', 'module')
    if target_type not in ('user', 'model', 'module', 'global'):
        return jsonify({'success': False, 'error': _('Invalid target_type')}), 400
    with get_db() as conn:
        row = conn.execute(
            'INSERT INTO llm_quotas (target_type, target_id, daily_limit, rate_limit, rate_window_sec) '
            'VALUES (%s,%s,%s,%s,%s) RETURNING id',
            (target_type, data.get('target_id'), data.get('daily_limit', 0),
             data.get('rate_limit', 0), data.get('rate_window_sec', 60))
        ).fetchone()
        conn.commit()
    _log(admin['user_id'], 'create_llm_quota', 'llm_quota', str(row['id']))
    return jsonify({'success': True, 'data': {'id': row['id']}})


@admin_bp.route('/llm-quotas/<int:qid>', methods=['PUT'])
def llm_quota_update(qid):
    admin, err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    with get_db() as conn:
        row = conn.execute('SELECT * FROM llm_quotas WHERE id=%s', (qid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Not found')}), 404
        updates = []
        params = []
        for field in ['target_type', 'daily_limit', 'rate_limit', 'rate_window_sec', 'target_id', 'is_active']:
            if field in data:
                updates.append(f'{field}=%s')
                params.append(data[field])
        if not updates:
            return jsonify({'success': True, 'message': _('No changes')})
        updates.append('updated_at=NOW()')
        params.append(qid)
        conn.execute(f"UPDATE llm_quotas SET {','.join(updates)} WHERE id=%s", params)
        conn.commit()
    return jsonify({'success': True})


@admin_bp.route('/llm-quotas/<int:qid>', methods=['DELETE'])
def llm_quota_delete(qid):
    admin, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        conn.execute('DELETE FROM llm_quotas WHERE id=%s', (qid,))
        conn.commit()
    _log(admin['user_id'], 'delete_llm_quota', 'llm_quota', str(qid))


@admin_bp.route('/ai-model-health', methods=['GET'])
def ai_model_health():
    admin, err = _require_admin()
    if err:
        return err
    from agent_matrix.failover import ModelHealthStore
    return jsonify({'success': True, 'data': ModelHealthStore().get_health()})


@admin_bp.route('/ai-model-failover-events', methods=['GET'])
def ai_model_failover_events():
    admin, err = _require_admin()
    if err:
        return err
    from agent_matrix.failover import ModelHealthStore
    return jsonify({'success': True, 'data': ModelHealthStore().get_recent_events()})
