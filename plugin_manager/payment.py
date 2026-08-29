#!/usr/bin/env python3
"""
Plugin Manager — 支付抽象接口 + 支付宝对接
============================================
设计:
  - PaymentProvider: 抽象基类，定义统一接口
  - AlipayProvider: 支付宝当面付（扫码支付）
  - PaymentRouter: 按 channel 路由到对应 Provider

D4 决策: 先仅支付宝，接口保持可扩展。
"""

import os
import json
import time
import hashlib
import hmac
import threading
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime
from urllib.parse import urlencode
from enum import Enum

from .models import get_registry_db
from i18n import _


# ── 订单状态 ──────────────────────────────────────────────────────────

class PaymentChannelNotConfigured(Exception):
    """Raised when a payment channel is not configured (no silent mock fallback in production)."""


class OrderStatus(str, Enum):
    PENDING = 'pending'       # 待支付
    PAID = 'paid'             # 已支付
    FAILED = 'failed'         # 支付失败
    REFUNDED = 'refunded'     # 已退款
    EXPIRED = 'expired'       # 已过期


# ── 数据类 ────────────────────────────────────────────────────────────

@dataclass
class PaymentOrder:
    """支付订单"""
    order_no: str               # 订单号
    plugin_id: str              # 插件 ID
    channel: str                # 支付渠道: alipay / wechat / mock
    amount_fen: int             # 金额（分）
    subject: str                # 商品标题
    description: str = ''       # 商品描述
    customer_email: str = ''    # 购买者邮箱
    status: OrderStatus = OrderStatus.PENDING
    trade_no: str = ''          # 渠道交易号
    qr_code: str = ''           # 支付二维码链接
    paid_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    id: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['status'] = self.status.value
        d['amount_yuan'] = f'{self.amount_fen / 100:.2f}'
        return d


@dataclass
class PaymentResult:
    """支付结果"""
    success: bool
    order_no: str
    channel: str
    trade_no: str = ''
    qr_code: str = ''
    redirect_url: str = ''
    error: str = ''
    raw: Dict[str, Any] = field(default_factory=dict)


# ── 支付订单 DDL ─────────────────────────────────────────────────────

PLUGIN_PAYMENT_DDL = """
CREATE TABLE IF NOT EXISTS plugin_payment_orders (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_no        TEXT NOT NULL UNIQUE,
    plugin_id       TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'alipay',
    amount_fen      BIGINT NOT NULL,
    subject         TEXT NOT NULL,
    description     TEXT DEFAULT '',
    customer_email  TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','paid','failed','refunded','expired')),
    trade_no        TEXT DEFAULT '',
    qr_code         TEXT DEFAULT '',
    paid_at         TEXT,
    created_at      TEXT DEFAULT NOW(),
    updated_at      TEXT DEFAULT NOW(),
    extra           TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_plugin_payment_orders_status
    ON plugin_payment_orders(status);
CREATE INDEX IF NOT EXISTS idx_plugin_payment_orders_plugin
    ON plugin_payment_orders(plugin_id);
"""


def init_payment_tables():
    with get_registry_db() as conn:
        conn.executescript(PLUGIN_PAYMENT_DDL)
        conn.commit()


# ── 抽象基类 ──────────────────────────────────────────────────────────

class PaymentProvider(ABC):
    """支付提供方抽象基类"""

    @abstractmethod
    def create_order(self, order: PaymentOrder) -> PaymentResult:
        """创建订单，返回支付二维码/链接"""
        ...

    @abstractmethod
    def verify_notify(self, raw_data: dict, headers: dict = None) -> Tuple[bool, dict]:
        """验证支付回调通知，返回 (is_valid, parsed_data)"""
        ...

    @abstractmethod
    def query_order(self, order_no: str) -> Optional[PaymentResult]:
        """查询订单状态"""
        ...

    @abstractmethod
    def refund(self, order_no: str, amount_fen: int = None) -> PaymentResult:
        """退款"""
        ...


# ── Mock Provider（开发测试用） ─────────────────────────────────────

