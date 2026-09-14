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
import re
import hashlib
import hmac
import base64
import socket
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    _CRYPTO_OK = True
except ImportError:
    InvalidSignature = None
    Ed25519PublicKey = None
    _CRYPTO_OK = False

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


# ── 官方版签名凭证（Task 6：官方版仅内部使用，凭证随私有仓库分发）──
# Ed25519 公钥（hex），与 deploy/scripts/sign_release.py 同款。
# official_token.json(.sig) 由发版机以 RELEASE_SIGN_KEY 签发并提交到 verorun-code
# 私有仓库；客户仓库永远没有，即使伪造 VR_EDITION=official 也无法通过验签，
# 官方版判定退化为普通客户版（走正常付费校验）。
RELEASE_VERIFY_KEY = "a467ea79346e26f8c4fb75ecc07b400b3af86f7c9a7aab9878bb2754b8107ef4"
OFFICIAL_TOKEN_NAME = "official_token.json"
OFFICIAL_TOKEN_SIG_NAME = "official_token.json.sig"


def _project_root() -> Path:
    """推断项目根目录：plugin_manager/license.py 的上级的上级。"""
    return Path(__file__).resolve().parents[1]


# ── 官方凭证吊销（VR-SEC：防旧凭证泄露后继续生效）──────────────────
# 吊销列表：veroguard/data/revoked_official_tokens.json
#   {"revoked_issued_at": ["2026-08-23T12:17:24+00:00"], "updated_at": "..."}
# 由发版方维护并随安全更新分发；文件缺失/损坏时视为空列表（不阻断本地判官方）。
REVOKED_TOKEN_LIST = _project_root() / 'veroguard' / 'data' / 'revoked_official_tokens.json'


def _load_revoked_issued_at() -> set:
    """读取本地官方凭证吊销列表（issued_at 字符串集合）。"""
    try:
        data = json.loads(REVOKED_TOKEN_LIST.read_text(encoding='utf-8'))
        return set(data.get('revoked_issued_at', []))
    except (OSError, json.JSONDecodeError):
        return set()


def _is_official_token_revoked(token: dict) -> bool:
    """官方凭证吊销判定：凭证 issued_at 命中吊销列表即视为吊销。"""
    issued_at = str(token.get('issued_at') or '')
    return bool(issued_at) and issued_at in _load_revoked_issued_at()


def _verify_official_token() -> bool:
    """校验官方版签名凭证（official_token.json + .sig，Ed25519 验签）。

    判定条件缺一不可：
      1. 凭证文件存在（随 verorun-code 私有仓库分发，客户无法获取）
      2. Ed25519 验签通过（内置公钥；客户无私钥无法伪造）
      3. edition == 'official'、version == 1 且未过期
    任一失败 → 不承认官方版（fail-closed，不静默放行）。
    """
    if not _CRYPTO_OK:
        return False
    token_path = _project_root() / OFFICIAL_TOKEN_NAME
    sig_path = _project_root() / OFFICIAL_TOKEN_SIG_NAME
    if not token_path.exists() or not sig_path.exists():
        return False
    try:
        token = json.loads(token_path.read_text(encoding='utf-8'))
        sig = bytes.fromhex(sig_path.read_text(encoding='utf-8').strip())
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(RELEASE_VERIFY_KEY))
        canonical = json.dumps(token, sort_keys=True, separators=(",", ":"))
        pub.verify(sig, canonical.encode('utf-8'))
    except (ValueError, InvalidSignature, json.JSONDecodeError, OSError):
        return False
    if token.get('version') != 1 or token.get('edition') != 'official':
        return False
    expires_at = token.get('expires_at', '')
    if not expires_at:
        return False
    try:
        if datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
            return False
    except ValueError:
        return False
    # VR-SEC: 吊销检查——凭证命中吊销列表即判定无效（防泄露凭证继续生效）
    if _is_official_token_revoked(token):
        return False
    return True


def _is_official_edition() -> bool:
    """官方版判定：VR_EDITION=official（本地标志）∧ 官方签名凭证有效（强校验）。

    官方版拥有全部插件权限，无需单独激活 License。
    客户版（customer）走正常付费校验。即使客户在 .env 写入
    VR_EDITION=official，缺少 verorun-code 私有仓库中的签名凭证
    仍会被判定为非官方版（fail-closed）。
    """
    if os.environ.get('VR_EDITION', '').strip().lower() != 'official':
        return False
    return _verify_official_token()


def _parse_semver(v: str) -> tuple:
    """解析 semver 'x.y.z' 为可比较元组；无法解析时返回 (0,0,0)。"""
    parts = []
    for seg in str(v).strip().split('.')[:3]:
        digits = ''.join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


# 签名清单 semver 缓存：{semver, mtime}，清单文件未变化时复用
_MANIFEST_SEMVER_CACHE: dict = {}


