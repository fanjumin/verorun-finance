#!/usr/bin/env python3
"""
Real-Name Verification Service — Provider abstraction layer.

核心合规要求（最高优先级）：
  - 身份证号仅在此模块的函数调用栈内存中临时存在。
  - 绝不写入数据库、日志文件、缓存或任何持久化存储。
  - 函数返回前 id_number 变量随栈帧销毁，gc 回收。

架构：
  POST /user/verification/apply   → VerificationService.initiate() → 返回第三方认证 URL
  POST /user/verification/callback → VerificationService.verify()   → 验签 + 回填 display_name

Provider 配置存储在 system_config 表中，管理员在后台填入后即可启用。
"""

import os, sys, hashlib, hmac, time, uuid, json, logging, urllib.parse, urllib.request, base64
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from i18n import _
from models import get_db, now_iso

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Provider 抽象基类
# ═══════════════════════════════════════════════════════════════════

class BaseVerificationProvider(ABC):
    """第三方实名认证 Provider 基类。

    子类实现时注意：id_number 只能在回调 verify() 方法的内存中使用，
    方法返回前必须确保引用已释放。"""

    @abstractmethod
    def get_config_prefix(self) -> str:
        """返回 system_config key 前缀，如 'verification.alipay'"""
        ...

    @abstractmethod
    def build_auth_url(self, request_id: str, return_url: str, **kwargs) -> str:
        """生成第三方认证跳转 URL。

        Args:
            request_id: 服务端生成的防重放流水号
            return_url: 认证完成后回跳地址
        Returns:
            第三方认证页面 URL
        """
        ...

    @abstractmethod
    def verify_signature(self, params: Dict[str, Any]) -> bool:
        """验证第三方回调签名。

        Args:
            params: 回调请求参数（GET query 或 POST body）
        Returns:
            True 表示签名有效
        """
        ...

    @abstractmethod
    def extract_real_name(self, params: Dict[str, Any]) -> str:
        """从回调参数中提取真实姓名。

        注意：id_number 在调用方（verify 方法）中提取并使用后立即丢弃，
        本方法只负责提取 real_name。
        """
        ...


# ═══════════════════════════════════════════════════════════════════
# Alipay Provider（骨架实现 — 配置项由管理员在后台填写）
# ═══════════════════════════════════════════════════════════════════

