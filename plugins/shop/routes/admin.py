#!/usr/bin/env python3
"""Shop Admin — 商城管理 (商品CRUD + 多图上传 + SKU/规格 + 分类 + 订单 + 优惠券)"""
from i18n import _, get_lang
import sys, os, json, time, secrets, csv, io
from flask import Blueprint, jsonify, request, current_app, send_file
from models import get_db
from datetime import datetime
from werkzeug.utils import secure_filename

shop_admin_bp = Blueprint('shop_admin', __name__, url_prefix='/admin/shop')


def _get_plugin_instance(name):
    """获取已启用插件实例，未启用返回 None"""
    pm = current_app.extensions.get('plugin_manager')
    if pm and pm.is_enabled(name):
        return pm.get_instance(name)
    return None


from plugin_manager.logger import get_plugin_logger
logger = get_plugin_logger('shop')


def _detect_image_type(header):
    """根据文件头魔数识别图片类型，无法识别返回 None"""
    if header[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if header[:2] == b'\xff\xd8':
        return 'jpg'
    if header[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if len(header) >= 12 and header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'webp'
    return None


# ── 输入长度限制 ──
_MAX_TITLE = 200
_MAX_SUBTITLE = 500
_MAX_CATEGORY = 100
_MAX_THUMBNAIL = 500
_MAX_DESC = 50000
_MAX_FEATURES_JSON = 50000
_MAX_AI_CONFIG_JSON = 50000

# ── 图片上传配置 ──
_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), '..', 'static', 'products')
_ALLOWED_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
_MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB
os.makedirs(_UPLOAD_DIR, exist_ok=True)


def _require_admin():
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        return None, (jsonify({'success': False, 'error': _('Please log in first')}), 401)
    from services.jwt_service import validate_token
    payload = validate_token(token)
    if not payload:
        return None, (jsonify({'success': False, 'error': _('Invalid Token')}), 401)
    if not payload.get('is_admin'):
        return None, (jsonify({'success': False, 'error': _('Requires admin permissions')}), 403)
    return payload, None


def _require_user():
    """验证用户登录（不要求管理员）"""
    auth = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '') if auth.startswith('Bearer ') else auth
    if not token:
        token = request.cookies.get('sso_token') or request.cookies.get('tm_token') or ''
    if not token:
        return None, (jsonify({'success': False, 'error': _('Please log in first')}), 401)
    from services.jwt_service import validate_token
    payload = validate_token(token)
    if not payload:
        return None, (jsonify({'success': False, 'error': _('Invalid Token')}), 401)
    return payload, None


def _log_admin_action(conn, admin_id, action, target_type, target_id, detail=''):
    try:
        conn.execute(
            'INSERT INTO admin_logs (admin_id, action, target_type, target_id, detail) VALUES (%s,%s,%s,%s,%s)',
            (admin_id, action, target_type, str(target_id), detail[:500])
        )
    except Exception:
        pass


def _safe_json(val, default=None):
    """安全解析JSON字段"""
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str) and val:
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            pass
    return default if default is not None else ([] if isinstance(default, list) else {})


def _product_to_dict(row):
    """将 products 行转为dict并解析JSON字段"""
    p = dict(row)
    p['features'] = _safe_json(p.get('features'), [])
    p['ai_config'] = _safe_json(p.get('ai_config'), {})
    p['images'] = _safe_json(p.get('images'), [])
    return p


def _gen_slug(text):
    """由标题生成 URL slug（小写连字符），超长截断"""
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-') or 'product'
    return slug[:80]


def _export_csv(headers, rows, filename):
    """P2：生成 UTF-8 BOM CSV 下载响应"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    data = ('\ufeff' + buf.getvalue()).encode('utf-8')
    return send_file(io.BytesIO(data), mimetype='text/csv', as_attachment=True,
                     download_name=filename)


# =============================================
# 图片上传
# =============================================
@shop_admin_bp.route('/products/upload-image', methods=['POST'])
def upload_image():
    """上传商品图片，返回URL"""
    payload, err = _require_admin()
    if err:
        return err

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': _('No file selected')}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'success': False, 'error': _('File name is empty')}), 400

    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    if ext not in _ALLOWED_EXTS:
        return jsonify({'success': False, 'error': f'Unsupported file format: {ext}, supported {_ALLOWED_EXTS}'}), 400

    # 魔数校验：确保文件内容与扩展名一致，防止伪装文件上传
    header = file.read(12)
    file.seek(0)
    detected = _detect_image_type(header)
    expected = 'jpg' if ext in ('jpg', 'jpeg') else ext
    if not detected or detected != expected:
        return jsonify({'success': False, 'error': _('File content does not match its extension')}), 400

    # 限流：每秒最多上传2张
    _rl_key = f'upload_img_{payload["user_id"]}'
    _rl = getattr(request, '_rate_limit_cache', {})
    now_t = time.time()
    last = _rl.get(_rl_key, 0)
    if now_t - last < 0.5:
        return jsonify({'success': False, 'error': _('Operation too fast, please wait a moment')}), 429
    _rl[_rl_key] = now_t
    request._rate_limit_cache = _rl

    # 生成唯一文件名
    ts = str(int(time.time() * 1000))
    rand = secrets.token_hex(4)
    filename = f'{ts}_{rand}.{ext}'
    filepath = os.path.join(_UPLOAD_DIR, filename)
    file.save(filepath)

    url = f'/pimg/{filename}'
    return jsonify({'success': True, 'data': {'url': url, 'filename': filename}})


@shop_admin_bp.route('/products/<int:pid>/images', methods=['GET'])
def get_product_images(pid):
    """获取商品图片列表"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute('SELECT images FROM products WHERE id=%s', (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        images = _safe_json(row['images'], [])
    return jsonify({'success': True, 'data': {'images': images}})


@shop_admin_bp.route('/products/<int:pid>/images', methods=['POST'])
def add_product_image(pid):
    """为商品添加图片"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'success': False, 'error': _('Please provide an image URL')}), 400

    with get_db() as conn:
        row = conn.execute('SELECT images FROM products WHERE id=%s', (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        images = _safe_json(row['images'], [])
        images.append({'url': url, 'sort_order': len(images)})
        conn.execute('UPDATE products SET images=%s, updated_at=NOW() WHERE id=%s',
                     (json.dumps(images, ensure_ascii=False), pid))
        conn.commit()
    return jsonify({'success': True, 'data': {'images': images}})


@shop_admin_bp.route('/products/<int:pid>/images/<int:idx>', methods=['DELETE'])
def delete_product_image(pid, idx):
    """删除商品指定图片"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute('SELECT images FROM products WHERE id=%s', (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        images = _safe_json(row['images'], [])
        if idx < 0 or idx >= len(images):
            return jsonify({'success': False, 'error': _('Invalid picture index')}), 400

        removed = images.pop(idx)
        # 如果是本地图片，删除物理文件
        url = removed.get('url', '') if isinstance(removed, dict) else str(removed)
        if url.startswith('/pimg/'):
            fpath = os.path.join(_UPLOAD_DIR, os.path.basename(url))
            if os.path.exists(fpath):
                os.remove(fpath)

        conn.execute('UPDATE products SET images=%s, updated_at=NOW() WHERE id=%s',
                     (json.dumps(images, ensure_ascii=False), pid))
        conn.commit()
    return jsonify({'success': True, 'data': {'images': images}})


