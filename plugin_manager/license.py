#!/usr/bin/env python3
"""
Plugin Manager — License 引擎
===============================
支持在线验证 + 离线 token 双通道。

设计:
  - 在线验证: 调用远程 License 服务 API
  - 离线验证: RSA-2048 签名的离线 token，72h 宽容期
  - 免费插件跳过 License 检查
  - Site ID: MAC + 机器名 + 磁盘序列号组合哈希

D3 决策确认: License 服务端是独立子服务，此处仅实现客户端侧逻辑。
"""

import os
import json
import hashlib
import hmac
import base64
import socket
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError

from .models_store import (
    LicenseRecord, LicenseType, LicenseStatus,
    init_license_store_tables, get_registry_db,
)

# ── 站点标识 ──────────────────────────────────────────────────────────

_SITE_ID_CACHE: Optional[str] = None
_SITE_ID_LOCK = threading.Lock()


def _get_mac_address() -> str:
    """获取 MAC 地址作为站点标识的一部分"""
    try:
        import uuid
        mac = uuid.getnode()
        if mac and mac != 0xFFFFFFFFFFFF:
            return f'{mac:012x}'
    except Exception:
        pass
    return 'unknown'


def get_site_id() -> str:
    """生成或获取站点唯一标识

    组合: MD5(MAC + hostname)
    结果固定，除非硬件更换。
    """
    global _SITE_ID_CACHE
    if _SITE_ID_CACHE is not None:
        return _SITE_ID_CACHE

    with _SITE_ID_LOCK:
        if _SITE_ID_CACHE is not None:
            return _SITE_ID_CACHE

        raw = f'{_get_mac_address()}-{socket.gethostname()}-{os.name}'
        _SITE_ID_CACHE = hashlib.md5(raw.encode()).hexdigest()[:16]
        return _SITE_ID_CACHE


# ── License 离线 token ────────────────────────────────────────────────

