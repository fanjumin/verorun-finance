#!/usr/bin/env python3
"""Enterprise Verification Plugin — 用户端 API 路由"""
import sys, os, json

_auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'auth-center')
if _auth_dir not in sys.path:
    sys.path.insert(0, _auth_dir)

from flask import Blueprint, request, jsonify

ev_user_bp = Blueprint('enterprise_verify_user', __name__, url_prefix='/user/enterprise/verify')


def _require_auth():
    """复用主系统的用户鉴权"""
    from routes.user import _require_auth as _ra
    return _ra()


def _get_main_db():
    """获取主系统数据库连接"""
    from models import get_db
    return get_db()


def _get_ev_db():
    """获取插件数据库连接"""
    from plugins.enterprise_verify.models import get_ev_db
    return get_ev_db()


# ── POST /user/enterprise/verify/ocr ──
@ev_user_bp.route('/ocr', methods=['POST'])
def enterprise_verify_ocr():
    """上传营业执照图片 → 硅基流动 OCR 识别 → 返回结构化结果"""
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']

    data = request.get_json(force=True) or {}
    image_base64 = data.get('image', '')

    if not image_base64:
        return jsonify({'success': False, 'error': 'Please upload license image'}), 400

    try:
        from plugins.enterprise_verify.services import ocr_business_license
        result = ocr_business_license(image_base64)
    except Exception as e:
        return jsonify({'success': False, 'error': f'Business License OCR failed: {str(e)}'}), 500

    return jsonify({
        'success': True,
        'data': {
            'company_name': result.get('company_name', ''),
            'reg_num': result.get('reg_num', ''),
            'legal_person': result.get('legal_person', ''),
            'address': result.get('address', ''),
            'registered_capital': result.get('registered_capital', ''),
            'business_scope': result.get('business_scope', ''),
        }
    })


# ── POST /user/enterprise/verify/submit ──
@ev_user_bp.route('/submit', methods=['POST'])
def enterprise_verify_submit():
    """用户确认 OCR 识别结果后提交企业认证，AI 自动审核"""
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']

    data = request.get_json(force=True) or {}
    company_name = (data.get('company_name') or '').strip()
    tax_id = (data.get('reg_num') or '').strip().replace(' ', '').upper()
    address = (data.get('address') or '').strip()
    legal_person = (data.get('legal_person') or '').strip()

    if not company_name or not tax_id:
        return jsonify({'success': False, 'error': 'Company name and Unified Social Credit Code are required'}), 400

    # 检查是否已有待审/已通过记录
    with _get_ev_db() as ev_conn:
        existing = ev_conn.execute(
            "SELECT id, status FROM enterprise_verifications WHERE user_id=? AND status!='rejected'",
            (user_id,)
        ).fetchone()
        if existing:
            if existing['status'] == 'approved':
                return jsonify({'success': False, 'error': 'You are already verified'}), 400
            return jsonify({'success': False, 'error': 'You already have a pending verification request'}), 400

    ocr_raw = json.dumps({
        'company_name': company_name,
        'tax_id': tax_id,
        'address': address,
        'legal_person': legal_person,
    }, ensure_ascii=False)

    from plugins.enterprise_verify.services import auto_audit
    audit = auto_audit(company_name, tax_id)

    status = audit['decision']
    review_notes = audit['reason']

    with _get_ev_db() as ev_conn:
        ev_conn.execute(
            """INSERT INTO enterprise_verifications
               (user_id, enterprise_name, tax_id, ocr_raw, status, review_notes, reviewed_at)
               VALUES (%s,%s,%s,%s,%s,%s, NOW())""",
            (user_id, company_name, tax_id, ocr_raw, status, review_notes)
        )
        ev_conn.commit()

    if status == 'approved':
        with _get_main_db() as conn:
            conn.execute(
                """UPDATE users SET
                   enterprise_name=%s, enterprise_tax_id=%s, enterprise_address=%s,
                   enterprise_verified=1, enterprise_verified_at=NOW()
                   WHERE id=%s""",
                (company_name, tax_id, address, user_id)
            )
            conn.commit()

    message = 'Enterprise Verified' if status == 'approve' else 'Verification submitted, pending admin review'

    return jsonify({
        'success': True,
        'message': message,
        'data': {
            'status': status,
            'enterprise_name': company_name,
            'tax_id': tax_id,
            'address': address,
            'legal_person': legal_person,
        }
    })


# ── GET /user/enterprise/verify/status ──
@ev_user_bp.route('/status', methods=['GET'])
def enterprise_verify_status():
    """查询当前用户的企业认证状态"""
    payload, err = _require_auth()
    if err:
        return err
    user_id = payload['user_id']

    with _get_main_db() as conn:
        user = conn.execute(
            "SELECT enterprise_name, enterprise_tax_id, enterprise_address, "
            "enterprise_verified, enterprise_verified_at FROM users WHERE id=%s",
            (user_id,)
        ).fetchone()

    with _get_ev_db() as ev_conn:
        latest = ev_conn.execute(
            "SELECT status, review_notes, created_at FROM enterprise_verifications "
            "WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()

    return jsonify({
        'success': True,
        'data': {
            'verified': bool(user['enterprise_verified']),
            'enterprise_name': user['enterprise_name'] or '',
            'enterprise_tax_id': user['enterprise_tax_id'] or '',
            'enterprise_address': user['enterprise_address'] or '',
            'enterprise_verified_at': user['enterprise_verified_at'] or '',
            'verification_status': latest['status'] if latest else 'none',
            'review_notes': latest['review_notes'] if latest else '',
            'submitted_at': latest['created_at'] if latest else '',
        }
    })