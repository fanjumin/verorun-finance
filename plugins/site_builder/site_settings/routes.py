#!/usr/bin/env python3
"""Site Settings Routes — 统一站点设置 API"""

import json

from flask import request, jsonify
from ..routes import _require_admin, _log
from .models import get_tokens, save_tokens, DEFAULT_TOKENS
from .token_service import validate_tokens, merge_tokens, get_token_schema
from .token_renderer import render_all


def register_routes(bp):
    """注册所有路由到蓝图"""

    @bp.route('/', methods=['GET'], strict_slashes=False)
    def get_site_settings():
        admin, err = _require_admin()
        if err: return err
        site_key = request.args.get('site', 'platform')
        data = get_tokens(site_key)
        return jsonify({'success': True, 'data': data})

    @bp.route('/', methods=['PUT'], strict_slashes=False)
    def update_site_settings():
        admin, err = _require_admin()
        if err: return err
        data = request.get_json(force=True) or {}
        site_key = data.get('site_key', 'platform')
        tokens = data.get('tokens')

        if not tokens:
            return jsonify({'success': False, 'error': 'tokens is required'}), 400

        valid, errors = validate_tokens(tokens)
        if not valid:
            return jsonify({'success': False, 'error': 'Validation failed', 'details': errors}), 400

        save_tokens(site_key, tokens, generated_by='manual')
        _log(admin['user_id'], 'update', 'site_settings', site_key, '')
        return jsonify({'success': True, 'message': 'Site settings updated'})

    @bp.route('//schema', methods=['GET'])
    def get_settings_schema():
        admin, err = _require_admin()
        if err: return err
        return jsonify({'success': True, 'data': get_token_schema()})

    @bp.route('//css', methods=['GET'])
    def get_settings_css():
        """获取渲染后的 CSS 变量"""
        site_key = request.args.get('site', 'platform')
        data = get_tokens(site_key)
        rendered = render_all(data['token_json'])
        return jsonify({'success': True, 'data': rendered})

    @bp.route('//render', methods=['GET'])
    def render_settings():
        """获取渲染后的完整 HTML 片段"""
        site_key = request.args.get('site', 'platform')
        data = get_tokens(site_key)
        rendered = render_all(data['token_json'])
        return jsonify({'success': True, 'data': rendered})

    @bp.route('//brand', methods=['GET'])
    def get_brand():
        admin, err = _require_admin()
        if err: return err
        data = get_tokens()
        return jsonify({'success': True, 'data': data['token_json'].get('brand', {})})

    @bp.route('//brand', methods=['PUT'])
    def update_brand():
        admin, err = _require_admin()
        if err: return err
        data = request.get_json(force=True) or {}
        tokens = get_tokens()
        current = tokens['token_json']
        current['brand'].update(data)
        save_tokens('platform', current)
        _log(admin['user_id'], 'update', 'site_settings_brand', '', str(list(data.keys())))
        return jsonify({'success': True, 'message': 'Brand updated'})

    @bp.route('//navigation', methods=['GET'])
    def get_navigation():
        admin, err = _require_admin()
        if err: return err
        data = get_tokens()
        return jsonify({'success': True, 'data': data['token_json'].get('navigation', {})})

    @bp.route('//navigation', methods=['PUT'])
    def update_navigation():
        admin, err = _require_admin()
        if err: return err
        data = request.get_json(force=True) or {}
        items = data.get('items')
        if not items:
            return jsonify({'success': False, 'error': 'items is required'}), 400
        tokens = get_tokens()
        current = tokens['token_json']
        current['navigation']['items'] = items
        save_tokens('platform', current)
        _log(admin['user_id'], 'update', 'site_settings_nav', '', f'{len(items)} items')
        return jsonify({'success': True, 'message': 'Navigation updated'})

    @bp.route('//footer', methods=['GET'])
    def get_footer():
        admin, err = _require_admin()
        if err: return err
        data = get_tokens()
        return jsonify({'success': True, 'data': data['token_json'].get('footer', {})})

    @bp.route('//footer', methods=['PUT'])
    def update_footer():
        admin, err = _require_admin()
        if err: return err
        data = request.get_json(force=True) or {}
        tokens = get_tokens()
        current = tokens['token_json']
        current['footer'].update(data)
        save_tokens('platform', current)
        _log(admin['user_id'], 'update', 'site_settings_footer', '', str(list(data.keys())))
        return jsonify({'success': True, 'message': 'Footer updated'})

    @bp.route('//colors', methods=['GET'])
    def get_colors():
        admin, err = _require_admin()
        if err: return err
        data = get_tokens()
        return jsonify({'success': True, 'data': data['token_json'].get('colors', {})})

    @bp.route('//colors', methods=['PUT'])
    def update_colors():
        admin, err = _require_admin()
        if err: return err
        data = request.get_json(force=True) or {}
        tokens = get_tokens()
        current = tokens['token_json']
        current['colors'].update(data)
        save_tokens('platform', current)
        _log(admin['user_id'], 'update', 'site_settings_colors', '', str(list(data.keys())))
        return jsonify({'success': True, 'message': 'Colors updated'})

    @bp.route('//typography', methods=['GET'])
    def get_typography():
        admin, err = _require_admin()
        if err: return err
        data = get_tokens()
        return jsonify({'success': True, 'data': data['token_json'].get('typography', {})})

    @bp.route('//typography', methods=['PUT'])
    def update_typography():
        admin, err = _require_admin()
        if err: return err
        data = request.get_json(force=True) or {}
        tokens = get_tokens()
        current = tokens['token_json']
        current['typography'].update(data)
        save_tokens('platform', current)
        _log(admin['user_id'], 'update', 'site_settings_typography', '', str(list(data.keys())))
        return jsonify({'success': True, 'message': 'Typography updated'})