def _get_license_secret() -> str:
    """Get License encryption key from environment variable.
    
    Raises RuntimeError if PLUGIN_LICENSE_SECRET is not set.
    """
    secret = os.environ.get('PLUGIN_LICENSE_SECRET')
    if not secret:
        raise RuntimeError(
            "PLUGIN_LICENSE_SECRET environment variable is required. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return secret


def generate_offline_token(plugin_id: str, license_key: str,
                           expires_at: str, site_id: str) -> str:
    """生成本地离线 token（HMAC-SHA256 签名）

    token = base64(json_data + '.' + HMAC_SHA256(json_data, secret))
    不存储私钥，仅用于本地防篡改验证。
    """
    payload = json.dumps({
        'p': plugin_id,
        'k': license_key[-8:],  # 仅存 key 后 8 位
        'e': expires_at,
        's': site_id,
        'v': 1,
    }, separators=(',', ':'))
    secret = _get_license_secret()
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    token = base64.urlsafe_b64encode(f'{payload}.{sig}'.encode()).decode()
    return token


def verify_offline_token(token: str, plugin_id: str, site_id: str) -> Tuple[bool, str]:
    """验证离线 token，返回 (is_valid, expires_at|error_msg)"""
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        if '.' not in decoded:
            return False, 'invalid format'
        payload_str, sig = decoded.rsplit('.', 1)
        payload = json.loads(payload_str)

        # 验证签名
        secret = _get_license_secret()
        expected_sig = hmac.new(secret.encode(), payload_str.encode(),
                                hashlib.sha256).hexdigest()[:32]
        if sig != expected_sig:
            return False, 'signature mismatch'

        # 验证插件 ID
        if payload.get('p') != plugin_id:
            return False, 'plugin_id mismatch'

        # 验证站点 ID
        if payload.get('s') != site_id:
            return False, 'site_id mismatch'

        # 验证版本
        if payload.get('v') != 1:
            return False, 'token version mismatch'

        return True, payload.get('e', '')

    except Exception as e:
        return False, str(e)


# ── 远程 License 服务 API (SPI 模式) ──────────────────────────────────

from .region import get_api_base


def _is_official_edition() -> bool:
    """官方版判定：VR_EDITION=official（由官方独立部署脚本写入 .env）

    官方版拥有全部插件权限，无需单独激活 License。
    客户版（customer）走正常付费校验。本地标志仅为加速判断，
    官方身份由私有仓库分发 + 独立部署脚本保证（客户无法获取）。
    """
    return os.environ.get('VR_EDITION', '').strip().lower() == 'official'


def _get_license_url() -> str:
    """获取 License 服务 API 基础 URL（区域感知）。
    环境变量 REMOTE_LICENSE_URL 覆盖优先（向后兼容）。
    """
    override = os.environ.get('REMOTE_LICENSE_URL', '')
    if override:
        return override.rstrip('/')
    return f"{get_api_base().rstrip('/v1')}/license"


def _call_remote(method: str, path: str, data: dict = None) -> dict:
    """调用远程 License 服务 API

    可 mock: 设置环境变量 REMOTE_LICENSE_URL = 'mock://' 或捕获异常后降级。
    本方法仅为 SPI 占位，阶段六部署真实服务后替换。
    """
    base = _get_license_url()
    url = f'{base.rstrip("/")}/{path.lstrip("/")}'
    if base.startswith('mock://'):
        # Mock 模式
        return _mock_remote(method, path, data)

    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, method=method)
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'PluginManager/1.0')

    try:
        resp = urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except URLError as e:
        return {'success': False, 'error': str(e)}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def _mock_remote(method: str, path: str, data: dict = None) -> dict:
    """Mock 远程 API，用于本地开发测试（仅 License 操作）"""
    if 'validate' in path and data:
        return {
            'success': True,
            'data': {
                'valid': True,
                'license_status': 'active',
                'expires_at': (datetime.now() + timedelta(days=365)).isoformat(),
            }
        }
    if 'activate' in path and data:
        return {
            'success': True,
            'data': {
                'license_key': data.get('license_key', 'MOCK-KEY'),
                'activated': True,
                'expires_at': (datetime.now() + timedelta(days=365)).isoformat(),
                'offline_token': generate_offline_token(
                    data.get('plugin_id', ''),
                    data.get('license_key', ''),
                    (datetime.now() + timedelta(days=365)).isoformat(),
                    get_site_id(),
                ),
            }
        }
    return {'success': True, 'data': {}}


# ── LicenseManager 核心类 ──────────────────────────────────────────────

