# VeroRun 维洛智能 (verorun.com / verorun.cn)
# 版权所有 (c) 2026 樊聚民 (fanjumin). All Rights Reserved.

#!/usr/bin/env python3
"""CMS Public Routes — pages served at VeroRun 维洛智能"""
from flask import Blueprint, render_template, redirect, request, send_from_directory, jsonify
from models.cms import get_page_blocks, get_posts, get_post_by_slug, get_setting, get_categories
from models.cms import get_downloads, get_download_by_slug

cms_bp = Blueprint('cms', __name__)


@cms_bp.route('/start')
def start():
    return render_template('start.html')


@cms_bp.route('/api/v1/insights/latest')
def insights_latest():
    posts = get_posts(limit=3, audience='public')
    return jsonify({"posts": posts, "count": len(posts)})


@cms_bp.route('/api/v1/categories')
def public_categories():
    """Return public active categories as JSON for widget rendering."""
    cats = get_categories(active_only=True, audience='public')
    return jsonify({"categories": cats, "count": len(cats)})


_PAGES = {
    'home': 'home',
    'brand': 'brand',
    'services': 'services',
    'download': 'download',
    'docs': 'docs',
}


def _render_page(page_id: str):
    blocks = get_page_blocks(page_id)
    if page_id == 'home' and not blocks:
        return redirect('/login')
    # Pre-parse extra_json for template use
    import json
    blocks2 = []
    for b in blocks:
        d = dict(b)
        try:
            d['extra_json'] = json.loads(d.get('extra_json', '{}'))
        except:
            d['extra_json'] = {}
        blocks2.append(d)
    # Query header nav
    header_nav = []
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT title, url FROM header_nav WHERE site='platform' AND is_enabled=1 ORDER BY sort_order ASC"
            ).fetchall()
            header_nav = [dict(r) for r in rows]
    except:
        pass
    return render_template('cms_page.html', page=page_id, blocks=blocks2, header_nav=header_nav)


@cms_bp.route('/brand')
def brand():
    return _render_page('brand')


@cms_bp.route('/services')
def services():
    """服务详情页 — CMS Hero + DB驱动服务卡片"""
    blocks = get_page_blocks('services')
    import json as _json
    hero_blocks = []
    for b in blocks:
        d = dict(b)
        try:
            d['extra_json'] = _json.loads(d.get('extra_json', '{}'))
        except Exception:
            d['extra_json'] = {}
        hero_blocks.append(d)
    
    header_nav = _get_header_nav()
    
    # Query structured data
    services_list = []
    comparison = []
    faq_list = []
    process_steps = []
    site_plans = []
    try:
        from models import get_db
        with get_db() as conn:
            srows = conn.execute("SELECT * FROM site_services WHERE is_active=1 ORDER BY sort_order").fetchall()
            for sr in srows:
                sd = dict(sr)
                features = conn.execute("SELECT feature FROM site_service_features WHERE service_id=%s ORDER BY sort_order", (sd['id'],)).fetchall()
                sd['features'] = [f[0] for f in features]
                services_list.append(sd)
            
            comp_rows = conn.execute("SELECT * FROM site_plan_comparison ORDER BY sort_order").fetchall()
            comparison = [dict(r) for r in comp_rows]
            
            faq_rows = conn.execute("SELECT * FROM site_faq WHERE is_active=1 ORDER BY sort_order").fetchall()
            faq_list = [dict(r) for r in faq_rows]
            
            step_rows = conn.execute("SELECT * FROM site_process_steps ORDER BY sort_order").fetchall()
            process_steps = [dict(r) for r in step_rows]
            
            pr = conn.execute(
                "SELECT item_key AS plan_key, COALESCE(name_zh, name_en) AS name, "
                "COALESCE(description_zh, description_en) AS description, "
                "price_year, price_month, tier, features_json "
                "FROM subscription.sub_items WHERE is_active=1 AND item_key LIKE 'site_%' ORDER BY sort_order"
            ).fetchall()
            for r in pr:
                d = dict(r)
                d['price_year'] = d['price_year'] // 100
                d['renewal_fee'] = d['price_month'] // 100
                d['setup_fee'] = d['price_year'] - d['renewal_fee']
                try: d['features'] = _json.loads(d.get('features_json', '[]'))
                except: d['features'] = []
                site_plans.append(d)
    except Exception as e:
        print(f'[services] DB query error: {e}')
    
    return render_template('services.html',
                           hero_blocks=hero_blocks, header_nav=header_nav,
                           services=services_list, comparison=comparison,
                           faq_list=faq_list, process_steps=process_steps,
                           site_plans=site_plans, page='services')


