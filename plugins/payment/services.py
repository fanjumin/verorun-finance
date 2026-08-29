#!/usr/bin/env python3
"""
Payment Plugin Services — 统一网关服务（纯网关，零业务）
=========================================================
支付网关实现位于 plugins/payment/gateways/（V1.5.0 从 subscription 迁入）。
本模块仅暴露 4 个网关级操作：统一下单 / 统一验签 / 统一退款 / 统一扣款。
不包含任何业务逻辑（订单状态、用户、商品、订阅等一律不在此）。
"""
from i18n import _


def create_payment(order_no, amount_fen, subject, description,
                   channel='alipay', interval_type='month'):
    """统一支付入口：按渠道自动路由（cn→支付宝/微信，intl→Stripe/PayPal）"""
    from plugins.payment.gateways import create_payment as _create
    return _create(order_no, amount_fen, subject, description,
                   channel=channel, interval_type=interval_type)


def verify_notify(channel, raw_data, headers=None, raw_body=None):
    """统一支付回调验签"""
    from plugins.payment.gateways import verify_notify as _verify
    return _verify(channel, raw_data, headers or {}, raw_body=raw_body)


def process_refund(order_no, amount_fen, channel, trade_no=''):
    """统一退款入口"""
    from plugins.payment.gateways import process_refund as _refund
    return _refund(order_no, amount_fen, channel, trade_no=trade_no)


def process_charge(channel, agreement_id, order_no, amount_fen, subject=''):
    """统一自动扣款入口（周期扣款/委托代扣）"""
    from plugins.payment.gateways import process_charge as _charge
    return _charge(channel, agreement_id, order_no, amount_fen, subject)
