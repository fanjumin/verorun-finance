#!/usr/bin/env python3
"""主题管理 API — 安装 / 列表 / 启用 / 删除"""
from i18n import _
import os, sys, json, zipfile, shutil, re
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app

theme_bp = Blueprint('theme_admin', __name__, url_prefix='/admin')

BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..')
THEMES_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'themes'))

ALLOWED_EXTENSIONS = {'.css', '.html', '.json', '.png', '.svg', '.jpg', '.jpeg', '.woff2', '.ttf', '.md', '.txt'}
FORBIDDEN_PATTERNS = [r'\.py$', r'\.js$', r'\.php$', r'\.sh$', r'\.exe$', r'\.bat$', r'\.dll$', r'\.so$']
SITE_KEYS = ['main', 'platform', 'admin']
SITE_LABELS = {'main': '主站', 'platform': '用户后台', 'admin': '管理后台'}

def _get_db():
    from models import get_db
    return get_db()

def _require_admin():
    from services.jwt_service import validate_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.args.get('token') or request.cookies.get('sso_token') or request.cookies.get('tm_token')
    payload = validate_token(token) if token else None
    if not payload or not payload.get('is_admin'):
        return jsonify({'success': False, 'error': '需要管理员权限'}), 403
    return None

def _sanitize_filename(name):
    """移除路径遍历字符"""
    return re.sub(r'[/\\]', '_(', name)

@theme_bp.route(')/themes', methods=['GET'])
def list_themes():
    err = _require_admin()
    if err: return err
    with _get_db() as conn:
        themes = conn.execute(
            'SELECT id, name, slug, version, author, author_url, description, industry, config_json, dir_name, installed_at '
            'FROM themes ORDER BY id'
        ).fetchall()
        site_configs = conn.execute(
            'SELECT site_key, theme_id FROM site_theme_config'
        ).fetchall()
    
    theme_list = []
    for t in themes:
        d = dict(t)
        # active sites
        active = [s['site_key'] for s in site_configs if s['theme_id'] == t['id']]
        d['active_sites'] = active
        d['is_default'] = (t['slug'] == 'default')
        d['thumbnail'] = '/themes/{}/preview.png'.format(t['dir_name'])
        try:
            cfg = json.loads(d.pop('config_json', '{}'))
            d['tags'] = cfg.get('tags', [])
            d['sites_supported'] = cfg.get('sites', [])
        except:
            d['tags'] = []
            d['sites_supported'] = []
        theme_list.append(d)
    
    return jsonify({'success': True, 'data': theme_list})