@cms_bp.route('/cases')
def cases():
    """案例展示页 — DB驱动"""
    blocks = get_page_blocks('cases')
    import json as _json
    hero_blocks = []
    for b in blocks:
        d = dict(b)
        try: d['extra_json'] = _json.loads(d.get('extra_json', '{}'))
        except: d['extra_json'] = {}
        hero_blocks.append(d)
    
    header_nav = _get_header_nav()
    
    case_list = []
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM site_cases WHERE is_active=1 ORDER BY sort_order").fetchall()
            case_list = [dict(r) for r in rows]
    except: pass
    
    return render_template('cases.html', hero_blocks=hero_blocks, header_nav=header_nav,
                           cases=case_list, page='cases')





@cms_bp.route('/ai-experience')
def ai_experience():
    """AI体验页"""
    blocks = get_page_blocks('ai-experience')
    import json as _json
    hero_blocks = []
    for b in blocks:
        d = dict(b)
        try: d['extra_json'] = _json.loads(d.get('extra_json', '{}'))
        except: d['extra_json'] = {}
        hero_blocks.append(d)
    
    header_nav = _get_header_nav()
    return render_template('ai_experience.html', hero_blocks=hero_blocks,
                           header_nav=header_nav, page='ai-experience')


def _get_header_nav():
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT title, url FROM header_nav WHERE site='platform' AND is_enabled=1 ORDER BY sort_order ASC"
            ).fetchall()
            return [dict(r) for r in rows]
    except:
        return []


@cms_bp.route('/download', strict_slashes=False)
@cms_bp.route('/download/')
def download():
    """下载首页 — 展示所有已发布下载项，保留CMS Hero区"""
    blocks = get_page_blocks('download')
    import json as _json
    hero_blocks = []
    for b in blocks:
        d = dict(b)
        try:
            d['extra_json'] = _json.loads(d.get('extra_json', '{}'))
        except Exception:
            d['extra_json'] = {}
        hero_blocks.append(d)
    
    cat_filter = request.args.get('cat', '')
    downloads = get_downloads(category=cat_filter if cat_filter else None)
    
    # Header nav
    header_nav = []
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT title, url FROM header_nav WHERE site='platform' AND is_enabled=1 ORDER BY sort_order ASC"
            ).fetchall()
            header_nav = [dict(r) for r in rows]
    except Exception:
        pass
    
    return render_template('download_list.html',
                           page='download', hero_blocks=hero_blocks,
                           downloads=downloads, current_cat=cat_filter,
                           header_nav=header_nav)


@cms_bp.route('/download/<slug>')
def download_detail(slug):
    """下载详情页"""
    item = get_download_by_slug(slug)
    if not item:
        return render_template('cms_404.html'), 404
    
    header_nav = []
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT title, url FROM header_nav WHERE site='platform' AND is_enabled=1 ORDER BY sort_order ASC"
            ).fetchall()
            header_nav = [dict(r) for r in rows]
    except Exception:
        pass
    
    return render_template('download_detail.html',
                           page='download', item=item, header_nav=header_nav)


@cms_bp.route('/docs', strict_slashes=False)
@cms_bp.route('/docs/')
def docs():
    """文档首页 — 展示所有公开分类及最新文章"""
    cats = get_categories(active_only=True, audience='public')
    return render_template('docs_index.html', categories=cats)


# ── /docs/<cat_slug>/ ── class listing ──

@cms_bp.route('/docs/<cat_slug>/')
def docs_category(cat_slug):
    """按分类列出公开文章"""
    cats = get_categories(active_only=True, audience='public')
    matched = [c for c in cats if c.get('slug') == cat_slug]
    if not matched:
        return render_template('cms_404.html'), 404
    cat = matched[0]
    posts = get_posts(category=cat['name'], audience='public', limit=20)
    return render_template('docs_list.html', category=cat, posts=posts, all_categories=cats)


# ── /docs/<cat_slug>/<slug> ── single article ──

@cms_bp.route('/docs/<cat_slug>/<slug>')
def docs_detail(cat_slug, slug):
    """单篇文章详情（仅公开）"""
    post = get_post_by_slug(slug)
    if not post or post.get('audience') != 'public':
        return render_template('cms_404.html'), 404
    return render_template('docs_detail.html', post=post)


# ── /legal/<slug> ── legal docs ──

@cms_bp.route('/legal/<slug>')
def legal_page(slug):
    """法律合规页面"""
    post = get_post_by_slug(slug)
    if not post or post.get('audience') != 'public' or post.get('category') != '法律合规':
        return render_template('cms_404.html'), 404
    return render_template('docs_detail.html', post=post)


@cms_bp.route('/insights')
def insights():
    posts = get_posts(category='产品动态', audience='public', limit=20)
    return render_template('insights_list.html', posts=posts, category='产品动态')


@cms_bp.route('/insights/<slug>')
def insight_detail(slug):
    post = get_post_by_slug(slug)
    if not post:
        return render_template('cms_404.html'), 404
    return render_template('insights_detail.html', post=post)



