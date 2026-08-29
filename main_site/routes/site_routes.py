#!/usr/bin/env python3
"""Site Routes — Multi-tenant site routing"""

import os
import json
from flask import Blueprint, request, jsonify, render_template, redirect, make_response
from models import get_db
from services.jwt_service import validate_token

site_bp = Blueprint('site', __name__)


def _get_site_tiers():
    """从 subscription_plans 读取建站套餐（零硬编码）"""
    tiers = {}
    try:
        from models import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT item_key AS plan_key, COALESCE(name_zh, name_en) AS name, "
                "COALESCE(description_zh, description_en) AS description, "
                "price_month, price_year, tier, features_json "
                "FROM subscription.sub_items WHERE is_active=1 AND item_key LIKE 'site_%' ORDER BY sort_order"
            ).fetchall()
        for r in rows:
            try:
                feats = json.loads(r['features_json'])
            except:
                feats = []
            tiers[r['plan_key'].replace('site_', '')] = {
                'name': r['name'],
                'price': r['price_month'] // 100,  # 分→元
                'features': feats,
                'color': 'gray' if r['tier'] == 'free' else ('purple' if r['tier'] == 'pro' else 'blue'),
            }
    except:
        pass
    return tiers


def _get_site_by_domain(domain):
    host = domain.lower().split(':')[0]
    with get_db() as conn:
        row = conn.execute("SELECT * FROM site_configs WHERE domain=%s", (host,)).fetchone()
        return dict(row) if row else None


def _get_site_by_slug(slug):
    deploy_domain = os.environ.get('DEPLOY_DOMAIN', '')
    if not deploy_domain:
        # fallback: read from first site_config
        with get_db() as conn:
            row = conn.execute("SELECT domain FROM site_configs LIMIT 1").fetchone()
            deploy_domain = row['domain'] if row else ''
    domain_map = {
        'tm': deploy_domain,
        'subscription': deploy_domain,
        'ecommerce': deploy_domain,
    }
    domain = domain_map.get(slug, slug)
    return _get_site_by_domain(domain)


def _get_site_blocks(site_id, page='home'):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM site_blocks WHERE site_id=%s AND page=%s AND is_published=1 ORDER BY position",
            (site_id, page)
        ).fetchall()
        blocks = [dict(r) for r in rows]
        for b in blocks:
            try:
                b['extra_json'] = json.loads(b.get('extra_json', '{}'))
            except:
                b['extra_json'] = {}
        return blocks


def _get_site_plans(site_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM site_plans WHERE site_id=%s AND is_published=1 ORDER BY sort_order",
            (site_id,)
        ).fetchall()
        plans = [dict(r) for r in rows]
        for p in plans:
            try:
                p['features_list'] = json.loads(p.get('features', '[]'))
            except:
                p['features_list'] = []
        return plans


def _inject_site_context(site):
    blocks = _get_site_blocks(site['id'])
    plans = _get_site_plans(site['id'])
    tiers = _get_site_tiers()
    return {
        'site': site,
        'blocks': blocks,
        'plans': plans,
        'tiers': tiers,
    }


# 多租户路由配置
# 不要在此定义 /、/pricing、/features、/contact 等静态路由 — 与主 app.py 路由冲突
# 保留 <slug>/ 动态路由用于后续多租户建站功能


@site_bp.route('/api/site/config')
def site_config_api():
    domain = request.headers.get('Host', '')
    site = _get_site_by_domain(domain)
    
    if not site:
        return jsonify({'error': 'Site not found'}), 404
    
    return jsonify({
        'success': True,
        'data': {
            'name': site['name'],
            'domain': site['domain'],
            'industry': site['industry'],
            'theme_color': site['theme_color'],
            'accent_color': site['accent_color'],
            'tier': site['tier'],
        }
    })


@site_bp.route('/api/site/blocks')
def site_blocks_api():
    domain = request.headers.get('Host', '')
    site = _get_site_by_domain(domain)
    
    if not site:
        return jsonify({'error': 'Site not found'}), 404
    
    page = request.args.get('page', 'home')
    blocks = _get_site_blocks(site['id'], page)
    
    return jsonify({'success': True, 'data': blocks})


@site_bp.route('/api/site/plans')
def site_plans_api():
    domain = request.headers.get('Host', '')
    site = _get_site_by_domain(domain)
    
    if not site:
        return jsonify({'error': 'Site not found'}), 404
    
    plans = _get_site_plans(site['id'])
    return jsonify({'success': True, 'data': plans})


@site_bp.route('/<slug>/')
def site_home_slug(slug):
    site = _get_site_by_slug(slug)
    if not site:
        return jsonify({'error': f'Site "{slug}" not found'}), 404
    context = _inject_site_context(site)
    return render_template('site_home.html', **context)


@site_bp.route('/<slug>/pricing')
def site_pricing_slug(slug):
    site = _get_site_by_slug(slug)
    if not site:
        return jsonify({'error': f'Site "{slug}" not found'}), 404
    context = _inject_site_context(site)
    return render_template('site_pricing.html', **context)


@site_bp.route('/<slug>/features')
def site_features_slug(slug):
    site = _get_site_by_slug(slug)
    if not site:
        return jsonify({'error': f'Site "{slug}" not found'}), 404
    context = _inject_site_context(site)
    return render_template('site_features.html', **context)


@site_bp.route('/<slug>/contact')
def site_contact_slug(slug):
    site = _get_site_by_slug(slug)
    if not site:
        return jsonify({'error': f'Site "{slug}" not found'}), 404
    context = _inject_site_context(site)
    return render_template('site_contact.html', **context)


@site_bp.route('/<slug>/api/config')
def site_config_api_slug(slug):
    site = _get_site_by_slug(slug)
    if not site:
        return jsonify({'error': f'Site "{slug}" not found'}), 404
    return jsonify({
        'success': True,
        'data': {
            'name': site['name'],
            'domain': site['domain'],
            'industry': site['industry'],
            'theme_color': site['theme_color'],
            'accent_color': site['accent_color'],
            'tier': site['tier'],
        }
    })


def init_site_seeds():
    with get_db() as conn:
        pass  # 种子数据已统一管理