class LicenseManager:
    """License 管理器"""

    def __init__(self):
        self._lock = threading.Lock()
        init_license_store_tables()

    # ── 激活 License ──────────────────────────────────────────────────

    def activate(self, plugin_id: str, license_key: str,
                 customer_email: str = '') -> dict:
        """激活 License

        流程:
          1. 调用远程 API 验证并激活
          2. 保存 LicenseRecord 到本地
          3. 生成离线 token 用于本地后续校验

        Returns:
            {'success': bool, 'license': dict, 'error': str}
        """
        site_id = get_site_id()

        # 检查本地是否已有激活记录
        existing = self._get_license(plugin_id)
        if existing:
            if existing.license_status == LicenseStatus.ACTIVE:
                return {
                    'success': True,
                    'license': existing.to_dict(),
                    'error': 'already_active',
                }

        # 调用远程 API
        remote_result = _call_remote('POST', '/activate', {
            'plugin_id': plugin_id,
            'license_key': license_key,
            'site_id': site_id,
            'customer_email': customer_email,
        })

        if not remote_result.get('success'):
            # 离线激活：尝试本地生成
            return self._activate_offline(plugin_id, license_key, site_id)

        data = remote_result.get('data', {})
        expires_at = data.get('expires_at', '')
        offline_token = data.get('offline_token', '')

        # 持久化
        record = LicenseRecord(
            plugin_id=plugin_id,
            license_key=license_key,
            license_type=LicenseType(data.get('license_type', 'onetime')),
            license_status=LicenseStatus.ACTIVE,
            site_id=site_id,
            customer_email=customer_email,
            activated_at=datetime.now().isoformat(),
            expires_at=expires_at or None,
            offline_token=offline_token,
            last_validated=datetime.now().isoformat(),
        )
        self._save_license(record)

        return {
            'success': True,
            'license': record.to_dict(),
        }

    def _activate_offline(self, plugin_id: str, license_key: str,
                          site_id: str) -> dict:
        """离线激活（不依赖远程 API）

        用于远程服务不可访问时的降级。
        生成一个短期有效的离线 token（7 天）。
        """
        expires = (datetime.now() + timedelta(days=7)).isoformat()
        offline_token = generate_offline_token(plugin_id, license_key,
                                                expires, site_id)
        record = LicenseRecord(
            plugin_id=plugin_id,
            license_key=license_key,
            license_type=LicenseType.ONETIME,
            license_status=LicenseStatus.GRACE,
            site_id=site_id,
            activated_at=datetime.now().isoformat(),
            expires_at=expires,
            offline_token=offline_token,
            grace_until=(datetime.now() + timedelta(hours=72)).isoformat(),
            last_validated=datetime.now().isoformat(),
        )
        self._save_license(record)
        return {
            'success': True,
            'license': record.to_dict(),
            'error': 'offline_mode',
        }

    # ── 验证 License ──────────────────────────────────────────────────

    def validate(self, plugin_id: str) -> dict:
        """验证插件 License

        Returns:
            {'valid': bool, 'status': str, 'expires_at': str, 'error': str}
        """
        # 官方版：全部插件直接授权（无需激活/续费）
        if _is_official_edition():
            return {'valid': True, 'status': 'official',
                    'expires_at': '', 'error': ''}
        record = self._get_license(plugin_id)
        if not record:
            return {'valid': False, 'status': 'unlicensed',
                    'error': 'no_license'}

        # 检查状态
        if record.license_status == LicenseStatus.EXPIRED:
            return {'valid': False, 'status': 'expired',
                    'expires_at': record.expires_at or ''}

        if record.license_status == LicenseStatus.REVOKED:
            return {'valid': False, 'status': 'revoked', 'error': 'license revoked'}

        if record.license_status == LicenseStatus.ACTIVE:
            # 检查是否过期
            if record.expires_at:
                try:
                    expires = datetime.fromisoformat(record.expires_at)
                    if datetime.now() > expires:
                        self._update_status(plugin_id, LicenseStatus.EXPIRED)
                        return {'valid': False, 'status': 'expired',
                                'expires_at': record.expires_at}
                except ValueError:
                    pass

            # 在线验证（SPI，可降级）
            # 附带本地商店登记的包哈希，供 License 服务端做「同包多站点复用」检测
            _pkg_hash = ''
            try:
                from .models import get_registry_db
                with get_registry_db() as conn:
                    _cur = conn.execute(
                        'SELECT package_hash FROM store_plugins WHERE identifier = %s',
                        (plugin_id,))
                    _row = _cur.fetchone()
                if _row:
                    _pkg_hash = _row['package_hash'] or ''
            except Exception:
                pass
            remote = _call_remote('POST', '/validate', {
                'plugin_id': plugin_id,
                'license_key': record.license_key,
                'site_id': get_site_id(),
                'package_hash': _pkg_hash,
            })
            if remote.get('success') and remote.get('data', {}).get('valid') is False:
                self._update_status(plugin_id, LicenseStatus.REVOKED)
                return {'valid': False, 'status': 'revoked',
                        'error': 'remote revoked'}

            self._update_last_validated(plugin_id)
            return {'valid': True, 'status': 'active',
                    'expires_at': record.expires_at or ''}

        if record.license_status == LicenseStatus.GRACE:
            # 检查离线宽容期
            if record.grace_until:
                try:
                    grace_end = datetime.fromisoformat(record.grace_until)
                    if datetime.now() > grace_end:
                        self._update_status(plugin_id, LicenseStatus.PENDING)
                        return {'valid': False, 'status': 'grace_expired',
                                'error': 'grace period expired'}
                except ValueError:
                    pass
            return {'valid': True, 'status': 'grace',
                    'expires_at': record.expires_at or ''}

        return {'valid': False, 'status': record.license_status.value}

    # ── 反激活 ────────────────────────────────────────────────────────

    def deactivate(self, plugin_id: str) -> dict:
        """反激活 License"""
        record = self._get_license(plugin_id)
        if not record:
            return {'success': False, 'error': 'no_license'}

        # 通知远程
        _call_remote('POST', '/deactivate', {
            'plugin_id': plugin_id,
            'license_key': record.license_key,
            'site_id': get_site_id(),
        })

        # 本地移除
        with get_registry_db() as conn:
            conn.execute(
                'DELETE FROM plugin_licenses WHERE plugin_id = %s',
                (plugin_id,)
            )
            conn.commit()

        return {'success': True}

    # ── 查询 ──────────────────────────────────────────────────────────

    def get_license(self, plugin_id: str) -> Optional[dict]:
        record = self._get_license(plugin_id)
        return record.to_dict() if record else None

    def list_licenses(self) -> List[dict]:
        with get_registry_db() as conn:
            rows = conn.execute(
                'SELECT * FROM plugin_licenses ORDER BY created_at DESC'
            ).fetchall()
            return [LicenseRecord.from_row(dict(r)).to_dict() for r in rows]

    # ── 版本包成员 License ──────────────────────────────────────────

    def grant_bundle_member(self, plugin_id: str, bundle_id: str,
                            order_no: str, expires_at: str,
                            subscription_id: Optional[int] = None) -> dict:
        """版本包成员 License：由包订单 key 派生 + bundle_id 标记。

        - license_key = f'{bundle_id}:{order_no}'（包订单唯一）
        - metadata 带 bundle_id / bundle_order_no，供生命周期批量同步
        - 幂等覆盖：一个插件仅保留一条 License（_save_license 先删后插）
        """
        record = LicenseRecord(
            plugin_id=plugin_id,
            license_key=f'{bundle_id}:{order_no}',
            license_type=LicenseType.SUBSCRIPTION,
            license_status=LicenseStatus.ACTIVE,
            site_id=get_site_id(),
            activated_at=datetime.now().isoformat(),
            expires_at=expires_at,
            last_validated=datetime.now().isoformat(),
            order_id=order_no,
            subscription_id=str(subscription_id or ''),
            auto_renew=True,
            metadata={'bundle_id': bundle_id, 'bundle_order_no': order_no},
        )
        self._save_license(record)
        return record.to_dict()

    def expire_bundle_members(self, bundle_id: str) -> int:
        """将某版本包的全部成员 License 标记过期（返回受影响行数）。

        bundle_id 为空时直接返回 0（保护性校验，避免误伤普通订阅）。
        """
        if not bundle_id:
            return 0
        with get_registry_db() as conn:
            cur = conn.execute(
                "UPDATE plugin_licenses SET license_status='expired', updated_at=NOW() "
                "WHERE metadata::jsonb->>'bundle_id'=%s AND license_status='active'",
                (bundle_id,)
            )
            conn.commit()
            return cur.rowcount if cur.rowcount else 0

    # ── 检查是否付费（供 PluginManager 集成） ─────────────────────────

    def is_paid_plugin(self, plugin_id: str) -> bool:
        """检查插件是否是付费插件。用户上传的自研插件始终免费。
        官方版：无付费概念，全部视为已授权（enable/upgrade 自动跳过 License 检查）。
        """
        # 官方版：所有插件免 License
        if _is_official_edition():
            return False
        with get_registry_db() as conn:
            # ★ v1.4: 用户上传的自研插件不接入付费体系
            row = conn.execute(
                'SELECT source FROM plugin_registry WHERE identifier = %s',
                (plugin_id,)
            ).fetchone()
            if row and row.get('source') == 'upload':
                return False

            row = conn.execute(
                'SELECT price_type FROM store_plugins WHERE identifier = %s',
                (plugin_id,)
            ).fetchone()
            if row:
                return row['price_type'] != 'free'
        return False

    def is_licensed(self, plugin_id: str) -> bool:
        """检查插件是否有有效 License"""
        result = self.validate(plugin_id)
        return result.get('valid', False)

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _get_license(self, plugin_id: str) -> Optional[LicenseRecord]:
        with get_registry_db() as conn:
            row = conn.execute(
                'SELECT * FROM plugin_licenses WHERE plugin_id = %s',
                (plugin_id,)
            ).fetchone()
            if row is None:
                return None
            return LicenseRecord.from_row(dict(row))

    def _save_license(self, record: LicenseRecord):
        with get_registry_db() as conn:
            # 先删除已有记录（插件级别，保证一个插件只有一条）
            conn.execute('DELETE FROM plugin_licenses WHERE plugin_id=%s', (record.plugin_id,))
            conn.execute("""
                INSERT INTO plugin_licenses (
                    plugin_id, license_key, license_type, license_status,
                    site_id, site_name, customer_email, max_sites,
                    activated_at, expires_at, trial_ends_at, last_validated,
                    offline_token, grace_until, order_id, subscription_id,
                    auto_renew, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                record.plugin_id, record.license_key,
                record.license_type.value, record.license_status.value,
                record.site_id, record.site_name, record.customer_email,
                record.max_sites,
                record.activated_at, record.expires_at,
                record.trial_ends_at, record.last_validated,
                record.offline_token, record.grace_until,
                record.order_id, record.subscription_id,
                int(record.auto_renew),
                json.dumps(record.metadata, ensure_ascii=False),
            ))
            conn.commit()

    def _update_status(self, plugin_id: str, status: LicenseStatus):
        with get_registry_db() as conn:
            conn.execute(
                "UPDATE plugin_licenses SET license_status=%s, updated_at=NOW() WHERE plugin_id=%s",
                (status.value, plugin_id)
            )
            conn.commit()

    def _update_last_validated(self, plugin_id: str):
        with get_registry_db() as conn:
            conn.execute(
                "UPDATE plugin_licenses SET last_validated=NOW(), updated_at=NOW() WHERE plugin_id=%s",
                (plugin_id,)
            )
            conn.commit()


# ── 开发者入驻占位接口（未来） ──────────────────────────────────────

def submit_plugin(plugin_data: dict) -> dict:
    """[未来] 开发者提交插件到商店审核（暂未开放，明确拒绝）

    Args:
        plugin_data: {
            'identifier': str,       # 唯一标识
            'name': str,             # 插件名称
            'version': str,          # 当前版本
            'description': str,      # 描述
            'price_type': str,       # 'free' | 'onetime' | 'sub'
            'price_amount': int,     # 金额（分），免费为 0
            'price_interval': str,   # 'month' | 'year' (仅 sub)
            'screenshots': list,     # 截图 URL
            'readme_url': str,       # 文档 URL
            'tags': list,            # 标签
        }
    Returns:
        {'success': bool, 'plugin_id': str, 'error': str}
    """
    from i18n import _
    return {'success': False, 'error': _('Plugin submission is not open yet')}


# ── 模块级单例 ──────────────────────────────────────────────────────

_LICENSE_MGR = None
_LICENSE_MGR_LOCK = threading.Lock()


def get_license_manager() -> LicenseManager:
    global _LICENSE_MGR
    if _LICENSE_MGR is None:
        with _LICENSE_MGR_LOCK:
            if _LICENSE_MGR is None:
                _LICENSE_MGR = LicenseManager()
    return _LICENSE_MGR