def _signed_manifest_semver() -> Optional[str]:
    """读取并验签完整性清单 manifest.json，返回签名确认的 semver（版本真相）。

    版本真相必须来自 Ed25519 验签的清单，防止客户篡改 VERSION 文件绕过防回滚。
    验签失败/清单缺失 → 返回 None（由调用方按 fail-closed 处理）。
    带 mtime 缓存：进程内首次读取后，清单文件未变化则复用。
    """
    global _MANIFEST_SEMVER_CACHE
    mf = _project_root() / 'veroguard' / 'data' / 'manifest.json'
    sig_file = mf.with_suffix(mf.suffix + '.sig')
    if not mf.exists() or not sig_file.exists():
        return None
    try:
        mtime = mf.stat().st_mtime
    except OSError:
        return None
    if _MANIFEST_SEMVER_CACHE.get('mtime') == mtime:
        return _MANIFEST_SEMVER_CACHE.get('semver')
    if not _CRYPTO_OK:
        return None
    try:
        manifest = json.loads(mf.read_text(encoding='utf-8'))
        sig = bytes.fromhex(sig_file.read_text(encoding='utf-8').strip())
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(RELEASE_VERIFY_KEY))
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        pub.verify(sig, canonical.encode('utf-8'))
        semver = manifest.get('semver')
        if not semver:
            return None
        _MANIFEST_SEMVER_CACHE.update({'semver': semver, 'mtime': mtime})
        return semver
    except (ValueError, InvalidSignature, json.JSONDecodeError, OSError):
        return None


def _version_in_scope(min_version: str, max_version: str, current: str) -> bool:
    """版本区间校验（防回滚核心）。

    当前版本 < min_version（降级）→ False
    当前版本 > max_version（超范围）→ False（max 为空不限制）
    """
    cur = _parse_semver(current)
    if min_version and cur < _parse_semver(min_version):
        return False
    if max_version and cur > _parse_semver(max_version):
        return False
    return True


def _get_license_url() -> str:
    """获取 License 服务 API 基础 URL（区域感知）。
    环境变量 REMOTE_LICENSE_URL 覆盖优先（向后兼容）。
    """
    override = os.environ.get('REMOTE_LICENSE_URL', '')
    if override:
        # VR-SEC: mock:// 前缀不能被 rstrip('/') 剥掉（否则 startswith('mock://') 永远为假）
        if override.startswith('mock://'):
            return override
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
        # VR-SEC (V8): mock 仅允许 dev 环境，其余环境一律拒绝（fail-closed）
        # 与 plugin_manager/routes.py 的 channel=='mock' 判定保持一致
        if os.environ.get('DEPLOY_ENV', '').strip().lower() != 'dev':
            return {'success': False,
                    'error': 'mock license URL disabled outside dev environment'}
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

        # 持久化（2.4 防回滚：记录激活时版本为 min_version）
        _meta = {}
        _cur_v = data.get('min_version') or _signed_manifest_semver()
        if _cur_v:
            _meta['min_version'] = _cur_v
        if data.get('max_version'):
            _meta['max_version'] = data['max_version']
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
            metadata=_meta,
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
        # 2.4 防回滚：离线激活同样记录激活时版本
        _meta = {}
        _cur_v = _signed_manifest_semver()
        if _cur_v:
            _meta['min_version'] = _cur_v
        record = LicenseRecord(
            plugin_id=plugin_id,
            license_key=license_key,
            license_type=LicenseType.ONETIME,
            license_status=LicenseStatus.GRACE,
            site_id=site_id,
            activated_at=datetime.now().isoformat(),
            expires_at=expires,
            offline_token=offline_token,
            metadata=_meta,
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

    def _version_scope_result(self, record) -> Optional[dict]:
        """版本区间校验（2.4 防回滚）：越界返回失败 dict，通过返回 None。"""
        meta = record.metadata or {}
        min_v = meta.get('min_version') or ''
        max_v = meta.get('max_version') or ''
        if not min_v and not max_v:
            return None
        cur_v = _signed_manifest_semver()
        if cur_v is None:
            # fail-closed：存在版本约束但无法确认版本真相
            return {'valid': False, 'status': 'version_out_of_scope',
                    'expires_at': record.expires_at or '',
                    'error': 'version unverifiable'}
        if not _version_in_scope(min_v, max_v, cur_v):
            return {'valid': False, 'status': 'version_out_of_scope',
                    'expires_at': record.expires_at or '',
                    'error': 'version out of scope'}
        return None

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

            # 2.4 防回滚：版本区间校验（越界返回失败 dict）
            _scope = self._version_scope_result(record)
            if _scope:
                return _scope

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
            # VR-SEC (V8): 宽容期设备绑定——许可记录绑定本机 site_id，跨机复制即失效
            if record.site_id and record.site_id != get_site_id():
                return {'valid': False, 'status': 'site_mismatch',
                        'error': 'license bound to another site'}
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
            # 2.4 防回滚：宽容期同样校验版本区间
            _scope = self._version_scope_result(record)
            if _scope:
                return _scope
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
        # 2.4 防回滚：版本包授权同样记录激活时版本
        _meta = {'bundle_id': bundle_id, 'bundle_order_no': order_no}
        _cur_v = _signed_manifest_semver()
        if _cur_v:
            _meta['min_version'] = _cur_v
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
            metadata=_meta,
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
