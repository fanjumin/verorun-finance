#!/usr/bin/env python3
"""
Plugin Manager — 官方插件水印/签名引擎
========================================
VeroRun 官方插件水印体系（批次 1：核心检测）。

三道防线：
  A. 生成侧（官方发布用，tools/publish_plugin.py 打包前调用）：
     write_watermark() 在插件**打包临时副本**中写入
     verorun.manifest（全文件 SHA256 清单）+ verorun.signature（HMAC 签名），
     并在 plugin.json 注入 _wm 隐藏字段、向首个 .py 追加 # vr-wm:<hex> 注释水印。
     注意：只作用于打包副本，严禁直接用于 plugins/ 源码目录。

  B. 检测侧（上传拦截用，routes.upload_plugin 解压后调用）：
     detect_official_watermark() 扫描解压目录，按优先级判定官方插件二次打包：
       ① 签名验签通过 / 存在官方清单  → signature / manifest
       ② 命中隐藏注释水印            → wm_comment
       ③ plugin.json 含 _wm 字段      → wm_meta
       ④ identifier/作者命中官方白名单 → whitelist（兜底，供 AI 复核）

密钥：
  - 签名与水印均使用 HMAC-SHA256，密钥来源优先级：
    PLUGIN_SIGNING_SECRET > PLUGIN_LICENSE_SECRET（零新增配置项）。
  - 无密钥环境下（本地/CI 未配置）：
    生成侧仅写入无签名的 manifest 与注释水印，检测侧仍可通过
    manifest / 注释水印 / 白名单命中判定，不依赖验签。
"""

import os
import re
import json
import hashlib
import hmac
from typing import Any, Dict

# ── 官方插件白名单（31 个已上线官方插件 identifier）────────────────
OFFICIAL_PLUGIN_IDS = {
    'ads', 'ali_api', 'analytics', 'captcha_embedded', 'chatbot',
    'content_factory', 'coupons', 'currency_converter', 'email',
    'enterprise_verify', 'health_check', 'im_gateway', 'logistics',
    'memory_engine', 'mini_app_builder', 'oauth_config', 'order_notify',
    'payment', 'project_workspace', 'reviews', 'shop', 'site_builder',
    'site_domains', 'sms', 'social_push', 'subscription', 'vault',
    'verification', 'veroscholar', 'visitor_profile', 'wishlist',
}

# 官方作者标识（plugin.json author 字段兜底匹配）
OFFICIAL_AUTHORS = {'VeroRun'}

# 打包产物文件名
MANIFEST_NAME = 'verorun.manifest'
SIGNATURE_NAME = 'verorun.signature'

# 隐藏水印特征
WM_COMMENT_PATTERN = re.compile(r'#\s*vr-wm:[0-9a-f]{32}\b')
WM_META_FIELD = '_wm'

# 不可辩驳的官方包特征（上传/批准时唯一硬拒集合）
# 修复 M2/M3：仅「签名验签通过」硬拒；manifest/注释水印/_wm/白名单降级进队列复核
WM_HARD = ('signature',)

# 受扫描的源码文件后缀
_SOURCE_EXTS = ('.py', '.json')

# 默认跳过的目录/文件
_SKIP_DIRS = {'__pycache__', '.git'}
_SKIP_NAMES = {'.gitignore'}


def _signing_secret() -> str:
    """读取签名/水印密钥（PLUGIN_SIGNING_SECRET 优先，回退 PLUGIN_LICENSE_SECRET）。"""
    return (os.environ.get('PLUGIN_SIGNING_SECRET') or
            os.environ.get('PLUGIN_LICENSE_SECRET') or '').strip()