@shop_admin_bp.route('/products/<int:pid>/images/reorder', methods=['POST'])
def reorder_product_images(pid):
    """重新排序商品图片"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    order = data.get('order', [])  # [2, 0, 1, 3] 新顺序索引
    if not order:
        return jsonify({'success': False, 'error': _('Please provide the sequence')}), 400

    with get_db() as conn:
        row = conn.execute('SELECT images FROM products WHERE id=%s', (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        images = _safe_json(row['images'], [])
        if len(order) != len(images):
            return jsonify({'success': False, 'error': _('Sequence index count mismatch')}), 400
        try:
            reordered = [images[i] for i in order]
        except IndexError:
            return jsonify({'success': False, 'error': _('Index out of range')}), 400
        conn.execute('UPDATE products SET images=%s, updated_at=NOW() WHERE id=%s',
                     (json.dumps(reordered, ensure_ascii=False), pid))
        conn.commit()
    return jsonify({'success': True, 'data': {'images': reordered}})


# =============================================
# 商品列表 / CRUD（增强版）
# =============================================
@shop_admin_bp.route('/products', methods=['GET'])
def list_products():
    """商品列表，支持搜索/分类/状态筛选"""
    payload, err = _require_admin()
    if err:
        return err
    search = request.args.get('search', '').strip()
    category_id = request.args.get('category_id', type=int, default=0)
    is_active = request.args.get('is_active', type=int, default=-1)
    status = request.args.get('status', '').strip()
    with get_db() as conn:
        sql = 'SELECT p.*, c.name as category_name FROM products p LEFT JOIN categories c ON p.category_id=c.id WHERE 1=1'
        params = []
        if search:
            sql += ' AND (p.title LIKE %s OR p.subtitle LIKE %s)'
            s = f'%{search}%'
            params.extend([s, s])
        if category_id > 0:
            sql += ' AND p.category_id=%s'
            params.append(category_id)
        if status:
            sql += ' AND p.status=%s'
            params.append(status)
        if is_active >= 0:
            sql += ' AND p.is_active=%s'
            params.append(is_active)
        sql += ' ORDER BY p.sort_order ASC, p.id DESC'
        rows = conn.execute(sql, params).fetchall()
    return jsonify({'success': True, 'data': [_product_to_dict(r) for r in rows]})


@shop_admin_bp.route('/products/export', methods=['GET'])
def export_products():
    """P2：商品 CSV 导出（UTF-8 BOM，Excel 兼容）"""
    payload, err = _require_admin()
    if err:
        return err
    headers = ['id', 'title', 'subtitle', 'product_type', 'category', 'price',
               'original_price', 'stock', 'sort_order', 'status', 'is_active',
               'slug', 'meta_title', 'meta_description', 'description', 'features']
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, title, subtitle, product_type, category, price, original_price, '
            'stock, sort_order, status, is_active, slug, meta_title, meta_description, '
            'description, features FROM products ORDER BY id'
        ).fetchall()
    data = [[
        r['id'], r['title'], r['subtitle'] or '', r['product_type'] or '',
        r['category'] or '', r['price'] or 0, r['original_price'] or 0,
        r['stock'] if r['stock'] is not None else '', r['sort_order'] or 0,
        r['status'] or 'active', r['is_active'] or 0, r['slug'] or '',
        r['meta_title'] or '', r['meta_description'] or '',
        r['description'] or '', r['features'] or ''
    ] for r in rows]
    return _export_csv(headers, data, 'shop_products.csv')


@shop_admin_bp.route('/products/import', methods=['POST'])
def import_products():
    """P2：商品 CSV 批量导入（按 id upsert，返回统计）"""
    payload, err = _require_admin()
    if err:
        return err
    f = request.files.get('file')
    if not f:
        return jsonify({'success': False, 'error': _('No file uploaded')}), 400
    try:
        text = f.read().decode('utf-8-sig', errors='replace')
        reader = csv.DictReader(io.StringIO(text))
    except Exception:
        return jsonify({'success': False, 'error': _('Import failed')}), 400

    def _iv(val, default=0):
        try:
            return int(float(val))
        except Exception:
            return default

    def _fv(val, default=0.0):
        try:
            return float(val)
        except Exception:
            return default

    imported = updated = failed = 0
    errors = []
    with get_db() as conn:
        for i, row in enumerate(reader, start=2):
            try:
                title = (row.get('title') or '').strip()
                if not title:
                    raise ValueError(f'row {i}: missing title')
                pid = _iv(row.get('id'), 0)
                status_val = (row.get('status') or 'active').strip()
                if status_val not in ('draft', 'active', 'archived'):
                    status_val = 'active'
                slug_val = (row.get('slug') or '').strip() or _gen_slug(title)
                fvals = [
                    title, (row.get('subtitle') or '').strip(),
                    (row.get('product_type') or 'service').strip(),
                    (row.get('category') or '').strip(),
                    _fv(row.get('price')),
                    _fv(row.get('original_price')),
                    _iv(row.get('stock')),
                    _iv(row.get('sort_order')),
                    status_val,
                    1 if str(row.get('is_active', '1')).strip().lower() in ('1', 'true', 'yes') else 0,
                    slug_val, (row.get('meta_title') or '').strip(),
                    (row.get('meta_description') or '').strip(),
                    (row.get('description') or ''),
                    (row.get('features') or ''),
                ]
                if pid:
                    existing = conn.execute('SELECT id FROM products WHERE id=%s', (pid,)).fetchone()
                    if not existing:
                        raise ValueError(f'row {i}: product id {pid} not found')
                    conn.execute(
                        '''UPDATE products SET title=%s, subtitle=%s, product_type=%s, category=%s,
                           price=%s, original_price=%s, stock=%s, sort_order=%s, status=%s,
                           is_active=%s, slug=%s, meta_title=%s, meta_description=%s,
                           description=%s, features=%s WHERE id=%s''',
                        fvals + [pid]
                    )
                    updated += 1
                else:
                    conn.execute(
                        '''INSERT INTO products (title, subtitle, product_type, category, price,
                           original_price, stock, sort_order, status, is_active, slug, meta_title,
                           meta_description, description, features)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        fvals
                    )
                    imported += 1
            except Exception as e:
                failed += 1
                errors.append(str(e))
        conn.commit()
    return jsonify({'success': True, 'data': {'imported': imported, 'updated': updated,
                                              'failed': failed, 'errors': errors[:20]}})


@shop_admin_bp.route('/products/<int:pid>', methods=['GET'])
def get_product(pid):
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute(
            'SELECT p.*, c.name as category_name FROM products p '
            'LEFT JOIN categories c ON p.category_id=c.id WHERE p.id=%s', (pid,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
    return jsonify({'success': True, 'data': _product_to_dict(row)})


@shop_admin_bp.route('/products/<int:pid>/preview', methods=['GET'])
def admin_preview_product(pid):
    """预览商品 — 绕过 is_active 检查"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute(
            'SELECT p.*, c.name as category_name FROM products p '
            'LEFT JOIN categories c ON p.category_id=c.id WHERE p.id=%s', (pid,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
    return jsonify({'success': True, 'data': _product_to_dict(row)})


@shop_admin_bp.route('/products', methods=['POST'])
def create_product():
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    required = ['title', 'price']
    for f in required:
        if f not in data:
            return jsonify({'success': False, 'error': f'Missing required field: {f}'}), 400

    if len(str(data.get('title', ''))) > _MAX_TITLE:
        return jsonify({'success': False, 'error': f'Title cannot exceed {_MAX_TITLE} characters'}), 400
    if len(str(data.get('description', ''))) > _MAX_DESC:
        return jsonify({'success': False, 'error': f'Description cannot exceed {_MAX_DESC} characters'}), 400

    images = data.get('images', [])
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except:
            images = []

    # 价格与库存校验：类型安全 + 非负
    try:
        price = float(data.get('price', 0))
        original_price = float(data.get('original_price', 0))
        stock = int(data.get('stock', 0))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': _('Price or stock format is invalid')}), 400
    if price < 0 or original_price < 0 or stock < 0:
        return jsonify({'success': False, 'error': _('Price and stock cannot be negative')}), 400

    status = data.get('status', 'draft')
    if status not in ('draft', 'active', 'archived'):
        status = 'draft'
    slug = data.get('slug', '').strip() or _gen_slug(data.get('title', ''))
    meta_title = data.get('meta_title', '').strip() or data.get('title', '')
    meta_description = data.get('meta_description', '').strip() or data.get('subtitle', '')
    with get_db() as conn:
        # slug 冲突时自动递增后缀（slug / slug-1 / slug-2 ...）
        base_slug = slug
        i = 1
        while conn.execute('SELECT 1 FROM products WHERE slug=%s', (slug,)).fetchone():
            slug = f'{base_slug}-{i}'
            i += 1
        pid = conn.execute(
            '''INSERT INTO products (title, subtitle, product_type, category,
               category_id, price, original_price, stock, thumbnail, description,
               features, images, ai_config, sort_order, is_active, status,
               slug, meta_title, meta_description)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (
                data.get('title', ''),
                data.get('subtitle', ''),
                data.get('product_type', 'service'),
                data.get('category', ''),
                int(data.get('category_id', 0)),
                price,
                original_price,
                stock,
                data.get('thumbnail', ''),
                data.get('description', ''),
                json.dumps(data.get('features', []), ensure_ascii=False),
                json.dumps(images, ensure_ascii=False),
                json.dumps(data.get('ai_config', {}), ensure_ascii=False),
                int(data.get('sort_order', 0)),
                1 if status == 'active' else 0,
                status,
                slug,
                meta_title,
                meta_description
            )).fetchone()['id']
    return jsonify({'success': True, 'data': {'id': pid}, 'message': _('Product has been created')})


@shop_admin_bp.route('/products/<int:pid>', methods=['PUT'])
def update_product(pid):
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    if not data:
        return jsonify({'success': False, 'error': _('No Update Data')}), 400

    # 价格/库存字段预校验：类型安全 + 非负
    for f in ('price', 'original_price', 'stock'):
        if f in data:
            try:
                v = float(data[f]) if f != 'stock' else int(data[f])
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': _('Price or stock format is invalid')}), 400
            if v < 0:
                return jsonify({'success': False, 'error': _('Price and stock cannot be negative')}), 400

    fields = ['title', 'subtitle', 'product_type', 'category',
              'category_id', 'price', 'original_price', 'stock', 'thumbnail',
              'description', 'sort_order', 'slug', 'meta_title', 'meta_description']
    sets = []
    vals = []
    for f in fields:
        if f in data:
            sets.append(f'{f}=%s')
            vals.append(data[f])
    # 状态与 is_active 双向同步（status ↔ is_active）
    if 'status' in data:
        status_val = data['status'] if data['status'] in ('draft', 'active', 'archived') else 'draft'
        sets.append('status=%s')
        vals.append(status_val)
        sets.append('is_active=%s')
        vals.append(1 if status_val == 'active' else 0)
    elif 'is_active' in data:
        new_active = 1 if int(data['is_active']) == 1 else 0
        sets.append('is_active=%s')
        vals.append(new_active)
        sets.append('status=%s')
        vals.append('active' if new_active else 'draft')
    if 'features' in data:
        sets.append('features=%s')
        vals.append(json.dumps(data['features'], ensure_ascii=False))
    if 'images' in data:
        imgs = data['images']
        if isinstance(imgs, str):
            try:
                imgs = json.loads(imgs)
            except:
                imgs = []
        sets.append('images=%s')
        vals.append(json.dumps(imgs, ensure_ascii=False))
    if 'ai_config' in data:
        sets.append('ai_config=%s')
        vals.append(json.dumps(data['ai_config'], ensure_ascii=False))
    if not sets:
        return jsonify({'success': False, 'error': _('No Valid Update Fields')}), 400

    sets.append("updated_at=NOW()")
    vals.append(pid)
    with get_db() as conn:
        conn.execute(f'UPDATE products SET {",".join(sets)} WHERE id=%s', vals)
        conn.commit()
        _log_admin_action(conn, payload['user_id'], 'update', 'product', pid,
                          json.dumps({k: data[k] for k in data if k in fields}, ensure_ascii=False))
    return jsonify({'success': True, 'message': _('Product has been updated')})


@shop_admin_bp.route('/products/<int:pid>', methods=['DELETE'])
def delete_product(pid):
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute('SELECT images FROM products WHERE id=%s', (pid,)).fetchone()
        if row:
            images = _safe_json(row['images'], [])
            for img in images:
                url = img.get('url', '') if isinstance(img, dict) else str(img)
                if url.startswith('/static/products/'):
                    fpath = os.path.join(_UPLOAD_DIR, os.path.basename(url))
                    if os.path.exists(fpath):
                        os.remove(fpath)
        # 清理关联数据
        conn.execute('DELETE FROM product_specs WHERE product_id=%s', (pid,))
        conn.execute('DELETE FROM product_spec_values WHERE spec_id IN (SELECT id FROM product_specs WHERE product_id=%s)', (pid,))
        conn.execute('DELETE FROM product_skus WHERE product_id=%s', (pid,))
        conn.execute('DELETE FROM products WHERE id=%s', (pid,))
        conn.commit()
        _log_admin_action(conn, payload['user_id'], 'delete', 'product', pid)
    return jsonify({'success': True, 'message': _('Product has been deleted')})


# =============================================
# 商品规格管理 (Specs)
# =============================================
@shop_admin_bp.route('/products/<int:pid>/specs', methods=['GET'])
def list_specs(pid):
    """获取商品规格列表（含规格值）"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        specs = conn.execute(
            'SELECT * FROM product_specs WHERE product_id=%s ORDER BY sort_order ASC', (pid,)
        ).fetchall()
        result = []
        for s in specs:
            sd = dict(s)
            vals = conn.execute(
                'SELECT * FROM product_spec_values WHERE spec_id=%s ORDER BY sort_order ASC', (s['id'],)
            ).fetchall()
            sd['values'] = [dict(v) for v in vals]
            result.append(sd)
    return jsonify({'success': True, 'data': result})


@shop_admin_bp.route('/products/<int:pid>/specs', methods=['POST'])
def create_spec(pid):
    """添加规格名"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    name = data.get('spec_name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': _('Specification name cannot be empty')}), 400
    with get_db() as conn:
        sid = conn.execute(
            'INSERT INTO product_specs (product_id, spec_name, sort_order) VALUES (%s,%s,%s) RETURNING id',
            (pid, name, int(data.get('sort_order', 0)))
        ).fetchone()['id']
        conn.commit()
    return jsonify({'success': True, 'data': {'id': sid}, 'message': _('Specification has been added')})


@shop_admin_bp.route('/products/<int:pid>/specs/<int:sid>', methods=['PUT'])
def update_spec(pid, sid):
    """修改规格名"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    name = data.get('spec_name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': _('Specification name cannot be empty')}), 400
    with get_db() as conn:
        conn.execute('UPDATE product_specs SET spec_name=%s, sort_order=%s WHERE id=%s AND product_id=%s',
                     (name, int(data.get('sort_order', 0)), sid, pid))
        conn.commit()
    return jsonify({'success': True, 'message': _('Specification has been updated')})


@shop_admin_bp.route('/products/<int:pid>/specs/<int:sid>', methods=['DELETE'])
def delete_spec(pid, sid):
    """删除规格及所有规格值"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        conn.execute('DELETE FROM product_spec_values WHERE spec_id=%s', (sid,))
        conn.execute('DELETE FROM product_specs WHERE id=%s AND product_id=%s', (sid, pid))
        conn.commit()
    return jsonify({'success': True, 'message': _('Specification has been deleted')})


# ── 规格值管理 ──
@shop_admin_bp.route('/products/<int:pid>/specs/<int:sid>/values', methods=['POST'])
def create_spec_value(pid, sid):
    """添加规格值"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    value = data.get('spec_value', '').strip()
    if not value:
        return jsonify({'success': False, 'error': _('Specification value cannot be empty')}), 400
    with get_db() as conn:
        vid = conn.execute(
            'INSERT INTO product_spec_values (spec_id, spec_value, sort_order) VALUES (%s,%s,%s) RETURNING id',
            (sid, value, int(data.get('sort_order', 0)))
        ).fetchone()['id']
        conn.commit()
    return jsonify({'success': True, 'data': {'id': vid}, 'message': _('Specification value has been added')})


@shop_admin_bp.route('/products/<int:pid>/specs/values/<int:vid>', methods=['PUT'])
def update_spec_value(pid, vid):
    """修改规格值"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    value = data.get('spec_value', '').strip()
    if not value:
        return jsonify({'success': False, 'error': _('Specification value cannot be empty')}), 400
    with get_db() as conn:
        conn.execute('UPDATE product_spec_values SET spec_value=%s, sort_order=%s WHERE id=%s',
                     (value, int(data.get('sort_order', 0)), vid))
        conn.commit()
    return jsonify({'success': True, 'message': _('Specification value has been updated')})


@shop_admin_bp.route('/products/<int:pid>/specs/values/<int:vid>', methods=['DELETE'])
def delete_spec_value(pid, vid):
    """删除规格值"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        conn.execute('DELETE FROM product_spec_values WHERE id=%s', (vid,))
        conn.commit()
    return jsonify({'success': True, 'message': _('Specification value has been deleted')})


# =============================================
# SKU 管理
# =============================================
@shop_admin_bp.route('/products/<int:pid>/skus', methods=['GET'])
def list_skus(pid):
    """获取商品SKU列表"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM product_skus WHERE product_id=%s ORDER BY id ASC', (pid,)
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@shop_admin_bp.route('/products/<int:pid>/skus/generate', methods=['POST'])
def generate_skus(pid):
    """
    根据规格组合自动生成SKU
    例如：颜色[红,蓝] × 尺寸[S,L] → 4个SKU
    """
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    try:
        base_price = float(data.get('base_price', 0))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': _('Invalid price format')}), 400
    if base_price < 0:
        return jsonify({'success': False, 'error': _('Price cannot be negative')}), 400

    with get_db() as conn:
        # 获取所有规格及其值
        specs = conn.execute(
            'SELECT * FROM product_specs WHERE product_id=%s ORDER BY sort_order ASC', (pid,)
        ).fetchall()
        if not specs:
            return jsonify({'success': False, 'error': _('Please add specifications first')}), 400

        spec_values = {}
        for s in specs:
            vals = conn.execute(
                'SELECT * FROM product_spec_values WHERE spec_id=%s ORDER BY sort_order ASC', (s['id'],)
            ).fetchall()
            if not vals:
                return jsonify({'success': False, 'error': f'Specification "{s["spec_name"]}" is missing a value'}), 400
            spec_values[s['id']] = {
                'name': s['spec_name'],
                'values': [dict(v) for v in vals]
            }

    # 笛卡尔积生成所有组合
    value_lists = [sv['values'] for sv in spec_values.values()]
    spec_ids = list(spec_values.keys())

    from itertools import product
    combinations = list(product(*value_lists))

    created_skus = []
    with get_db() as conn:
        for combo in combinations:
            spec_path = {}
            parts = []
            for i, v in enumerate(combo):
                spec_name = spec_values[spec_ids[i]]['name']
                spec_path[spec_name] = v['spec_value']
                parts.append(v['spec_value'])
            sku_code = f"SKU-{pid}-{'-'.join(parts)}"

            # 检查是否已存在
            existing = conn.execute(
                'SELECT id FROM product_skus WHERE product_id=%s AND sku_code=%s', (pid, sku_code)
            ).fetchone()
            if existing:
                continue

            sku_id = conn.execute(
                'INSERT INTO product_skus (product_id, sku_code, spec_path, price, stock) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                (pid, sku_code, json.dumps(spec_path, ensure_ascii=False), base_price, 0)
            ).fetchone()['id']
            created_skus.append({'id': sku_id, 'sku_code': sku_code, 'spec_path': spec_path,
                                'price': base_price, 'stock': 0})
        conn.commit()

    return jsonify({
        'success': True,
        'data': {'skus': created_skus, 'total': len(created_skus)},
        'message': f'Generated {len(created_skus)} SKUs'
    })


@shop_admin_bp.route('/products/<int:pid>/skus/<int:skuid>', methods=['PUT'])
def update_sku(pid, skuid):
    """修改SKU信息（价格/库存/图片）"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    fields = ['price', 'stock', 'image', 'is_active']
    sets = []
    vals = []
    for f in fields:
        if f in data:
            sets.append(f'{f}=%s')
            vals.append(data[f])
    if not sets:
        return jsonify({'success': False, 'error': _('No Update Data')}), 400
    sets.append("updated_at=NOW()")
    vals.append(skuid)
    with get_db() as conn:
        conn.execute(f'UPDATE product_skus SET {",".join(sets)} WHERE id=%s AND product_id=%s', vals + [pid])
        conn.commit()
    return jsonify({'success': True, 'message': _('SKU Updated')})


@shop_admin_bp.route('/products/<int:pid>/skus/<int:skuid>', methods=['DELETE'])
def delete_sku(pid, skuid):
    """删除SKU"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        conn.execute('DELETE FROM product_skus WHERE id=%s AND product_id=%s', (skuid, pid))
        conn.commit()
    return jsonify({'success': True, 'message': _('SKU Deleted')})


# =============================================
# 商品分类管理 (Categories)
# =============================================
@shop_admin_bp.route('/categories', methods=['GET'])
def list_categories():
    """获取分类树"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM shop.categories ORDER BY level ASC, sort_order ASC, id ASC'
        ).fetchall()

    # 构建树形结构
    cats = [dict(r) for r in rows]
    tree = []
    cat_map = {}
    for c in cats:
        c['children'] = []
        cat_map[c['id']] = c
    for c in cats:
        if c['parent_id'] and c['parent_id'] in cat_map:
            cat_map[c['parent_id']]['children'].append(c)
        else:
            tree.append(c)

    return jsonify({'success': True, 'data': {'tree': tree, 'list': cats}})


@shop_admin_bp.route('/categories', methods=['POST'])
def create_category():
    """创建分类"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': _('Category name cannot be empty')}), 400
    if len(name) > _MAX_CATEGORY:
        return jsonify({'success': False, 'error': _('Category name is too long')}), 400
    parent_id = int(data.get('parent_id', 0))
    level = 0
    if parent_id:
        with get_db() as conn:
            parent = conn.execute('SELECT level FROM shop.categories WHERE id=%s', (parent_id,)).fetchone()
            if parent:
                level = parent['level'] + 1

    slug = data.get('slug', '').strip() or name.lower().replace(' ', '-')
    if len(slug) > _MAX_CATEGORY:
        return jsonify({'success': False, 'error': _('Category name is too long')}), 400
    with get_db() as conn:
        try:
            # 防重复：同父级下已存在同名分类（不区分大小写）时拒绝创建，避免重复分类
            dup = conn.execute(
                'SELECT id FROM shop.categories WHERE LOWER(name)=LOWER(%s) AND parent_id=%s LIMIT 1',
                (name, parent_id)
            ).fetchone()
            if dup:
                return jsonify({'success': False, 'error': _('A category with this name already exists')}), 400
            # slug 冲突时自动递增后缀（slug / slug-1 / slug-2 ...），避免撞唯一约束
            base_slug = slug
            i = 1
            while conn.execute('SELECT 1 FROM shop.categories WHERE slug=%s', (slug,)).fetchone():
                slug = f'{base_slug}-{i}'
                i += 1
            cid = conn.execute(
                'INSERT INTO shop.categories (name, slug, parent_id, level, icon, sort_order, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                (name, slug, parent_id, level, data.get('icon', ''), int(data.get('sort_order', 0)), 1)
            ).fetchone()['id']
            conn.commit()
        except Exception as e:
            return jsonify({'success': False, 'error': f'Creation failed: {e}'}), 400
    return jsonify({'success': True, 'data': {'id': cid}, 'message': _('Category created')})


@shop_admin_bp.route('/categories/<int:cid>', methods=['PUT'])
def update_category(cid):
    """修改分类"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    fields = ['name', 'slug', 'icon', 'sort_order', 'is_active', 'parent_id']
    sets = []
    vals = []
    for f in fields:
        if f in data:
            sets.append(f'{f}=%s')
            vals.append(data[f])
    if not sets:
        return jsonify({'success': False, 'error': _('No Update Data')}), 400
    # 如果更新了 parent_id，重算 level
    if 'parent_id' in data:
        parent_id = int(data.get('parent_id', 0))
        level = 0
        if parent_id:
            with get_db() as conn:
                parent = conn.execute('SELECT level FROM shop.categories WHERE id=%s', (parent_id,)).fetchone()
                if parent:
                    level = parent['level'] + 1
        sets.append('level=%s')
        vals.append(level)
    sets.append("updated_at=NOW()")
    vals.append(cid)
    with get_db() as conn:
        conn.execute(f'UPDATE shop.categories SET {",".join(sets)} WHERE id=%s', vals)
        conn.commit()
    return jsonify({'success': True, 'message': _('Category updated')})


@shop_admin_bp.route('/categories/<int:cid>', methods=['DELETE'])
def delete_category(cid):
    """删除分类"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        # 检查是否有子分类
        children = conn.execute('SELECT id FROM shop.categories WHERE parent_id=%s', (cid,)).fetchall()
        if children:
            return jsonify({'success': False, 'error': _('Please delete child categories first')}), 400
        # 检查是否有商品使用此分类
        prods = conn.execute('SELECT id FROM products WHERE category_id=%s LIMIT 1', (cid,)).fetchall()
        if prods:
            return jsonify({'success': False, 'error': _('Cannot delete as there are items in this category')}), 400
        conn.execute('DELETE FROM shop.categories WHERE id=%s', (cid,))
        conn.commit()
    return jsonify({'success': True, 'message': _('Category Deleted')})
# =============================================
# AI 智能优化 — 直接使用 AIEngine，支持 DeepSeek/阿里百炼/硅基流动/OpenAI等
# =============================================
class ShopAIProcessor:
    """商城AI内容处理器，内置 Prompt 模板，无需外部依赖"""

    SYSTEM_PROMPT = '你是一个专业的电商文案优化专家，擅长优化商品标题和描述，使其更具吸引力和营销力。'

    def __init__(self):
        self.engine = None
        self.provider = None
        self.model = None
        self.lang = get_lang() or ''
        self._init_engine()

    def _read_config(self, key, default=''):
        try:
            with get_db() as conn:
                row = conn.execute("SELECT value FROM system_config WHERE key=%s", (key,)).fetchone()
                return row['value'] if row and row['value'] else default
        except Exception:
            return default

    def _init_engine(self):
        """使用 system_config 配置初始化 AIEngine"""
        try:
            self.provider = self._read_config('shop_ai_provider', 'deepseek')
            from models.database import get_active_model
            _, default_model, _ = get_active_model(self.provider)
            self.model = self._read_config('shop_ai_model', default_model or '')
            if not self.model:
                self.model = default_model or ''

            from agent_matrix.engine import UnifiedLLM
            agent_config = {
                'provider': self.provider,
                'model_name': self.model,
                'api_key_ref': f'{self.provider}_api_key',
                'system_prompt': self.SYSTEM_PROMPT,
            }
            self.engine = UnifiedLLM(agent_config)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.engine = None

    def _call_ai(self, prompt, max_tokens=2048, temperature=0.7):
        """调用 LLM，返回 (成功, 内容)；配置/服务不可用类异常置 _ai_config_error=True"""
        self._ai_config_error = False
        if not self.engine:
            self._ai_config_error = True
            return False, _('AI engine not initialized, please check shop_ai_provider/shop_ai_model and API Key in system_config')
        lang_hint = '请使用中文回复。' if str(self.lang).lower().startswith('zh') else 'Respond in English.'
        try:
            content = self.engine.chat(
                messages=[
                    {'role': 'system', 'content': self.SYSTEM_PROMPT},
                    {'role': 'user', 'content': prompt + '\n\n' + lang_hint},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                module='shop_ai',
            )
            if content:
                return True, content.strip()
            return False, _('AI response is empty')
        except Exception as e:
            # 配置/网关类错误（模型/provider/API Key/网络/配额）→ 上层按 AI 服务不可用(503)处理
            msg = str(e)
            self._ai_config_error = isinstance(e, (ValueError, RuntimeError)) or any(
                k in msg.lower() for k in ('not found', 'api key', 'timeout', 'timed out',
                                           'connection', 'unauthorized', 'forbidden',
                                           'rate limit', 'quota', 'unavailable', 'invalid'))
            return False, msg

    def is_ready(self):
        return self.engine is not None

    # ── 标题优化（多版本） ──
    def generate_title_options(self, product_info):
        """生成 3 个风格不同的标题选项"""
        original_title = product_info.get('title', '')
        if not original_title:
            return False, _('Original title cannot be empty')

        prompt = f'''你是一个电商标题优化专家。请根据以下商品信息，生成 3 个优化后的商品标题。

原始标题：{original_title}
商品描述：{product_info.get('description', _('None'))[:200]}
商品类目：{product_info.get('category', _('None'))}

要求：
1. 标题长度 20-40 字
2. 包含核心卖点和关键词
3. SEO 友好，适合电商平台搜索
4. 3 个标题风格不同：①专业型  ②吸引力型  ③简洁型

请以 JSON 格式返回，不要包含任何其他文本：
[{{"id":1,"title":_("Title 1"),"style":"professional","reason":_("Select reason")}},{{"id":2,"title":_("Title 2"),"style":"appealing","reason":_("Select reason")}},{{"id":3,"title":_("Title 3"),"style":"concise","reason":_("Select reason")}}]'''

        success, response = self._call_ai(prompt, max_tokens=4096, temperature=0.8)
        if not success:
            return False, response

        import re
        try:
            options = json.loads(response)
            if not isinstance(options, list):
                if '[' in response and ']' in response:
                    options = json.loads(response[response.index('['):response.rindex(']') + 1])
                else:
                    return False, _('AI response format is invalid, failed to parse JSON')
            result = []
            for opt in options:
                result.append({
                    'id': opt.get('id', len(result) + 1),
                    'title': opt.get('title', ''),
                    'style': opt.get('style', 'normal'),
                    'reason': opt.get('reason', ''),
                })
            return True, result
        except (json.JSONDecodeError, Exception) as e:
            return False, f'Failed to parse AI return result: {e}'

    # ── 描述优化 ──
    def optimize_description(self, original_description, product_features=None):
        """重写商品描述，突出卖点"""
        if not original_description or not original_description.strip():
            return False, _('Original description cannot be empty')

        prompt = f'''你是一个电商描述优化专家。请优化以下商品描述：

原始描述：{original_description}

要求：
1. 保持核心信息完整
2. 突出产品卖点和优势
3. 语言生动有感染力，适合电商平台展示
4. 使用段落结构，200-500 字
5. 无需包含标题，直接输出描述正文'''

        if product_features and product_features.get('specs'):
            prompt += f'\n\n商品特征/规格：{product_features["specs"]}'

        success, optimized = self._call_ai(prompt, max_tokens=1500, temperature=0.6)
        if success and optimized:
            optimized = optimized.strip().strip('"').strip("'")
        return success, optimized

    # ── 卖点生成 ──
    def _generate_selling_points(self, product_info):
        """生成 3-5 个核心卖点"""
        specs = product_info.get('specs', [])
        specs_text = ', '.join(str(s) for s in specs) if isinstance(specs, list) else str(specs)

        prompt = f'''请为以下商品生成 3-5 个核心卖点：

商品名称：{product_info.get('title', '')}
商品描述：{product_info.get('description', '')[:300]}
{(_('Specification: ') + specs_text) if specs_text else ''}

要求：
1. 每个卖点一句话，简洁有力
2. 突出差异化优势
3. 从用户角度出发，强调利益而非功能
4. 适合在商品详情页展示

请以 JSON 数组格式返回，不要包含其他文本：
[_("Feature 1"),_("Feature 2"),_("Feature 3")]'''

        success, response = self._call_ai(prompt, max_tokens=500, temperature=0.6)
        if not success:
            return False, []

        import re
        try:
            points = json.loads(response)
            if not isinstance(points, list):
                if '[' in response and ']' in response:
                    points = json.loads(response[response.index('['):response.rindex(']') + 1])
                else:
                    points = []
            return True, [p.strip() for p in points if p.strip()][:5]
        except (json.JSONDecodeError, Exception):
            # Fallback: 按行解析
            points = []
            for line in response.split('\n'):
                line = line.strip().lstrip('- •*·').strip()
                if line and len(line) > 5:
                    points.append(line)
            return (True, points[:5]) if points else (False, [])

    # ── 标签生成 ──
    def _generate_tags(self, product_info):
        """生成 5-8 个相关标签"""
        desc = product_info.get('description', '') or ''
        prompt = f'''请为以下商品生成 5-8 个相关标签：

商品名称：{product_info.get('title', '')}
商品类目：{product_info.get('category', _('None'))}
描述：{desc[:100]}

要求：标签需覆盖商品核心属性、功能、使用场景。

请以 JSON 数组格式返回：
[_("Tag 1"),_("Tag 2"),_("Tag 3")]'''

        success, response = self._call_ai(prompt, max_tokens=200, temperature=0.5)
        if not success:
            return False, []

        try:
            tags = json.loads(response)
            if isinstance(tags, list):
                return True, [t.strip() for t in tags if t.strip()][:8]
        except (json.JSONDecodeError, Exception):
            tags = [t.strip().strip('"[]\'') for t in response.replace('"', '').split(',')]
            return True, [t for t in tags if t][:8]
        return False, []


def _get_ai_processor():
    """获取商城AI处理器实例 — 使用 AIEngine，支持 DeepSeek/阿里百炼/硅基流动/OpenAI/OpenRouter/Ollama"""
    proc = ShopAIProcessor()
    return proc if proc.is_ready() else None




@shop_admin_bp.route('/products/<int:pid>/ai-optimize', methods=['POST'])
def ai_optimize_product(pid):
    """AI全量优化：标题 + 描述 + 卖点 + 标签"""
    payload, err = _require_admin()
    if err:
        return err

    proc = _get_ai_processor()
    if not proc or not proc.engine:
        return jsonify({'success': False, 'error': _('AI service unavailable, please check API Key configuration')}), 503

    with get_db() as conn:
        row = conn.execute('SELECT * FROM products WHERE id=%s', (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        product = _product_to_dict(row)

    title = product.get('title', '')
    desc = product.get('description', '')
    category = product.get('category', '')
    features_list = product.get('features', [])
    features_str = ' '.join(features_list) if features_list else ''

    result = {
        'optimized_title': '',
        'title_options': [],
        'optimized_description': '',
        'selling_points': [],
        'tags': [],
    }

    try:
        # 1. 标题优化（多版本）
        if title:
            success, title_options = proc.generate_title_options({
                'title': title,
                'category': category,
                'description': features_str or desc[:200],
            })
            if success and title_options:
                result['title_options'] = title_options
                result['optimized_title'] = title_options[0]['title']

        # 2. 描述优化
        if desc:
            success, opt_desc = proc.optimize_description(desc, {'specs': features_str})
            if success:
                result['optimized_description'] = opt_desc

        # 3. 卖点生成
        if title or features_str:
            success, points = proc._generate_selling_points({
                'title': title,
                'description': features_str or desc[:300],
                'specs': features_list,
            })
            if success:
                result['selling_points'] = points

        # 4. 标签生成
        if title:
            success, tags = proc._generate_tags({
                'title': title,
                'category': category,
                'description': desc[:200] if desc else features_str,
            })
            if success:
                result['tags'] = tags

        _log_admin_action(payload.get('user_id', 0), 'ai_optimize', 'product', pid,
                          f'AI Optimization: {title[:30]}...')
        return jsonify({'success': True, 'data': result})

    except Exception as e:
        return jsonify({'success': False, 'error': f'AI Optimization Failed: {str(e)}'}), 500


@shop_admin_bp.route('/products/<int:pid>/ai-title', methods=['POST'])
def ai_optimize_title(pid):
    """AI单功能：标题多版本生成"""
    payload, err = _require_admin()
    if err:
        return err

    proc = _get_ai_processor()
    if not proc or not proc.engine:
        return jsonify({'success': False, 'error': _('AI Service Unavailable')}), 503

    with get_db() as conn:
        row = conn.execute('SELECT * FROM products WHERE id=%s', (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        product = _product_to_dict(row)

    try:
        features_str = ' '.join(product.get('features', []))
        success, options = proc.generate_title_options({
            'title': product.get('title', ''),
            'category': product.get('category', ''),
            'description': features_str or (product.get('description', '')[:200]),
        })
        if not success:
            if getattr(proc, '_ai_config_error', False):
                return jsonify({'success': False, 'error': _('AI Service Unavailable')}), 503
            return jsonify({'success': False, 'error': options or _('AI Title Generation Failed')}), 500
        return jsonify({'success': True, 'data': {'options': options}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@shop_admin_bp.route('/products/<int:pid>/ai-description', methods=['POST'])
def ai_optimize_description(pid):
    """AI单功能：描述重写"""
    payload, err = _require_admin()
    if err:
        return err

    proc = _get_ai_processor()
    if not proc or not proc.engine:
        return jsonify({'success': False, 'error': _('AI Service Unavailable')}), 503

    with get_db() as conn:
        row = conn.execute('SELECT * FROM products WHERE id=%s', (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        product = _product_to_dict(row)

    data = request.get_json() or {}
    custom_desc = data.get('description', '') or product.get('description', '')
    if not custom_desc:
        return jsonify({'success': False, 'error': _('No describable content to optimize')}), 400

    try:
        features_str = ' '.join(product.get('features', []))
        success, optimized = proc.optimize_description(custom_desc, {'specs': features_str})
        if not success:
            return jsonify({'success': False, 'error': optimized or _('AI Description Optimization Failed')}), 500
        return jsonify({'success': True, 'data': {'description': optimized}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@shop_admin_bp.route('/products/<int:pid>/ai-features', methods=['POST'])
def ai_generate_features(pid):
    """AI单功能：生成卖点列表"""
    payload, err = _require_admin()
    if err:
        return err

    proc = _get_ai_processor()
    if not proc or not proc.engine:
        return jsonify({'success': False, 'error': _('AI Service Unavailable')}), 503

    with get_db() as conn:
        row = conn.execute('SELECT * FROM products WHERE id=%s', (pid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Product does not exist')}), 404
        product = _product_to_dict(row)

    try:
        success, points = proc._generate_selling_points({
            'title': product.get('title', ''),
            'description': product.get('description', '')[:300],
            'specs': product.get('features', []),
        })
        if not success:
            return jsonify({'success': False, 'error': _('AI Selling Point Generation Failed')}), 500
        return jsonify({'success': True, 'data': {'features': points}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@shop_admin_bp.route('/products/ai-batch', methods=['POST'])
def ai_batch_optimize():
    """批量AI优化多个商品"""
    payload, err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    product_ids = data.get('product_ids', [])
    optimize_type = data.get('type', 'all')  # all / title / description / features

    if not product_ids or len(product_ids) > 20:
        return jsonify({'success': False, 'error': _('Please select 1-20 products')}), 400

    proc = _get_ai_processor()
    if not proc or not proc.engine:
        return jsonify({'success': False, 'error': _('AI Service Unavailable')}), 503

    results = []
    with get_db() as conn:
        placeholders = ','.join(['%s'] * len(product_ids))
        rows = conn.execute(
            f'SELECT * FROM products WHERE id IN ({placeholders})',
            product_ids
        ).fetchall()

    for row in rows:
        p = _product_to_dict(row)
        item = {'product_id': p['id'], 'title': p.get('title', '')}
        features_str = ' '.join(p.get('features', []))
        try:
            if optimize_type in ('all', 'title'):
                s, opts = proc.generate_title_options({
                    'title': p.get('title', ''),
                    'category': p.get('category', ''),
                    'description': features_str or (p.get('description', '')[:200]),
                })
                if s and opts:
                    item['optimized_title'] = opts[0]['title']
                    item['title_options'] = opts

            if optimize_type in ('all', 'description') and p.get('description'):
                s, opt_desc = proc.optimize_description(
                    p['description'], {'specs': features_str}
                )
                if s:
                    item['optimized_description'] = opt_desc

            if optimize_type in ('all', 'features'):
                s, points = proc._generate_selling_points({
                    'title': p.get('title', ''),
                    'description': p.get('description', '')[:300],
                    'specs': p.get('features', []),
                })
                if s:
                    item['selling_points'] = points

            item['success'] = True
        except Exception as e:
            item['success'] = False
            item['error'] = str(e)

        results.append(item)

    _log_admin_action(payload.get('user_id', 0), 'ai_batch_optimize', 'product',
                      ','.join(str(x) for x in product_ids),
                      f'Batch AI Optimization ({optimize_type}): {len(results)} items')
    return jsonify({'success': True, 'data': {'results': results, 'total': len(results)}})


@shop_admin_bp.route('/products/ai-generate', methods=['POST'])
def ai_generate_product():
    """AI从零生成商品文案草稿（标题+描述+卖点+标签）"""
    payload, err = _require_admin()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': _('Product name is required')}), 400

    proc = _get_ai_processor()
    if not proc or not proc.is_ready():
        return jsonify({'success': False, 'error': _('AI Service Unavailable')}), 503

    lang = '中文' if str(proc.lang).lower().startswith('zh') else 'English'
    prompt = f'''You are an e-commerce copywriting expert.
Respond in {lang}. Return ONLY valid JSON:
{{"title": "...", "description": "...", "features": ["...", "..."], "tags": ["..."]}}
Product name: {name}
Category: {(data.get('category') or '').strip()}
Keywords: {(data.get('keywords') or '').strip()}'''

    success, raw = proc._call_ai(prompt, max_tokens=1500, temperature=0.7)
    if not success:
        return jsonify({'success': False, 'error': raw or _('AI Title Generation Failed')}), 502

    try:
        draft = json.loads(raw)
    except Exception:
        return jsonify({'success': False, 'error': _('AI response format is invalid, failed to parse JSON')}), 502

    draft['title'] = (draft.get('title') or name)[:60]
    draft['description'] = (draft.get('description') or '')[:2000]
    draft['features'] = [f for f in draft.get('features', []) if f][:5]
    draft['tags'] = [t for t in draft.get('tags', []) if t][:8]

    _log_admin_action(payload.get('user_id', 0), 'ai_generate', 'product', 0, f'AI Generate: {name[:30]}...')
    return jsonify({'success': True, 'data': {'draft': draft}})


# =============================================
# 优惠券管理（已迁移至插件: plugins/coupons/）
# =============================================


# =============================================
# 订单管理
# =============================================
@shop_admin_bp.route('/orders', methods=['GET'])
def list_orders():
    payload, err = _require_admin()
    if err:
        return err
    status = request.args.get('status', '')
    page = request.args.get('page', 1, type=int) or 1
    page_size = request.args.get('page_size', 20, type=int) or 20
    page_size = min(max(page_size, 1), 100)
    offset = (page - 1) * page_size
    with get_db() as conn:
        where = ''
        params = []
        if status:
            where = ' WHERE oi.status=%s'
            params.append(status)
        total = conn.execute(
            f'SELECT COUNT(*) AS c FROM order_items oi{where}', params
        ).fetchone()['c']
        sql = f'''SELECT oi.*, u.username, u.phone, p.title as prod_title
                 FROM order_items oi
                 LEFT JOIN users u ON oi.user_id=u.id
                 LEFT JOIN products p ON oi.product_id=p.id
                 {where} ORDER BY oi.created_at DESC LIMIT %s OFFSET %s'''
        rows = conn.execute(sql, params + [page_size, offset]).fetchall()
    data = []
    for r in rows:
        d = dict(r)
        d['shipping_status_text'] = ''
        if d.get('shipping_status') == 'shipped':
            _logistics = _get_plugin_instance('logistics')
            if _logistics:
                d['shipping_status_text'] = _logistics.get_shipping_status_text(d['shipping_status'])
        data.append(d)
    return jsonify({'success': True, 'data': data, 'total': total,
                    'page': page, 'page_size': page_size})


@shop_admin_bp.route('/orders/export', methods=['GET'])
def export_orders():
    """P2：订单 CSV 导出（UTF-8 BOM，Excel 兼容）"""
    payload, err = _require_admin()
    if err:
        return err
    headers = ['id', 'order_no', 'user_id', 'username', 'phone', 'product_id',
               'product_title', 'quantity', 'unit_price', 'sku_id', 'sku_price',
               'subtotal', 'discount', 'total', 'status', 'shipping_status',
               'receiver_name', 'receiver_phone', 'receiver_address',
               'payment_method', 'note', 'created_at', 'paid_at']
    with get_db() as conn:
        rows = conn.execute(
            '''SELECT oi.*, u.username, u.phone FROM order_items oi
               LEFT JOIN users u ON oi.user_id=u.id ORDER BY oi.id'''
        ).fetchall()
    data = [[
        r['id'], r['order_id'] or '', r['user_id'] or '', r['username'] or '',
        r['phone'] or '', r['product_id'] or '', r['product_title'] or '',
        r['quantity'] or '', r['unit_price'] or 0, r['sku_id'] or 0,
        r['sku_price'] or 0, r['subtotal'] or 0, r['discount'] or 0,
        r['total'] or 0, r['status'] or '', r['shipping_status'] or '',
        r['receiver_name'] or '', r['receiver_phone'] or '',
        r['receiver_address'] or '', r['payment_method'] or '',
        r['note'] or '', (r['created_at'] or '').isoformat() if r['created_at'] else '',
        (r['paid_at'] or '').isoformat() if r['paid_at'] else ''
    ] for r in rows]
    return _export_csv(headers, data, 'shop_orders.csv')


@shop_admin_bp.route('/orders/<int:oid>/detail', methods=['GET'])
def order_detail(oid):
    """订单详情 — 含商品快照、支付记录、物流信息"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute(
            '''SELECT oi.*, u.username, u.phone, u.display_name,
                      p.title as prod_title, p.thumbnail as prod_thumb,
                      p.price as prod_price
               FROM order_items oi
               LEFT JOIN users u ON oi.user_id=u.id
               LEFT JOIN products p ON oi.product_id=p.id
               WHERE oi.id=%s''', (oid,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404

        d = dict(row)

        # 支付事件记录
        payments = conn.execute(
            "SELECT * FROM payment_events WHERE order_no=%s ORDER BY created_at DESC",
            (d.get('order_no') or d.get('id'),)
        ).fetchall()
        d['payments'] = [dict(p) for p in payments]

        # 物流信息
        d['shipping'] = None
        if d.get('shipping_status') == 'shipped':
            try:
                tracking = conn.execute(
                    "SELECT * FROM order_shipping WHERE order_item_id=%s ORDER BY created_at DESC",
                    (oid,)
                ).fetchall()
                d['shipping'] = [dict(t) for t in tracking] if tracking else None
            except Exception:
                d['shipping'] = None

    return jsonify({'success': True, 'data': d})


@shop_admin_bp.route('/orders/<int:oid>/note', methods=['PUT'])
def update_order_note(oid):
    """P2：订单备注 — 更新管理端订单备注"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    note = (data.get('note') or '').strip()
    if len(note) > 2000:
        return jsonify({'success': False, 'error': _('Note is too long')}), 400
    with get_db() as conn:
        cur = conn.execute('UPDATE shop.order_items SET note=%s WHERE id=%s', (note, oid))
        if cur.rowcount == 0:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        conn.commit()
    return jsonify({'success': True, 'message': _('Note saved')})


@shop_admin_bp.route('/orders/<int:oid>/confirm', methods=['POST'])
def confirm_order(oid):
    """确认订单支付 → 自动触发云服务开通"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute('SELECT * FROM order_items WHERE id=%s', (oid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if row['status'] != 'pending':
            return jsonify({'success': False, 'error': _('Only confirm pending payment orders')}), 400
        conn.execute(
            "UPDATE order_items SET status='paid', paid_at=NOW() WHERE id=%s",
            (oid,))
        # 添加 user_purchases 记录
        conn.execute('''INSERT INTO user_purchases
            (user_id, product_id, order_id, purchase_type, status, created_at)
            VALUES (%s,%s,%s,%s,%s,NOW())''',
            (row['user_id'], row['product_id'], row['order_id'], 'once', 'active'))
        conn.commit()
        _log_admin_action(conn, payload['user_id'], 'confirm_payment', 'order', oid,
                          f'product_id={row["product_id"]} user_id={row["user_id"]}')

    # ── 云服务自动开通（已移除）──

    return jsonify({'success': True, 'message': _('Payment confirmed')})


@shop_admin_bp.route('/orders/<int:oid>/refund', methods=['POST'])
def refund_order(oid):
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    with get_db() as conn:
        row = conn.execute('SELECT * FROM order_items WHERE id=%s', (oid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if row['status'] == 'refunded':
            return jsonify({'success': False, 'error': _('Order refunded')}), 400
        if row['status'] not in ('paid', 'shipped', 'refunding'):
            return jsonify({'success': False, 'error': _('Current order status does not allow refund')}), 400

        payment_method = row['payment_method'] or ''
        payment_trade_no = row['payment_trade_no'] or ''
        amount = int((row['subtotal'] - row.get('discount', 0)) * 100)  # 转换为分

        # 如果有关联的支付交易号，尝试调用网关退款
        if payment_trade_no:
            try:
                refund_result = {'success': False, 'error': 'Unknown payment method'}
                if payment_method == 'alipay':
                    from plugins.payment.gateways.alipay import refund_order as _alipay_refund
                    refund_result = _alipay_refund(row['order_id'], amount)
                elif payment_method == 'wechat':
                    from plugins.payment.gateways.wechat import refund_order as _wechat_refund
                    refund_result = _wechat_refund(row['order_id'], amount)
                elif payment_method == 'stripe':
                    from plugins.payment.gateways.stripe import refund_order as _stripe_refund
                    refund_result = _stripe_refund(payment_trade_no, amount)
                elif payment_method == 'paypal':
                    from plugins.payment.gateways.paypal import refund_order as _paypal_refund
                    refund_result = _paypal_refund(payment_trade_no, amount)

                if not refund_result.get('success'):
                    err_msg = refund_result.get('error', 'Gateway refund failed')
                    logger.error(f'[Shop Refund] Gateway error for order {oid}: {err_msg}')
                    # P1-3 资金一致性：网关退款失败即中止，DB 状态不变，等待管理员重试/线下处理
                    return jsonify({'success': False, 'error': f"{_('Refund failed at gateway')}: {err_msg}"}), 502
            except Exception as e:
                logger.error(f'[Shop Refund] Gateway call failed for order {oid}: {e}')
                return jsonify({'success': False, 'error': f"{_('Refund failed at gateway')}: {str(e)}"}), 502

        # VR-SHOP-001：PostgreSQL 无标量 MAX，销量钳位用 GREATEST
        conn.execute('UPDATE products SET sales_count = GREATEST(0, sales_count - %s) WHERE id=%s',
                     (row['quantity'], row['product_id']))
        # 回补商品与 SKU 库存
        conn.execute('UPDATE products SET stock=stock+%s WHERE id=%s',
                     (row['quantity'], row['product_id']))
        if row.get('sku_id'):
            conn.execute('UPDATE product_skus SET stock=stock+%s WHERE id=%s',
                         (row['quantity'], row['sku_id']))
        conn.execute(
            "UPDATE order_items SET status='refunded', refunded_at=NOW() WHERE id=%s",
            (oid,)
        )
        conn.execute(
            "UPDATE user_purchases SET status='cancelled', expire_at=NOW() "
            "WHERE product_id=%s AND user_id=%s AND status='active'",
            (row['product_id'], row['user_id'])
        )
        conn.commit()
        _log_admin_action(conn, payload['user_id'], 'refund', 'order', oid,
                          f'product_id={row["product_id"]} user_id={row["user_id"]} reason={data.get("reason","")}')
    return jsonify({'success': True, 'message': _('Refunded')})


@shop_admin_bp.route('/orders/<int:oid>/complete', methods=['POST'])
def complete_order_admin(oid):
    """管理员标记订单为已完成"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        row = conn.execute('SELECT * FROM order_items WHERE id=%s', (oid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if row['status'] not in ('paid', 'shipped'):
            return jsonify({'success': False, 'error': _('Current order status does not allow marking as completed')}), 400
        conn.execute(
            "UPDATE order_items SET status='completed', completed_at=NOW() WHERE id=%s",
            (oid,)
        )
        conn.commit()
        _log_admin_action(conn, payload['user_id'], 'complete', 'order', oid, '')
    return jsonify({'success': True, 'message': _('Marked as completed')})


# =============================================
# 物流发货
# =============================================
@shop_admin_bp.route('/express-companies', methods=['GET'])
def list_express_companies():
    """快递公司列表"""
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        rows = conn.execute(
            'SELECT code, name FROM express_companies WHERE is_active=1 ORDER BY sort_order'
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})


@shop_admin_bp.route('/orders/<int:oid>/ship', methods=['POST'])
def ship_order(oid):
    """发货：填写快递公司和单号"""
    payload, err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    company = (data.get('company') or '').strip()
    tracking = (data.get('tracking_number') or '').strip()
    if not company or not tracking:
        return jsonify({'success': False, 'error': _('Please select a shipping company and enter the tracking number')}), 400

    with get_db() as conn:
        row = conn.execute('SELECT * FROM order_items WHERE id=%s', (oid,)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if row['status'] != 'paid':
            return jsonify({'success': False, 'error': _('Only ship paid orders')}), 400
        if row.get('shipping_status') == 'shipped':
            return jsonify({'success': False, 'error': _('This order has been shipped')}), 400

        conn.execute(
            "UPDATE order_items SET tracking_company=%s, tracking_number=%s, "
            "shipping_status='shipped', shipped_at=NOW() WHERE id=%s",
            (company, tracking, oid)
        )
        conn.commit()
        _log_admin_action(conn, payload['user_id'], 'ship_order', 'order', oid,
                          f'company={company} tracking={tracking}')
    # 触发事件：发货
    try:
        from plugin_manager.event_bus import get_event_bus, EventName
        get_event_bus().emit(EventName.ORDER_SHIPPED, order_id=row.get('order_id', oid),
                             user_id=row['user_id'], company=company, tracking_number=tracking)
    except Exception:
        pass

    return jsonify({'success': True, 'message': f'Marked as shipped ({company}: {tracking})'})


@shop_admin_bp.route('/orders/<int:oid>/track', methods=['GET'])
def track_order(oid):
    """查询物流轨迹"""
    payload, err = _require_admin()
    if err:
        return err

    with get_db() as conn:
        row = conn.execute(
            'SELECT oi.*, ec.kdniao_code FROM order_items oi '
            'LEFT JOIN express_companies ec ON oi.tracking_company=ec.code '
            'WHERE oi.id=%s', (oid,)
        ).fetchone()
        if not row:
            return jsonify({'success': False, 'error': _('Order does not exist')}), 404
        if not row.get('tracking_number'):
            return jsonify({'success': False, 'error': _('This order has not been shipped')}), 400

        shipper_code = row['kdniao_code'] or row['tracking_company']
        logistic_code = row['tracking_number']

    # 调用物流插件查询
    success, data, err_msg = False, {}, _('Logistics plugin is not enabled')
    _logistics = _get_plugin_instance('logistics')
    if _logistics:
        success, data, err_msg = _logistics.query_track(shipper_code, logistic_code)

    if not success:
        # 返回基础发货信息 + 错误提示
        return jsonify({
            'success': True,
            'data': {
                'tracking_company': row['tracking_company'],
                'tracking_number': row['tracking_number'],
                'shipped_at': row.get('shipped_at', ''),
                'shipping_status': row.get('shipping_status', ''),
                'traces': [],
                'track_error': err_msg,
            }
        })

    return jsonify({
        'success': True,
        'data': {
            'tracking_company': row['tracking_company'],
            'tracking_number': row['tracking_number'],
            'shipped_at': row.get('shipped_at', ''),
            'shipping_status': row.get('shipping_status', ''),
            'traces': data.get('traces', []),
            'state': data.get('state', 0),
            'state_text': data.get('state_text', ''),
        }
    })


# =============================================
# 购买记录
# =============================================
@shop_admin_bp.route('/purchases', methods=['GET'])
def list_purchases():
    payload, err = _require_admin()
    if err:
        return err
    with get_db() as conn:
        rows = conn.execute(
            '''SELECT up.*, u.username, u.phone, p.title as prod_title
               FROM user_purchases up
               LEFT JOIN users u ON up.user_id=u.id
               LEFT JOIN products p ON up.product_id=p.id
               ORDER BY up.created_at DESC LIMIT 100'''
        ).fetchall()
    return jsonify({'success': True, 'data': [dict(r) for r in rows]})