@theme_bp.route('/themes/install', methods=['POST'])
def install_theme():
    err = _require_admin()
    if err: return err

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': '未收到文件'}), 400
    
    file = request.files['file']
    if not file.filename or not file.filename.lower().endswith('.zip'):
        return jsonify({'success': False, 'error': '请上传 .zip 格式的主题包'}), 400

    # 保存临时文件
    tmp_path = os.path.join('/tmp', 'theme_{}.zip'.format(int(datetime.now().timestamp())))
    file.save(tmp_path)

    try:
        with zipfile.ZipFile(tmp_path, 'r') as zf:
            # 安全检查
            total_size = 0
            for info in zf.infolist():
                # 路径遍历防护
                original = info.filename.split('/')[-1]
                safe_name = _sanitize_filename(original)
                if safe_name != original:
                    return jsonify({'success': False, 'error': '文件名包含非法字符: {}'.format(original)}), 400
                
                ext = os.path.splitext(original)[1].lower()
                
                # 黑名单检查
                for pat in FORBIDDEN_PATTERNS:
                    if re.search(pat, original.lower()):
                        return jsonify({'success': False, 'error': '禁止的文件类型: {}'.format(original)}), 400
                
                # 白名单检查（只对已知文件）
                if ext and ext not in ALLOWED_EXTENSIONS:
                    return jsonify({'success': False, 'error': '不支持的文件类型: {}'.format(ext)}), 400
                
                # 大小限制
                if info.file_size > 2 * 1024 * 1024:  # 单文件 2MB
                    return jsonify({'success': False, 'error': '文件过大: {}'.format(original)}), 400
                total_size += info.file_size
                if total_size > 10 * 1024 * 1024:  # 总大小 10MB
                    return jsonify({'success': False, 'error': '主题包总大小超过 10MB'}), 400

            # 读取 theme.json
            try:
                manifest_data = zf.read('theme.json').decode('utf-8')
                manifest = json.loads(manifest_data)
            except KeyError:
                return jsonify({'success': False, 'error': '缺少 theme.json 文件'}), 400
            except json.JSONDecodeError as e:
                return jsonify({'success': False, 'error': 'theme.json 格式错误: {}'.format(str(e))}), 400

            # 验证必需字段
            if not manifest.get('name') or not manifest.get('slug'):
                return jsonify({'success': False, 'error': 'theme.json 缺少必需字段: name, slug'}), 400

            slug = manifest['slug'].strip().lower()
            slug = re.sub(r'[^a-z0-9\-]', '-', slug)
            if slug != manifest['slug'].strip():
                return jsonify({'success': False, 'error': 'slug 只能包含小写字母、数字和连字符'}), 400

            # 检查重复
            with _get_db() as conn:
                existing = conn.execute('SELECT id FROM themes WHERE slug=%s', (slug,)).fetchone()
                if existing:
                    return jsonify({'success': False, 'error': '主题 slug "{}" 已存在'.format(slug)}), 409

            # 解压
            dest_dir = os.path.join(THEMES_ROOT, slug)
            os.makedirs(dest_dir, exist_ok=True)
            zf.extractall(dest_dir)

            # 验证 theme.css 存在
            if not os.path.isfile(os.path.join(dest_dir, 'theme.css')):
                open(os.path.join(dest_dir, 'theme.css'), 'w').write('/* {} */\n'.format(manifest['name']))

            # 写入数据库
            with _get_db() as conn:
                conn.execute(
                    'INSERT INTO themes (name, slug, version, author, author_url, description, industry, '
                    'tags, config_json, dir_name, installed_at, updated_at) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())',
                    (
                        manifest['name'],
                        slug,
                        manifest.get('version', '1.0.0'),
                        manifest.get('author', ''),
                        manifest.get('author_url', ''),
                        manifest.get('description', ''),
                        manifest.get('industry', ''),
                        json.dumps(manifest.get('tags', []), ensure_ascii=False),
                        json.dumps(manifest, ensure_ascii=False),
                        slug,
                    )
                )
                conn.commit()

            return jsonify({
                'success': True,
                'theme': {'slug': slug, 'name': manifest['name'], 'version': manifest.get('version', '1.0.0')}
            })

    except zipfile.BadZipFile:
        return jsonify({'success': False, 'error': '无效的 ZIP 文件'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': '安装失败: {}'.format(str(e))}), 500
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@theme_bp.route('/themes/<int:theme_id>', methods=['GET'])
def get_theme(theme_id):
    err = _require_admin()
    if err: return err
    with _get_db() as conn:
        t = conn.execute('SELECT * FROM themes WHERE id=%s', (theme_id,)).fetchone()
    if not t:
        return jsonify({'success': False, 'error': '主题不存在'}), 404
    return jsonify({'success': True, 'data': dict(t)})

@theme_bp.route('/themes/<int:theme_id>', methods=['DELETE'])
def delete_theme(theme_id):
    err = _require_admin()
    if err: return err
    with _get_db() as conn:
        t = conn.execute('SELECT * FROM themes WHERE id=%s', (theme_id,)).fetchone()
        if not t:
            return jsonify({'success': False, 'error': '主题不存在'}), 404
        if t['slug'] == 'default':
            return jsonify({'success': False, 'error': '不能删除默认主题'}), 403
        
        # 清空使用该主题的站点
        conn.execute('UPDATE site_theme_config SET theme_id=NULL, updated_at=NOW() WHERE theme_id=%s', (theme_id,))
        # 删除 DB 记录
        conn.execute('DELETE FROM themes WHERE id=%s', (theme_id,))
        conn.commit()
    
    # 删除目录
    theme_dir = os.path.join(THEMES_ROOT, t['dir_name'])
    if os.path.isdir(theme_dir):
        try:
            shutil.rmtree(theme_dir)
        except Exception:
            pass
    
    return jsonify({'success': True, 'message': '主题已卸载'})

@theme_bp.route('/themes/sites', methods=['GET'])
def list_site_themes():
    err = _require_admin()
    if err: return err
    with _get_db() as conn:
        configs = conn.execute('SELECT site_key, theme_id FROM site_theme_config').fetchall()
        themes = conn.execute('SELECT id, name, slug FROM themes').fetchall()
    
    theme_map = {t['id']: {'name': t['name'], 'slug': t['slug']} for t in themes}
    result = []
    config_map = {c['site_key']: c['theme_id'] for c in configs}
    
    for key in SITE_KEYS:
        tid = config_map.get(key)
        tinfo = theme_map.get(tid) if tid else None
        result.append({
            'site_key': key,
            'label': SITE_LABELS.get(key, key),
            'theme_id': tid,
            'theme_name': tinfo['name'] if tinfo else '默认',
            'theme_slug': tinfo['slug'] if tinfo else None,
        })
    
    return jsonify({'success': True, 'data': result})

@theme_bp.route('/themes/sites', methods=['PUT'])
def set_site_theme():
    err = _require_admin()
    if err: return err
    data = request.get_json() or {}
    site_key = data.get('site_key', '').strip()
    theme_id = data.get('theme_id')  # can be null/0 for default
    
    if site_key not in SITE_KEYS:
        return jsonify({'success': False, 'error': '无效的 site_key'}), 400
    
    if theme_id and theme_id != 0:
        with _get_db() as conn:
            t = conn.execute('SELECT id FROM themes WHERE id=%s', (theme_id,)).fetchone()
            if not t:
                return jsonify({'success': False, 'error': '主题不存在'}), 404
        final_id = theme_id
    else:
        final_id = None
    
    with _get_db() as conn:
        existing = conn.execute('SELECT id FROM site_theme_config WHERE site_key=%s', (site_key,)).fetchone()
        if existing:
            conn.execute(
                'UPDATE site_theme_config SET theme_id=%s, updated_at=NOW() WHERE site_key=%s',
                (final_id, site_key)
            )
        else:
            conn.execute(
                'INSERT INTO site_theme_config (site_key, theme_id, updated_at) VALUES (%s,%s,NOW())',
                (site_key, final_id)
            )
        conn.commit()
    
    return jsonify({'success': True, 'message': '站点 {} 主题已切换'.format(SITE_LABELS.get(site_key, site_key))})


# 导出辅助函数供 app.py 使用
def get_active_theme_slug_for_site(site_key):
    """查询某站点当前激活的主题 slug，返回 None 表示默认"""
    try:
        with _get_db() as conn:
            row = conn.execute(
                'SELECT t.slug FROM site_theme_config s '
                'LEFT JOIN themes t ON s.theme_id = t.id '
                'WHERE s.site_key=%s', (site_key,)
            ).fetchone()
        if row and row['slug'] and row['slug'] != 'default':
            return row['slug']
    except Exception:
        pass
    return None

def get_active_theme_template_dir(site_key):
    """返回激活主题的模板目录绝对路径，若无则返回 None"""
    slug = get_active_theme_slug_for_site(site_key)
    if not slug:
        return None
    tpl_dir = os.path.join(THEMES_ROOT, slug, 'templates')
    if os.path.isdir(tpl_dir):
        return tpl_dir
    return None