def _hmac_hex(secret: str, payload: str) -> str:
    """HMAC-SHA256 派生十六进制摘要。"""
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _iter_files(root: str):
    """遍历插件目录下全部受保护文件，产出相对路径（正斜杠）。

    排除 manifest / signature 自身，避免清单自引用。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if fname.endswith('.pyc') or fname in _SKIP_NAMES:
                continue
            if fname in (MANIFEST_NAME, SIGNATURE_NAME):
                continue
            yield os.path.relpath(os.path.join(dirpath, fname), root).replace('\\', '/')


def build_manifest(plugin_dir: str, identifier: str, version: str) -> Dict[str, Any]:
    """计算插件目录全部文件 SHA256，生成 manifest dict。"""
    files = {}
    for rel in _iter_files(plugin_dir):
        path = os.path.join(plugin_dir, rel)
        sha = hashlib.sha256()
        try:
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    sha.update(chunk)
        except OSError:
            continue
        files[rel] = sha.hexdigest()
    return {
        'identifier': identifier,
        'version': version,
        'files': files,
    }


# ── 生成侧（官方发布用）──────────────────────────────────────────────

def write_watermark(plugin_dir: str, identifier: str, version: str,
                    secret: str = None) -> None:
    """向插件**打包副本**写入官方签名 + 隐藏水印。

    Args:
        plugin_dir: 插件目录（应为打包用临时副本，不得是 plugins/ 源码目录）
        identifier: 插件标识
        version:    插件版本号
        secret:     HMAC 密钥；缺省时从环境变量读取，为空则跳过签名（仅清单/注释水印）

    Note:
        顺序保证：先注入隐藏水印，最后构建 manifest —— manifest 记录的是
        最终文件哈希，与运行时 verify_manifest 逐文件比对结果一致。
    """
    secret = (secret or '').strip() or _signing_secret()

    # 1. 隐藏水印先注入：plugin.json 加 _wm 字段
    meta_path = os.path.join(plugin_dir, 'plugin.json')
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            meta[WM_META_FIELD] = _hmac_hex(secret or 'nokey', f'{identifier}:{version}')
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, OSError):
            pass

    # 2. 散落注释水印：向首个 .py 文件追加确定性水印注释行
    stamp = _hmac_hex(secret or 'nokey', f'{identifier}:{version}:comment')[:32]
    _wm_injected = False
    for dirpath, dirnames, filenames in os.walk(plugin_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in sorted(filenames):
            if not fname.endswith('.py'):
                continue
            path = os.path.join(dirpath, fname)
            try:
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(f'\n# vr-wm:{stamp}\n')
                _wm_injected = True
            except OSError:
                continue
            break  # 注入一个 .py 即可作为官方特征
        if _wm_injected:
            break

    # 3. 最后构建 manifest（此时文件为最终形态），写入清单 + 签名
    manifest = build_manifest(plugin_dir, identifier, version)
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    manifest_path = os.path.join(plugin_dir, MANIFEST_NAME)
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    if secret:
        sig = _hmac_hex(secret, canonical)
        sig_path = os.path.join(plugin_dir, SIGNATURE_NAME)
        with open(sig_path, 'w', encoding='utf-8') as f:
            f.write(sig)


# ── 检测侧（上传拦截用）──────────────────────────────────────────────

def detect_official_watermark(plugin_dir: str, secret: str = None) -> Dict[str, Any]:
    """扫描插件目录，判定是否为官方插件二次打包。

    Args:
        plugin_dir: 已解压的插件目录（upload 流程中）
        secret:     HMAC 密钥；缺省从环境变量读取

    Returns:
        {
            'official': bool,   # 判定为官方插件二次打包
            'identifier': str,  # 命中的官方 identifier（可能为空）
            'version': str,
            'method': str,      # signature | manifest | wm_comment | wm_meta | whitelist | ''
            'reason': str,
        }
    """
    result = {'official': False, 'identifier': '', 'version': '',
              'method': '', 'reason': ''}
    secret = (secret or '').strip() or _signing_secret()

    # ① 官方清单 + 签名
    manifest_path = os.path.join(plugin_dir, MANIFEST_NAME)
    sig_path = os.path.join(plugin_dir, SIGNATURE_NAME)
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
            identifier = str(manifest.get('identifier') or '')
            version = str(manifest.get('version') or '')

            if os.path.isfile(sig_path):
                with open(sig_path, 'r', encoding='utf-8') as f:
                    sig = f.read().strip()
                if secret and hmac.compare_digest(
                        sig, _hmac_hex(secret, canonical)):
                    result.update({'official': True, 'identifier': identifier,
                                   'version': version, 'method': 'signature',
                                   'reason': '官方签名验签通过（verorun.signature）'})
                    return result
            result.update({'official': True, 'identifier': identifier,
                           'version': version, 'method': 'manifest',
                           'reason': '存在官方打包清单 verorun.manifest'})
            return result
        except (json.JSONDecodeError, OSError):
            pass

    # ② 隐藏注释水印
    for dirpath, dirnames, filenames in os.walk(plugin_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(_SOURCE_EXTS):
                continue
            path = os.path.join(dirpath, fname)
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    head = f.read(65536)
                if WM_COMMENT_PATTERN.search(head):
                    result.update({'official': True, 'method': 'wm_comment',
                                   'reason': '命中隐藏注释水印 vr-wm:'
                                             f'（{os.path.relpath(path, plugin_dir)}）'})
                    return result
            except OSError:
                continue

    # ③ plugin.json 隐藏字段
    meta_path = os.path.join(plugin_dir, 'plugin.json')
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            wm = meta.get(WM_META_FIELD)
            if isinstance(wm, str) and len(wm) >= 32:
                result.update({'official': True,
                               'identifier': str(meta.get('identifier') or ''),
                               'version': str(meta.get('version') or ''),
                               'method': 'wm_meta',
                               'reason': 'plugin.json 含官方水印字段 _wm'})
                return result
        except (json.JSONDecodeError, OSError):
            pass

    # ④ 官方白名单兜底（identifier / author 命中）
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            identifier = str(meta.get('identifier') or '').strip().lower()
            author = str(meta.get('author') or '').strip()
            if identifier and identifier in OFFICIAL_PLUGIN_IDS:
                result.update({'official': True, 'identifier': identifier,
                               'version': str(meta.get('version') or ''),
                               'method': 'whitelist',
                               'reason': f'identifier {identifier} 命中官方插件白名单'})
            elif author in OFFICIAL_AUTHORS and identifier:
                result.update({'official': True, 'identifier': identifier,
                               'version': str(meta.get('version') or ''),
                               'method': 'whitelist',
                               'reason': f'author={author} 为官方作者'})
        except (json.JSONDecodeError, OSError):
            pass

    return result


# ── 运行时校验（批次3，供 manager.enable 调用）────────────────────────

def _sha256_file(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            sha.update(chunk)
    return sha.hexdigest()


def _safe_in_dir(base: str, rel: str) -> bool:
    """校验清单内相对路径不越出插件目录（H2 修复）。

    拒绝绝对路径、`..` 上跳、符号链接逃逸。
    """
    norm = os.path.normpath(rel).replace('\\', '/')
    if os.path.isabs(norm) or norm == '..' or norm.startswith('../'):
        return False
    real_base = os.path.realpath(base)
    real_path = os.path.realpath(os.path.join(real_base, norm))
    return real_path == real_base or real_path.startswith(real_base + os.sep)


def verify_manifest(plugin_dir: str) -> Dict[str, Any]:
    """校验已安装插件目录文件与 verorun.manifest 记录的一致性。

    Args:
        plugin_dir: 已安装插件目录

    Returns:
        {'valid': bool, 'checked': bool, 'mismatched': [str]}

    兼容策略（批次3 · 新包严格 / 旧包宽松）：
      - 携带官方清单（verorun.manifest）的包 → 逐文件比对，不一致即 invalid；
      - 无清单的旧包 → checked=False，valid=True（放行，不影响存量插件）。
    """
    manifest_path = os.path.join(plugin_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        return {'valid': True, 'checked': False, 'mismatched': []}

    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {'valid': False, 'checked': True, 'mismatched': [MANIFEST_NAME]}

    recorded = manifest.get('files', {})
    mismatched = []
    for rel, expect in recorded.items():
        # H2 修复：拒绝越界路径（../、绝对路径、符号链接逃逸）
        if not _safe_in_dir(plugin_dir, rel):
            mismatched.append(rel)
            continue
        path = os.path.join(plugin_dir, rel)
        if not os.path.isfile(path):
            mismatched.append(rel)
            continue
        try:
            actual = _sha256_file(path)
        except OSError:
            mismatched.append(rel)
            continue
        if actual != expect:
            mismatched.append(rel)

    # 清单外的多余文件不拦截（插件运行期可能生成临时/缓存文件）
    return {'valid': len(mismatched) == 0, 'checked': True,
            'mismatched': mismatched}