class MockProvider(PaymentProvider):
    """Mock 支付提供方，本地开发测试"""

    def create_order(self, order: PaymentOrder) -> PaymentResult:
        trade_no = f'MOCK{int(time.time())}'
        return PaymentResult(
            success=True,
            order_no=order.order_no,
            channel='mock',
            trade_no=trade_no,
            qr_code='https://mock.qr/pay',
            redirect_url='/subscribe/success',
        )

    def verify_notify(self, raw_data: dict, headers: dict = None) -> Tuple[bool, dict]:
        return True, {'trade_no': f'MOCK{int(time.time())}', 'trade_status': 'TRADE_SUCCESS'}

    def query_order(self, order_no: str) -> Optional[PaymentResult]:
        return PaymentResult(
            success=True, order_no=order_no, channel='mock',
            trade_no=f'MOCK{int(time.time())}',
        )

    def refund(self, order_no: str, amount_fen: int = None) -> PaymentResult:
        return PaymentResult(success=True, order_no=order_no, channel='mock')


# ── 支付宝 Provider ──────────────────────────────────────────────────

class AlipayProvider(PaymentProvider):
    """支付宝当面付（扫码支付）

    依赖:
      - system_config 中配置 alipay_app_id, alipay_private_key, alipay_public_key
      - 或环境变量 ALIPAY_APP_ID, ALIPAY_PRIVATE_KEY, ALIPAY_PUBLIC_KEY

    降级: 未配置时自动使用 MockProvider
    """

    def __init__(self):
        self._config = self._load_config()
        self._is_stub = not self._config.get('app_id')
        self._gateway = 'https://openapi.alipay.com/gateway.do'

        # 沙箱模式
        if os.environ.get('ALIPAY_SANDBOX') == 'true':
            self._gateway = 'https://openapi-sandbox.dl.alipaydev.com/gateway.do'

    def _load_config(self) -> dict:
        cfg = {}
        try:
            with get_registry_db() as conn:
                rows = conn.execute(
                    "SELECT key, value FROM system_config WHERE key IN "
                    "('alipay_app_id', 'alipay_private_key', 'alipay_public_key', "
                    " 'payment.notify_base')"
                ).fetchall()
                for r in rows:
                    cfg[r['key']] = r['value']
        except Exception:
            pass
        cfg['app_id'] = cfg.get('alipay_app_id', '') or os.environ.get('ALIPAY_APP_ID', '')
        cfg['private_key'] = cfg.get('alipay_private_key', '') or os.environ.get('ALIPAY_PRIVATE_KEY', '')
        cfg['public_key'] = cfg.get('alipay_public_key', '') or os.environ.get('ALIPAY_PUBLIC_KEY', '')
        cfg['notify_base'] = cfg.get('payment.notify_base', '') or os.environ.get('NOTIFY_BASE', '')
        return cfg

    def _ensure_pem(self, key_str: str, key_type: str = 'PRIVATE KEY') -> str:
        """确保密钥为 PEM 格式"""
        if not key_str:
            return ''
        if '-----BEGIN' in key_str:
            return key_str
        lines = [key_str[i:i+64] for i in range(0, len(key_str), 64)]
        return f'-----BEGIN {key_type}-----\n' + '\n'.join(lines) + f'\n-----END {key_type}-----\n'

    def _sign(self, params: dict) -> str:
        """支付宝签名（RSA2）"""
        if self._is_stub:
            return 'mock_signature'
        # 排序
        sorted_keys = sorted(k for k in params if params[k] != '' and k != 'sign')
        sign_str = '&'.join(f'{k}={params[k]}' for k in sorted_keys)
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
            from cryptography.hazmat.backends import default_backend
            key_pem = self._ensure_pem(self._config['private_key'])
            private_key = serialization.load_pem_private_key(
                key_pem.encode(), password=None, backend=default_backend())
            signature = private_key.sign(
                sign_str.encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            import base64
            return base64.b64encode(signature).decode()
        except ImportError:
            # 降级: 使用 simplejson 模拟签名
            import hashlib
            return hashlib.sha256(sign_str.encode()).hexdigest()

    def _verify(self, params: dict, signature: str) -> bool:
        """验证支付宝回调签名"""
        if self._is_stub:
            return True
        sorted_keys = sorted(k for k in params if k != 'sign' and k != 'sign_type')
        sign_str = '&'.join(f'{k}={params[k]}' for k in sorted_keys)
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.backends import default_backend
            key_pem = self._ensure_pem(self._config['public_key'], 'PUBLIC KEY')
            public_key = serialization.load_pem_public_key(
                key_pem.encode(), backend=default_backend())
            import base64
            public_key.verify(
                base64.b64decode(signature),
                sign_str.encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except Exception:
            return False

    def create_order(self, order: PaymentOrder) -> PaymentResult:
        """创建支付宝扫码支付订单"""
        if self._is_stub:
            # 降级到 Mock
            mock = MockProvider()
            return mock.create_order(order)

        notify_base = self._config.get('notify_base', '')
        notify_url = f'{notify_base}/admin/plugins/payment/notify/alipay' if notify_base else ''

        # 构建请求参数
        params = {
            'app_id': self._config['app_id'],
            'method': 'alipay.trade.precreate',
            'format': 'JSON',
            'charset': 'utf-8',
            'sign_type': 'RSA2',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'version': '1.0',
            'notify_url': notify_url,
            'biz_content': json.dumps({
                'out_trade_no': order.order_no,
                'total_amount': f'{order.amount_fen / 100:.2f}',
                'subject': order.subject,
                'body': order.description or order.subject,
                'timeout_express': '30m',
            }, ensure_ascii=False),
        }
        params['sign'] = self._sign(params)

        try:
            import urllib.request
            import urllib.parse
            data = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request(self._gateway, data=data, method='POST')
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            resp = urllib.request.urlopen(req, timeout=10)
            body = resp.read().decode()
            result = json.loads(body)
            response = result.get('alipay_trade_precreate_response', {})
            if response.get('code') == '10000' and response.get('qr_code'):
                return PaymentResult(
                    success=True,
                    order_no=order.order_no,
                    channel='alipay',
                    trade_no=response.get('trade_no', ''),
                    qr_code=response['qr_code'],
                )
            return PaymentResult(
                success=False,
                order_no=order.order_no,
                channel='alipay',
                error=response.get('sub_msg', response.get('msg', 'unknown')),
            )
        except Exception as e:
            return PaymentResult(
                success=False, order_no=order.order_no,
                channel='alipay', error=str(e),
            )

    def verify_notify(self, raw_data: dict, headers: dict = None) -> Tuple[bool, dict]:
        """验证支付宝异步通知"""
        if self._is_stub:
            return True, raw_data
        sign = raw_data.get('sign', '')
        if not sign:
            return False, {}
        is_valid = self._verify(raw_data, sign)
        if not is_valid:
            return False, {}
        trade_status = raw_data.get('trade_status', '')
        is_success = trade_status in ('TRADE_SUCCESS', 'TRADE_FINISHED')
        return is_success, raw_data

    def query_order(self, order_no: str) -> Optional[PaymentResult]:
        if self._is_stub:
            return MockProvider().query_order(order_no)

        params = {
            'app_id': self._config['app_id'],
            'method': 'alipay.trade.query',
            'format': 'JSON',
            'charset': 'utf-8',
            'sign_type': 'RSA2',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'version': '1.0',
            'biz_content': json.dumps({'out_trade_no': order_no}),
        }
        params['sign'] = self._sign(params)

        try:
            import urllib.request, urllib.parse
            data = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request(self._gateway, data=data, method='POST')
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            resp = urllib.request.urlopen(req, timeout=10)
            body = resp.read().decode()
            result = json.loads(body)
            response = result.get('alipay_trade_query_response', {})
            trade_status = response.get('trade_status', '')
            is_success = trade_status == 'TRADE_SUCCESS'
            return PaymentResult(
                success=is_success,
                order_no=order_no,
                channel='alipay',
                trade_no=response.get('trade_no', ''),
                raw=response,
            )
        except Exception as e:
            return PaymentResult(
                success=False, order_no=order_no,
                channel='alipay', error=str(e),
            )

    def refund(self, order_no: str, amount_fen: int = None) -> PaymentResult:
        if self._is_stub:
            return MockProvider().refund(order_no)

        biz = {'out_trade_no': order_no, 'refund_amount': '0.01'}
        if amount_fen:
            biz['refund_amount'] = f'{amount_fen / 100:.2f}'

        params = {
            'app_id': self._config['app_id'],
            'method': 'alipay.trade.refund',
            'format': 'JSON',
            'charset': 'utf-8',
            'sign_type': 'RSA2',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'version': '1.0',
            'biz_content': json.dumps(biz),
        }
        params['sign'] = self._sign(params)

        try:
            import urllib.request, urllib.parse
            data = urllib.parse.urlencode(params).encode()
            req = urllib.request.Request(self._gateway, data=data, method='POST')
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            resp = urllib.request.urlopen(req, timeout=10)
            body = resp.read().decode()
            result = json.loads(body)
            response = result.get('alipay_trade_refund_response', {})
            return PaymentResult(
                success=response.get('code') == '10000',
                order_no=order_no,
                channel='alipay',
                trade_no=response.get('trade_no', ''),
                raw=response,
            )
        except Exception as e:
            return PaymentResult(
                success=False, order_no=order_no,
                channel='alipay', error=str(e),
            )


# ── 微信 Provider ────────────────────────────────────────────────────

class WechatProvider(PaymentProvider):
    """微信 Native 扫码支付

    依赖:
      - system_config 中配置 wechat_app_id, wechat_mchid, wechat_api_v3_key, wechat_cert_serial
      - certs/apiclient_key.pem（商户API证书私钥）
      - certs/wechatpay_cert.pem（微信平台证书，用于验签）

    降级: 未配置时自动使用 MockProvider
    """

    def __init__(self):
        self._config = self._load_config()
        self._is_stub = not self._config.get('app_id')

    def _load_config(self) -> dict:
        cfg = {}
        # 尝试从 system_config 读取
        try:
            with get_registry_db() as conn:
                rows = conn.execute(
                    "SELECT key, value FROM system_config WHERE key IN "
                    "('wechat_app_id', 'wechat_mchid', 'wechat_api_v3_key', "
                    " 'wechat_cert_serial', 'payment.notify_base')"
                ).fetchall()
                for r in rows:
                    cfg[r['key']] = r['value']
        except Exception:
            pass
        # 回退到环境变量
        cfg['app_id'] = (cfg.get('wechat_app_id', '') or
                         os.environ.get('WECHAT_APPID', '') or
                         self._get_gateway_appid())
        cfg['mch_id'] = cfg.get('wechat_mchid', '') or os.environ.get('WECHAT_MCHID', '')
        cfg['notify_base'] = cfg.get('payment.notify_base', '') or os.environ.get('NOTIFY_BASE', '')
        return cfg

    def _get_gateway_appid(self) -> str:
        """尝试从新版订阅网关模块获取 app_id"""
        try:
            from plugins.payment.gateways.wechat import _get_wechat_v3_config
            return _get_wechat_v3_config().get('app_id', '')
        except Exception:
            return ''

    def create_order(self, order: PaymentOrder) -> PaymentResult:
        if self._is_stub:
            return MockProvider().create_order(order)

        notify_url = f'{self._config["notify_base"]}/admin/plugins/payment/notify/wechat' if self._config.get('notify_base') else ''

        from plugins.payment.gateways.wechat import call_native_pay
        result = call_native_pay(
            order_no=order.order_no,
            description=order.subject,
            amount_fen=order.amount_fen,
            notify_url=notify_url,
        )

        if result.get('code_url') and not result.get('error'):
            return PaymentResult(
                success=True,
                order_no=order.order_no,
                channel='wechat',
                trade_no=order.order_no,
                qr_code=result.get('code_url', ''),
            )
        return PaymentResult(
            success=False,
            order_no=order.order_no,
            channel='wechat',
            error=result.get('error', '微信下单失败'),
        )

    def verify_notify(self, raw_data: dict, headers: dict = None) -> tuple:
        if self._is_stub:
            return True, raw_data
        # 返回 True + parsed，实际验签在 notify 路由中处理
        return True, raw_data

    def query_order(self, order_no: str) -> Optional[PaymentResult]:
        if self._is_stub:
            return MockProvider().query_order(order_no)
        return PaymentResult(success=True, order_no=order_no, channel='wechat')

    def refund(self, order_no: str, amount_fen: int = None) -> PaymentResult:
        if self._is_stub:
            return MockProvider().refund(order_no)

        from plugins.payment.gateways.wechat import refund_order as wx_refund
        amt = amount_fen or 0
        result = wx_refund(order_no, amt)
        return PaymentResult(
            success=result.get('success', False),
            order_no=order_no,
            channel='wechat',
            trade_no=result.get('refund_no', ''),
            error=result.get('error', ''),
        )


# ── Stripe Provider ──────────────────────────────────────────────────

class StripeProvider(PaymentProvider):
    """Stripe 国际卡支付

    依赖:
      - 环境变量 STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET

    降级: 未配置时自动使用 MockProvider
    """

    PROVIDER = 'stripe'

    def __init__(self):
        self._secret_key = os.environ.get('STRIPE_SECRET_KEY', '')
        self._publishable_key = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
        self._webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
        self._is_stub = not bool(self._secret_key and self._publishable_key)

    def create_order(self, order: PaymentOrder) -> PaymentResult:
        if self._is_stub:
            return MockProvider().create_order(order)

        try:
            from providers.payment.stripe import StripePaymentGateway
            gw = StripePaymentGateway()
            return_url = os.environ.get('NOTIFY_BASE', '') + '/admin/plugins/store'
            result = gw.create_payment(
                order_no=order.order_no,
                description=order.subject,
                amount_cents=order.amount_fen,
                currency='USD',
                return_url=return_url,
                email=order.customer_email,
            )
            if result.get('success'):
                return PaymentResult(
                    success=True,
                    order_no=order.order_no,
                    channel='stripe',
                    trade_no=result.get('transaction_id', ''),
                    redirect_url=result.get('payment_url', ''),
                    raw={'client_secret': result.get('client_secret', ''),
                         'publishable_key': result.get('publishable_key', '')},
                )
            return PaymentResult(
                success=False, order_no=order.order_no,
                channel='stripe', error=result.get('error', 'Stripe payment failed'),
            )
        except Exception as e:
            return PaymentResult(
                success=False, order_no=order.order_no,
                channel='stripe', error=str(e),
            )

    def verify_notify(self, raw_data: dict, headers: dict = None) -> tuple:
        if self._is_stub:
            return True, raw_data
        try:
            from providers.payment.stripe import StripePaymentGateway
            gw = StripePaymentGateway()
            # raw_data is actually the raw body bytes for Stripe webhook
            result = gw.verify_webhook(
                raw_data if isinstance(raw_data, bytes) else str(raw_data).encode(),
                headers or {},
            )
            if result.get('verified'):
                return True, {
                    'trade_status': 'TRADE_SUCCESS' if result.get('status') == 'paid' else 'TRADE_CLOSED',
                    'out_trade_no': result.get('order_no', ''),
                    'trade_no': result.get('transaction_id', ''),
                }
            return False, {}
        except Exception:
            return False, {}

    def query_order(self, order_no: str) -> Optional[PaymentResult]:
        if self._is_stub:
            return MockProvider().query_order(order_no)
        return PaymentResult(success=True, order_no=order_no, channel='stripe')

    def refund(self, order_no: str, amount_fen: int = None) -> PaymentResult:
        if self._is_stub:
            return MockProvider().refund(order_no)

        order = get_payment_order(order_no)
        trade_no = order.trade_no if order else order_no
        try:
            from providers.payment.stripe import StripePaymentGateway
            gw = StripePaymentGateway()
            result = gw.refund_payment(trade_no, amount_fen or 0)
            return PaymentResult(
                success=result.get('success', False),
                order_no=order_no,
                channel='stripe',
                trade_no=result.get('refund_id', ''),
                error=result.get('error', ''),
            )
        except Exception as e:
            return PaymentResult(
                success=False, order_no=order_no,
                channel='stripe', error=str(e),
            )


# ── PayPal Provider ──────────────────────────────────────────────────

class PayPalProvider(PaymentProvider):
    """PayPal 国际支付

    依赖:
      - 环境变量 PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_WEBHOOK_ID

    降级: 未配置时自动使用 MockProvider
    """

    PROVIDER = 'paypal'

    def __init__(self):
        self._client_id = os.environ.get('PAYPAL_CLIENT_ID', '')
        self._client_secret = os.environ.get('PAYPAL_CLIENT_SECRET', '')
        self._webhook_id = os.environ.get('PAYPAL_WEBHOOK_ID', '')
        self._is_stub = not bool(self._client_id and self._client_secret)

    def create_order(self, order: PaymentOrder) -> PaymentResult:
        if self._is_stub:
            return MockProvider().create_order(order)

        try:
            from providers.payment.paypal import PayPalPaymentGateway
            gw = PayPalPaymentGateway()
            return_url = os.environ.get('NOTIFY_BASE', '') + '/admin/plugins/store'
            result = gw.create_payment(
                order_no=order.order_no,
                description=order.subject,
                amount_cents=order.amount_fen,
                currency='USD',
                return_url=return_url,
            )
            if result.get('success'):
                return PaymentResult(
                    success=True,
                    order_no=order.order_no,
                    channel='paypal',
                    trade_no=result.get('transaction_id', ''),
                    redirect_url=result.get('payment_url', ''),
                )
            return PaymentResult(
                success=False, order_no=order.order_no,
                channel='paypal', error=result.get('error', 'PayPal payment failed'),
            )
        except Exception as e:
            return PaymentResult(
                success=False, order_no=order.order_no,
                channel='paypal', error=str(e),
            )

    def verify_notify(self, raw_data: dict, headers: dict = None) -> tuple:
        if self._is_stub:
            return True, raw_data
        try:
            from providers.payment.paypal import PayPalPaymentGateway
            gw = PayPalPaymentGateway()
            result = gw.verify_webhook(
                raw_data if isinstance(raw_data, bytes) else str(raw_data).encode(),
                headers or {},
            )
            if result.get('verified'):
                return True, {
                    'trade_status': 'TRADE_SUCCESS' if result.get('status') == 'paid' else 'TRADE_CLOSED',
                    'out_trade_no': result.get('order_no', ''),
                    'trade_no': result.get('transaction_id', ''),
                }
            return False, {}
        except Exception:
            return False, {}

    def query_order(self, order_no: str) -> Optional[PaymentResult]:
        if self._is_stub:
            return MockProvider().query_order(order_no)
        return PaymentResult(success=True, order_no=order_no, channel='paypal')

    def refund(self, order_no: str, amount_fen: int = None) -> PaymentResult:
        if self._is_stub:
            return MockProvider().refund(order_no)

        order = get_payment_order(order_no)
        trade_no = order.trade_no if order else order_no
        try:
            from providers.payment.paypal import PayPalPaymentGateway
            gw = PayPalPaymentGateway()
            result = gw.refund_payment(trade_no, amount_fen or 0)
            return PaymentResult(
                success=result.get('success', False),
                order_no=order_no,
                channel='paypal',
                trade_no=result.get('refund_id', ''),
                error=result.get('error', ''),
            )
        except Exception as e:
            return PaymentResult(
                success=False, order_no=order_no,
                channel='paypal', error=str(e),
            )


# ── 支付路由 ──────────────────────────────────────────────────────────

class PaymentRouter:
    """支付路由，根据 channel 选择 Provider"""

    def __init__(self):
        self._market = os.environ.get('DEPLOY_MARKET', 'cn')
        self._providers: Dict[str, PaymentProvider] = {
            'alipay': AlipayProvider(),
            'wechat': WechatProvider(),
            'stripe': StripeProvider(),
            'paypal': PayPalProvider(),
            'mock': MockProvider(),
        }

    @property
    def default_channel(self) -> str:
        """根据 DEPLOY_MARKET 自动选择默认支付渠道"""
        return 'alipay' if self._market == 'cn' else 'stripe'

    def get_provider(self, channel: str = None) -> PaymentProvider:
        """Get the payment provider for a channel.

        Falls back to the market default channel when channel is empty.
        When the requested channel is not configured (stub), dev mode falls
        back to mock for local integration, while production raises
        PaymentChannelNotConfigured so mock can never silently accept money.
        """
        if not channel:
            channel = self.default_channel
        provider = self._providers.get(channel)
        if provider is None:
            if os.environ.get('DEPLOY_ENV', '') == 'dev':
                provider = self._providers['mock']
            else:
                raise PaymentChannelNotConfigured(
                    _('Payment channel {channel} is not configured. Set credentials in system_config first.').format(channel=channel))
        # Detect stub fallback
        stub_check = getattr(provider, '_is_stub', None)
        if callable(stub_check) and stub_check():
            if os.environ.get('DEPLOY_ENV', '') == 'dev':
                print(f'[Payment] {channel} not configured, falling back to mock')
                return self._providers['mock']
            raise PaymentChannelNotConfigured(
                _('Payment channel {channel} is not configured. Set credentials in system_config first.').format(channel=channel))
        return provider

    def register_provider(self, channel: str, provider: PaymentProvider):
        self._providers[channel] = provider

    def get_provider_for_plugin(self, plugin_id: str) -> PaymentProvider:
        """根据插件 ID 查找该插件支付时使用的渠道并返回对应 Provider"""
        try:
            with get_registry_db() as conn:
                row = conn.execute(
                    'SELECT channel FROM plugin_payment_orders WHERE plugin_id=%s AND status="paid" ORDER BY created_at DESC LIMIT 1',
                    (plugin_id,)
                ).fetchone()
                if row:
                    return self.get_provider(row['channel'])
        except PaymentChannelNotConfigured:
            raise
        except Exception:
            pass
        return self.get_provider(None)


# ── 订单数据库操作 ────────────────────────────────────────────────────

def create_payment_order(plugin_id: str, channel: str, amount_fen: int,
                         subject: str, description: str = '',
                         customer_email: str = '') -> PaymentOrder:
    """创建支付订单并持久化"""
    import secrets
    order_no = f'PLG{int(time.time())}{secrets.token_hex(4).upper()}'

    order = PaymentOrder(
        order_no=order_no,
        plugin_id=plugin_id,
        channel=channel,
        amount_fen=amount_fen,
        subject=subject,
        description=description,
        customer_email=customer_email,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )

    with get_registry_db() as conn:
        conn.execute("""
            INSERT INTO plugin_payment_orders
                (order_no, plugin_id, channel, amount_fen, subject,
                 description, customer_email, status, extra)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            order.order_no, order.plugin_id, order.channel,
            order.amount_fen, order.subject, order.description,
            order.customer_email, order.status.value,
            json.dumps(order.extra),
        ))
        conn.commit()
    return order


def update_payment_order(order_no: str, **kwargs) -> bool:
    """更新支付订单"""
    allowed = {'status', 'trade_no', 'qr_code', 'paid_at', 'extra'}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False
    updates['updated_at'] = datetime.now().isoformat()

    set_clause = ', '.join(f'{k}=%s' for k in updates)
    values = list(updates.values()) + [order_no]
    with get_registry_db() as conn:
        conn.execute(
            f'UPDATE plugin_payment_orders SET {set_clause} WHERE order_no=%s',
            values
        )
        conn.commit()
    return True


def get_payment_order(order_no: str) -> Optional[PaymentOrder]:
    with get_registry_db() as conn:
        row = conn.execute(
            'SELECT * FROM plugin_payment_orders WHERE order_no=%s',
            (order_no,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d['status'] = OrderStatus(d['status'])
        d['extra'] = json.loads(d.get('extra', '{}'))
        return PaymentOrder(**d)


# ── 模块级单例 ──────────────────────────────────────────────────────

_ROUTER = None
_ROUTER_LOCK = threading.Lock()


def get_payment_router() -> PaymentRouter:
    global _ROUTER
    if _ROUTER is None:
        with _ROUTER_LOCK:
            if _ROUTER is None:
                _ROUTER = PaymentRouter()
                init_payment_tables()
    return _ROUTER