class AlipayVerificationProvider(BaseVerificationProvider):
    """支付宝实人认证 Provider。

    对接支付宝开放平台「身份认证」产品。
    管理员需在后台 system_config 中填入：
      - verification.alipay.app_id
      - verification.alipay.private_key      (PKCS8 格式)
      - verification.alipay.alipay_public_key
      - verification.alipay.return_url
    """

    GATEWAY = "https://openapi.alipay.com/gateway.do"

    def get_config_prefix(self) -> str:
        return "verification.alipay"

    def _get_config(self, key: str) -> str:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_config WHERE key=%s", (key,)
            ).fetchone()
        return row['value'] if row else ''

    def _ensure_pem_format(self, key_str: str, key_type: str = 'PRIVATE KEY') -> str:
        """确保密钥字符串有正确的 PEM 头尾标记。"""
        key_str = key_str.strip()
        begin_marker = f'-----BEGIN {key_type}-----'
        end_marker = f'-----END {key_type}-----'
        if not key_str.startswith('-----BEGIN '):
            key_str = begin_marker + '\n' + key_str + '\n' + end_marker
        return key_str

    def _sign_params(self, params: dict) -> str:
        """RSA2 签名：对参数按 key 排序后签名。"""
        private_key = self._get_config('verification.alipay.private_key')
        if not private_key:
            raise RuntimeError("支付宝私钥未配置，请在 system_config 中设置 verification.alipay.private_key")
        private_key = self._ensure_pem_format(private_key, 'PRIVATE KEY')

        sorted_keys = sorted(params.keys())
        sign_str = '&'.join([f'{k}={params[k]}' for k in sorted_keys])

        key = serialization.load_pem_private_key(private_key.encode('utf-8'), password=None)
        sig = key.sign(
            sign_str.encode('utf-8'),
            asym_padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _api_call(self, method: str, biz_content: dict) -> dict:
        """通用支付宝 API 调用（RSA2 签名 POST 请求）。"""
        app_id = self._get_config('verification.alipay.app_id')
        if not app_id:
            raise RuntimeError("支付宝 App ID 未配置")

        params = {
            'app_id': app_id,
            'method': method,
            'format': 'JSON',
            'charset': 'utf-8',
            'sign_type': 'RSA2',
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'version': '1.0',
            'biz_content': json.dumps(biz_content, ensure_ascii=True),
        }
        sign = self._sign_params(params)
        params['sign'] = sign

        data_bytes = '&'.join(
            [f'{k}={urllib.parse.quote(str(params[k]))}' for k in params]
        ).encode('utf-8')

        req = urllib.request.Request(self.GATEWAY, data=data_bytes)
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read()
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('gbk')
        logger.info(f"[支付宝API] {method} 原始响应: {text[:500]}")
        result = json.loads(text)
        return result

    def _query_certify_result(self, certify_id: str) -> dict:
        """调用 alipay.user.certify.open.query 查询认证结果。"""
        biz_content = {'certify_id': certify_id}
        result = self._api_call('alipay.user.certify.open.query', biz_content)
        query_resp = result.get('alipay_user_certify_open_query_response', {})
        return query_resp

    def build_auth_url(self, request_id: str, return_url: str, **kwargs) -> str:
        """构建支付宝身份认证跳转链接。

        完整流程：
          1. 服务端调用 alipay.user.certify.open.initialize（RSA2 签名）→ 获取 certify_id
          2. POST 调用 alipay.user.certify.open.certify → 捕获302 Location
          3. 返回 custweb.alipay.com/certify/... 真实认证URL
          4. 前端生成二维码，用户支付宝App扫码完成认证

        参数 cert_name / cert_no 通过 kwargs 传入，用于 CERT_INFO 验证模式。
        """
        app_id = self._get_config('verification.alipay.app_id')
        cfg_return = self._get_config('verification.alipay.return_url')
        final_return = return_url or cfg_return

        if not app_id:
            raise RuntimeError("支付宝 App ID 未配置，请在后台 system_config 中设置 verification.alipay.app_id")

        cert_name = kwargs.get('cert_name', '').strip()
        cert_no = kwargs.get('cert_no', '').strip()
        if not cert_name or not cert_no:
            raise RuntimeError("缺少实名认证必需信息：真实姓名和身份证号")

        # Step 1: 调用初始化接口获取 certify_id
        biz_content = {
            'outer_order_no': request_id,
            'biz_code': 'FACE',
            'identity_param': {
                'identity_type': 'CERT_INFO',
                'cert_type': 'IDENTITY_CARD',
                'cert_name': cert_name,
                'cert_no': cert_no,
            },
            'merchant_config': {
                'return_url': final_return or '',
            },
        }

        result = self._api_call('alipay.user.certify.open.initialize', biz_content)
        init_resp = result.get('alipay_user_certify_open_initialize_response', {})

        if init_resp.get('code') != '10000':
            logger.error(f"支付宝认证初始化失败: 完整响应={json.dumps(init_resp, ensure_ascii=False)[:500]}")
            err_msg = init_resp.get('sub_msg', init_resp.get('msg', '未知错误'))
            raise RuntimeError(f"支付宝认证初始化失败: {err_msg}")

        certify_id = init_resp.get('certify_id', '')
        if not certify_id:
            raise RuntimeError("支付宝认证初始化返回缺少 certify_id")

        # Step 2: POST调用certify接口，捕获302 Location（真实认证URL）
        certify_params = {
            'app_id': app_id,
            'method': 'alipay.user.certify.open.certify',
            'format': 'JSON',
            'charset': 'utf-8',
            'sign_type': 'RSA2',
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'version': '1.0',
            'biz_content': json.dumps({'certify_id': certify_id}, ensure_ascii=True),
        }
        certify_params['sign'] = self._sign_params(certify_params)

        # POST到网关，不跟随重定向，捕获Location（custweb.alipay.com/certify/...）
        import http.client as _http_client
        data_parts = [f'{k}={urllib.parse.quote(str(v))}' for k, v in certify_params.items()]
        post_data = '&'.join(data_parts).encode('utf-8')

        parsed = urllib.parse.urlparse(self.GATEWAY)
        conn = _http_client.HTTPSConnection(parsed.hostname, timeout=15)
        conn.request('POST', parsed.path, body=post_data, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        resp = conn.getresponse()
        location = resp.getheader('Location', '').strip()
        conn.close()

        if location:
            logger.info(f"[支付宝Certify] 认证URL: {location[:200]}")
            self._last_gateway = self.GATEWAY
            self._last_params = dict(certify_params)
            self._last_certify_url = location
            return location

        raw = resp.read()
        try:
            html_text = raw.decode('utf-8')
        except UnicodeDecodeError:
            html_text = raw.decode('gbk')
        logger.warning(f"[支付宝Certify] 无302跳转，body前300字符: {html_text[:300]}")
        raise RuntimeError("支付宝认证接口未返回有效跳转URL")

    def verify_signature(self, params: Dict[str, Any]) -> bool:
        """验证支付宝异步通知签名。使用 RSA2 公钥验签。"""
        try:
            sign = params.pop('sign', '')
            sign_type = params.pop('sign_type', 'RSA2')

            if not sign:
                logger.warning("支付宝验签失败: 签名为空")
                return False

            alipay_public_key = self._get_config('verification.alipay.alipay_public_key')
            if not alipay_public_key:
                logger.warning("支付宝验签失败: 未配置支付宝公钥")
                return False

            alipay_public_key = self._ensure_pem_format(alipay_public_key, 'PUBLIC KEY')

            unsigned = '&'.join(
                f'{k}={v}' for k, v in sorted(params.items())
                if k not in ('sign', 'sign_type')
            )

            public_key = serialization.load_pem_public_key(
                alipay_public_key.encode('utf-8')
            )
            signature = base64.b64decode(sign)
            public_key.verify(
                signature,
                unsigned.encode('utf-8'),
                asym_padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except Exception as e:
            logger.error(f"支付宝验签失败: {e}")
            return False

    def extract_real_name(self, params: Dict[str, Any]) -> str:
        """从支付宝回调参数中提取真实姓名。

        支付宝认证回调后，通过 alipay.user.certify.open.query API
        查询认证详情，从中提取 cert_name（真实姓名）。

        合规保证：cert_no（身份证号）仅在内存中使用，绝不持久化。
        """
        # 优先从回调参数直接获取（部分场景会在回调中返回）
        cert_name = (params.get('cert_name') or params.get('real_name') or '').strip()
        if cert_name:
            return cert_name

        # 从回调参数提取 certify_id，调用查询 API
        certify_id = (params.get('certify_id') or '').strip()
        if not certify_id:
            logger.warning("支付宝认证回调缺少 certify_id，无法查询认证结果")
            return ''

        try:
            query_resp = self._query_certify_result(certify_id)
            if query_resp.get('code') != '10000':
                logger.error(f"支付宝查询认证结果失败: {query_resp.get('sub_msg', query_resp.get('msg', ''))}")
                return ''

            if query_resp.get('passed') != 'T' and query_resp.get('passed') != 'true':
                logger.warning(f"支付宝认证未通过: passed={query_resp.get('passed')}")
                return ''

            # 提取姓名（合规：不存储 cert_no）
            cert_name = (query_resp.get('cert_name') or '').strip()

            # 身份证号仅用于验证一致性的日志脱敏（绝不存储）
            cert_no = query_resp.get('cert_no', '')
            if cert_no and len(cert_no) > 4:
                masked = cert_no[0] + '***' + cert_no[-1]
                logger.info(f"支付宝实名认证成功: name={cert_name}, cert_no(masked)={masked}")
            elif cert_no:
                logger.info(f"支付宝实名认证成功: name={cert_name}")

            return cert_name
        except Exception as e:
            logger.error(f"查询支付宝认证结果异常: {e}")
            return ''


# ═══════════════════════════════════════════════════════════════════
# Stub Provider（开发模式）
# ═══════════════════════════════════════════════════════════════════

class StubVerificationProvider(BaseVerificationProvider):
    """开发/测试用 Provider — 模拟第三方认证通过。

    当 system_config 中 verification.stub_mode=true 时使用。
    不发起真实 HTTP 请求，直接返回模拟成功结果。
    """

    def get_config_prefix(self) -> str:
        return "verification.stub"

    def build_auth_url(self, request_id: str, return_url: str, **kwargs) -> str:
        # 开发模式：直接跳到回调地址，模拟第三方认证通过
        # 合规：不将身份证号等 PII 放入 URL（浏览器历史/服务器日志/Referer 均会泄露）
        separator = '&' if '?' in return_url else '?'
        return (
            f"{return_url}{separator}"
            f"request_id={request_id}&"
            f"stub=true&"
            f"real_name=测试用户"
        )

    def verify_signature(self, params: Dict[str, Any]) -> bool:
        # 开发模式：跳过验签，检查 request_id 存在即可
        return bool(params.get('request_id'))

    def extract_real_name(self, params: Dict[str, Any]) -> str:
        return (params.get('real_name') or '测试用户').strip()


# ═══════════════════════════════════════════════════════════════════
# Provider 工厂
# ═══════════════════════════════════════════════════════════════════

_PROVIDER_REGISTRY = {
    'alipay': AlipayVerificationProvider,
    'stub': StubVerificationProvider,
}


def _get_config(key: str) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_config WHERE key=%s", (key,)
        ).fetchone()
    return row['value'] if row else ''


def get_provider() -> BaseVerificationProvider:
    """根据 system_config 配置返回对应的 Provider 实例。"""
    stub_mode = _get_config('verification.stub_mode') == 'true'
    if stub_mode:
        return StubVerificationProvider()

    provider_name = _get_config('verification.provider') or 'alipay'
    provider_cls = _PROVIDER_REGISTRY.get(provider_name)
    if not provider_cls:
        raise ValueError(f"未知的实名认证 Provider: {provider_name}")
    return provider_cls()


# ═══════════════════════════════════════════════════════════════════
# 核心服务方法
# ═══════════════════════════════════════════════════════════════════

def generate_request_id(user_id: int) -> str:
    """生成防重放认证流水号。"""
    ts = int(time.time() * 1000)
    short_uuid = uuid.uuid4().hex[:12]
    return f"rv_{user_id}_{ts}_{short_uuid}"


def check_duplicate(user_id: int) -> bool:
    """检查用户是否已完成实名认证，防止重复认证。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT is_real_name_verified FROM users WHERE id=%s",
            (user_id,)
        ).fetchone()
    return bool(row and row['is_real_name_verified'])


def initiate_verification(user_id: int, return_url: str, cert_name: str = '', cert_no: str = '') -> Dict[str, Any]:
    """发起实名认证 — 返回第三方认证 URL。

    Args:
        user_id: 用户 ID
        return_url: 认证完成后回跳的前端地址
        cert_name: 用户真实姓名（支付宝 CERT_INFO 模式必需）
        cert_no: 用户身份证号（支付宝 CERT_INFO 模式必需）

    Returns:
        {success, data: {auth_url, request_id}} 或 {success: False, error}
    """
    # 检查是否已认证（防重复）
    if check_duplicate(user_id):
        return {'success': False, 'error': '您已完成实名认证，无需重复操作'}

    enabled = _get_config('verification.enabled') == 'true'
    stub_mode = _get_config('verification.stub_mode') == 'true'
    if not enabled and not stub_mode:
        return {'success': False, 'error': '实名认证功能暂未开放'}

    request_id = generate_request_id(user_id)
    provider = get_provider()

    try:
        auth_url = provider.build_auth_url(request_id, return_url, cert_name=cert_name, cert_no=cert_no)
    except Exception as e:
        logger.error(f"Verification URL generation failed: {e}")
        return {'success': False, 'error': f'认证服务异常: {str(e)}'}

    # 记录认证流水（不包含敏感信息）
    with get_db() as conn:
        conn.execute(
            """INSERT INTO verification_requests
               (user_id, request_id, provider, return_url, status, created_at)
               VALUES (%s,%s,%s,%s,'pending',%s)""",
            (user_id, request_id, provider.__class__.__name__, return_url, now_iso())
        )
        conn.commit()

    return {
        'success': True,
        'data': {
            'auth_url': auth_url,
            'request_id': request_id,
            'certify_url': getattr(provider, '_last_certify_url', ''),
            'gateway': getattr(provider, '_last_gateway', ''),
            'params': getattr(provider, '_last_params', {}),
        }
    }


def verify_callback(user_id: int, params: Dict[str, Any]) -> Dict[str, Any]:
    """处理第三方认证回调 — 验签 + 回填 display_name。

    核心合规保证：
      - id_number 仅在此函数栈内存中使用
      - 验签通过后只写入 display_name + is_real_name_verified + real_name_verified_at
      - 函数返回前 id_number 引用随局部变量销毁
      - 绝不调用任何数据库写入 id_number 的逻辑
      - 不记录包含 id_number 的日志

    Args:
        user_id: 用户 ID（从 request_id 解析或 session 获取）
        params: 回调请求参数

    Returns:
        {success, data: {display_name}} 或 {success: False, error}
    """
    # 检查重复认证
    if check_duplicate(user_id):
        return {'success': False, 'error': '您已完成实名认证'}

    # 验证 request_id（防重放）
    request_id = params.get('request_id') or params.get('outer_order_no') or ''
    if not request_id:
        return {'success': False, 'error': '缺少认证流水号'}

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, status, user_id FROM verification_requests WHERE request_id=%s",
            (request_id,)
        ).fetchone()

        if not existing:
            return {'success': False, 'error': '认证流水不存在'}

        # F-C4: request_id 归属校验 — 防止使用他人流水号冒名认证
        if existing['user_id'] != user_id:
            logger.warning(
                f"Verification request_id={request_id} ownership mismatch: "
                f"caller={user_id}, owner={existing['user_id']}"
            )
            return {'success': False, 'error': '认证流水归属校验失败'}

        if existing['status'] == 'completed':
            return {'success': False, 'error': '该认证流水已处理'}

    # 获取 Provider 并验签
    provider = get_provider()

    if not provider.verify_signature(params):
        logger.warning(f"Verification signature failed for request_id={request_id}")
        return {'success': False, 'error': '签名验证失败，回调可能被伪造'}

    # 【合规关键】提取姓名 — 只取 real_name，id_number 不存储
    real_name = provider.extract_real_name(params)

    # id_number 仅在此处临时提取用于日志（脱敏），函数结束后自动销毁
    # —— 绝不写入数据库、日志或任何持久化存储 ——
    id_number_raw = params.get('cert_no') or params.get('id_number') or ''
    if id_number_raw:
        # 日志脱敏：仅记录前1后1位用于调试
        masked = id_number_raw[0] + '***' + id_number_raw[-1] if len(id_number_raw) > 4 else '***'
        logger.info(f"Verification id_number validated (masked={masked}), not stored per compliance policy")
    # id_number_raw 引用在此处之后不再被使用，函数返回时 gc 回收

    if not real_name:
        return {'success': False, 'error': '未能获取认证姓名'}

    # 写入数据库 — 合规：只写 display_name + 认证标记
    with get_db() as conn:
        conn.execute(
            """UPDATE users SET
               display_name = %s,
               verified_by = %s,
               verified_at = %s,
               is_real_name_verified = 1,
               real_name_verified_at = %s
               WHERE id = %s""",
            (real_name, 'alipay', now_iso(), now_iso(), user_id)
        )
        # 更新认证流水状态
        conn.execute(
            "UPDATE verification_requests SET status='completed', completed_at=%s WHERE request_id=%s",
            (now_iso(), request_id)
        )
        conn.commit()

    # 发送通知（使用已有的通知模板 'user.realname_verified'）
    try:
        from services.notification_service import create_notification as _create_notif
        _create_notif(
            user_id=user_id,
            ntype='reward',
            title=_('Real-name verification passed'),
            content=_('Congratulations! Your real name has been verified and your display name has been updated to {name}.', name=real_name),
        )
    except Exception as e:
        logger.warning(f"Notification send failed (non-critical): {e}")

    return {
        'success': True,
        'data': {
            'display_name': real_name,
            'verified': True,
            'verified_at': now_iso(),
        }
    }
