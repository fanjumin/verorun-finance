#!/usr/bin/env python3
"""
Plugin Manager — 插件审核引擎（AI 审核网关 · 批次2）
====================================================
上传插件进入 pending 队列后，调用本模块进行规则引擎自动审核，
输出结构化报告供人工审批兜底（插件标准 §16：AI 辅助 + 人工审批）。

检查项：
  1. 官方水印/签名复核（复用 watermark.detect_official_watermark）
  2. 结构合规（plugin.json 必填字段 / identifier 格式 / 入口 __init__.py）
  3. 官方插件代码相似度比对（文件级 SHA256 精确匹配，识别直接复制）
  4. 危险代码扫描（os.system / subprocess / eval / exec / pickle / socket 等）

审核结论（audit_status）:
  reject  — 硬拒（官方水印/签名命中、结构不合规、命中危险代码）
  manual  — 需人工复核（官方 identifier 白名单命中、与官方文件完全一致）
  pass    — 通过（无风险特征，等待 approve 安装）
"""

import os
import re
import json
import ast
import hashlib
from typing import Any, Dict, List, Optional

from .watermark import detect_official_watermark, OFFICIAL_PLUGIN_IDS

# 必填字段
_REQUIRED_FIELDS = ('identifier', 'name', 'version', 'min_app_version')
_IDENTIFIER_RE = re.compile(r'^[a-z0-9_]+$')

# 危险代码模式（正则，忽略大小写）
_DANGEROUS_PATTERNS = [
    (r'\bos\.system\s*\(', 'os.system 命令执行'),
    (r'\bos\.popen\s*\(', 'os.popen 命令执行'),
    (r'\bsubprocess\s*(\.|\[)', 'subprocess 子进程调用'),
    (r'\bPopen\s*\(', 'Popen 子进程调用'),
    (r'\beval\s*\(', 'eval 动态执行'),
    (r'\bexec\s*\(', 'exec 动态执行'),
    (r'\bpickle\s*\.\s*(loads?|dumps)\s*\(', 'pickle 反序列化'),
    (r'\bmarshal\s*\.\s*loads\s*\(', 'marshal 反序列化'),
    (r'\b__import__\s*\(', '动态导入'),
    (r'\bsocket\s*\.\s*connect\s*\(', 'socket 外连'),
    (r'\bshutil\s*\.\s*rmtree\s*\(', 'shutil.rmtree 递归删除'),
]

# 参与相似度比对的文件后缀
_SIM_EXTS = ('.py', '.json')

# 水印/签名文件不入官方指纹库（M4 修复）
_WM_FILES = {'verorun.manifest', 'verorun.signature'}

# 超大文件只扫首尾各 1MB，避免整文件载入内存（M1 修复）
_MAX_SCAN_BYTES = 1024 * 1024

# 默认跳过的目录
_SKIP_DIRS = {'__pycache__', '.git'}


def _sha256_file(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            sha.update(chunk)
    return sha.hexdigest()


def _walk_files(root: str):
    """遍历目录下全部文件（跳过缓存目录）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            yield os.path.join(dirpath, fname)


def build_official_fingerprint(plugins_root: str) -> Dict[str, Dict[str, str]]:
    """构建官方插件文件指纹库 {identifier: {relpath: sha256}}。

    仅收录白名单内的官方插件目录（避免把用户已安装的自研插件计入）。
    """
    lib = {}
    if not os.path.isdir(plugins_root):
        return lib
    for entry in sorted(os.listdir(plugins_root)):
        if entry.startswith('_') or entry.startswith('.'):
            continue
        if entry not in OFFICIAL_PLUGIN_IDS:
            continue
        pdir = os.path.join(plugins_root, entry)
        if not os.path.isdir(pdir) or not os.path.isfile(os.path.join(pdir, 'plugin.json')):
            continue
        files = {}
        for path in _walk_files(pdir):
            if not path.endswith(_SIM_EXTS):
                continue
            rel = os.path.relpath(path, pdir).replace('\\', '/')
            if rel in _WM_FILES:  # M4 修复：水印/签名文件不入指纹
                continue
            try:
                files[rel] = _sha256_file(path)
            except OSError:
                continue
        lib[entry] = files
    return lib


def _check_structure(plugin_dir: str) -> List[str]:
    """结构合规检查，返回问题列表（空列表 = 合规）。"""
    reasons = []
    meta_path = os.path.join(plugin_dir, 'plugin.json')
    if not os.path.isfile(meta_path):
        return ['缺少 plugin.json']
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
    except json.JSONDecodeError:
        return ['plugin.json 不是合法 JSON']
    except OSError as e:
        return [f'plugin.json 读取失败: {e}']

    missing = [k for k in _REQUIRED_FIELDS if not str(meta.get(k) or '').strip()]
    if missing:
        reasons.append(f'缺少必填字段: {", ".join(missing)}')

    identifier = str(meta.get('identifier') or '').strip().lower()
    if identifier and not _IDENTIFIER_RE.match(identifier):
        reasons.append(f'identifier 格式非法: {identifier}')

    # 入口文件检查：支持插件目录内直接 __init__.py 或 <identifier>/ 子目录
    if os.path.isfile(os.path.join(plugin_dir, '__init__.py')):
        pass
    elif identifier and os.path.isfile(os.path.join(plugin_dir, identifier, '__init__.py')):
        pass
    else:
        reasons.append('缺少插件入口 __init__.py')
    return reasons


def _scan_dangerous(plugin_dir: str) -> List[str]:
    """危险代码扫描，返回命中列表（超大 .py 仅扫头尾，防内存暴涨）。"""
    hits = []
    for path in _walk_files(plugin_dir):
        if not path.endswith('.py'):
            continue
        try:
            _sz = os.path.getsize(path)
            if _sz > _MAX_SCAN_BYTES * 4:
                continue  # 超大文件交由人工复核
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                if _sz <= _MAX_SCAN_BYTES:
                    text = f.read()
                else:
                    head = f.read(_MAX_SCAN_BYTES)
                    f.seek(max(0, _sz - _MAX_SCAN_BYTES))
                    tail = f.read(_MAX_SCAN_BYTES)
                    text = head + tail
        except OSError:
            continue
        for pat, label in _DANGEROUS_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                hits.append(f'{label}（{os.path.relpath(path, plugin_dir)}）')
    # 去重保序
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _load_plugin_meta(plugin_dir: str) -> Dict[str, Any]:
    """读取插件 plugin.json（失败返回空 dict，供权限一致性校验）。"""
    meta_path = os.path.join(plugin_dir, 'plugin.json')
    if not os.path.isfile(meta_path):
        return {}
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _scan_dangerous_ast(plugin_dir: str) -> List[str]:
    """AST 级危险代码扫描（补充正则盲区，P0-1）。

    正则 _DANGEROUS_PATTERNS 已覆盖 os.system()/eval() 等直接调用；
    AST 层补抓可确定性判定的间接/混淆调用：
      1) getattr(任意, 'system'/'popen'/'eval' 等) 动态取危险函数
      2) 任意对象 .system()/.popen()/.Popen()（别名/局部变量绕过 os 前缀）
      3) __import__('os').system(...) 链式调用
    命中即并入「危险代码」判定（reject）。
    """
    hits = []
    for path in _walk_files(plugin_dir):
        if not path.endswith('.py'):
            continue
        try:
            _sz = os.path.getsize(path)
            if _sz > _MAX_SCAN_BYTES * 4:
                continue  # 超大文件交由人工复核
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                src = f.read()
        except OSError:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        rel = os.path.relpath(path, plugin_dir).replace('\\', '/')
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # ① getattr 动态取危险函数
            if (isinstance(fn, ast.Name) and fn.id == 'getattr'
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and str(node.args[1].value) in ('system', 'popen', 'Popen', 'eval', 'exec')):
                hits.append(f'getattr 动态获取危险函数 {node.args[1].value}()（{rel}）')
                continue
            # ② 任意对象 .system()/.popen()/.Popen()（含 __import__('os').system 链式）
            if isinstance(fn, ast.Attribute) and fn.attr in ('system', 'popen', 'Popen'):
                if isinstance(fn.value, ast.Call) and isinstance(fn.value.func, ast.Name) and fn.value.func.id == '__import__':
                    hits.append(f'__import__().{fn.attr} 动态命令执行（{rel}）')
                elif not (isinstance(fn.value, ast.Name) and fn.value.id == 'os'):
                    hits.append(f'.{fn.attr}() 命令执行（{rel}）')
                continue
    # 去重保序
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _call_root_module(fn) -> Optional[str]:
    """提取调用表达式根的模块名：urllib.request.urlopen → 'urllib'；requests.get → 'requests'；
    无法确定（局部变量/调用链）返回 None。"""
    node = fn
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_network_call(fn) -> bool:
    """判定 Call.func 是否命中常见网络请求写法。

    覆盖：裸名 urlopen/urlretrieve、requests.*、requests.Session().xxx、
    aiohttp.ClientSession().xxx、urllib.request.urlopen/urlretrieve。
    仅匹配确定性网络动词，避免把 urllib.parse、aiohttp.web（服务端）等误判为外连。
    """
    _verbs = ('get', 'post', 'put', 'delete', 'patch', 'request')
    if isinstance(fn, ast.Name) and fn.id in ('urlopen', 'urlretrieve'):
        return True  # from urllib.request import urlopen 等
    if not isinstance(fn, ast.Attribute):
        return False
    root = _call_root_module(fn)
    if root == 'urllib':
        return fn.attr in ('urlopen', 'urlretrieve')
    if root in ('requests', 'aiohttp') and fn.attr in _verbs:
        return True
    # requests.Session().xxx / aiohttp.ClientSession().xxx（fn.value 是 Call，root 提取不到）
    if (fn.attr in _verbs
            and isinstance(fn.value, ast.Call)
            and isinstance(fn.value.func, ast.Attribute)
            and fn.value.func.attr in ('Session', 'ClientSession')
            and _call_root_module(fn.value.func.value) in ('requests', 'aiohttp')):
        return True
    return False


def _check_permission_consistency(plugin_dir: str, meta: Dict[str, Any]) -> List[str]:
    """权限一致性校验（P0-1）：代码实际使用的网络外连/文件写操作，
    必须在 plugin.json permissions 中显式声明。

    仅覆盖可确定性判定的写法，避免误伤：
      - 网络请求（urlopen/urlretrieve、requests.*、requests.Session().*、
        aiohttp.ClientSession().*、urllib.request.urlopen/urlretrieve）
      - 文件写/删除（open(...,'w'/'a'/'x')、os.remove/unlink、shutil.rmtree/move/rename）

    读文件为插件常规操作，不纳入。结果以「权限不一致：」前缀进入人工复核（manual），不直接拒绝。
    """
    declared = set(meta.get('permissions') or [])
    issues = []
    for path in _walk_files(plugin_dir):
        if not path.endswith('.py'):
            continue
        try:
            _sz = os.path.getsize(path)
            if _sz > _MAX_SCAN_BYTES * 4:
                continue
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                src = f.read()
        except OSError:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        rel = os.path.relpath(path, plugin_dir).replace('\\', '/')
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # 网络外连（含 urllib.request.urlopen / requests.Session().xxx 链式）
            if 'network:request' not in declared:
                if _is_network_call(fn):
                    issues.append(f'权限不一致：代码使用网络请求但未声明 network:request（{rel}）')
            # 文件写/删除
            if 'filesystem:write' not in declared:
                fs = False
                if isinstance(fn, ast.Name) and fn.id == 'open' and len(node.args) >= 2:
                    mode = node.args[1]
                    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                        mode_str = mode.value.lstrip()
                        if mode_str and mode_str[0] in ('w', 'a', 'x'):
                            fs = True
                elif (isinstance(fn, ast.Attribute)
                      and fn.attr in ('remove', 'unlink', 'rmtree', 'rename', 'move')
                      and isinstance(fn.value, ast.Name) and fn.value.id in ('os', 'shutil')):
                    fs = True
                if fs:
                    issues.append(f'权限不一致：代码执行文件写/删除但未声明 filesystem:write（{rel}）')
    # 去重
    seen, out = set(), []
    for h in issues:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _check_similarity(plugin_dir: str,
                      official_lib: Dict[str, Dict[str, str]]) -> List[str]:
    """与官方插件做文件级 SHA256 精确匹配，返回完全一致的命中列表。"""
    matches = []
    for path in _walk_files(plugin_dir):
        if not path.endswith(_SIM_EXTS):
            continue
        rel = os.path.relpath(path, plugin_dir).replace('\\', '/')
        if rel in ('verorun.manifest', 'verorun.signature'):
            continue
        try:
            sha = _sha256_file(path)
        except OSError:
            continue
        for pid, files in official_lib.items():
            if sha in files.values():
                official_rel = next((k for k, v in files.items() if v == sha), '?')
                matches.append(f'{rel} 与官方插件 {pid}/{official_rel} 完全一致')
                break
    return matches


def review_plugin(plugin_dir: str, watermark_result: Dict[str, Any] = None,
                  plugins_root: str = None) -> Dict[str, Any]:
    """对 pending 插件执行规则引擎审核。

    Args:
        plugin_dir:        待审核插件目录（plugins/.pending/<id>/）
        watermark_result:  预先的水印检测结果（缺省则内部重新检测）
        plugins_root:      官方插件目录（缺省为项目 plugins/）

    Returns:
        {'status': 'reject' | 'manual' | 'pass',
         'reasons': [...],
         'report': {...}}
    """
    if watermark_result is None:
        watermark_result = detect_official_watermark(plugin_dir)

    report = {'watermark': watermark_result}
    reasons: List[str] = []

    # ① 官方水印/签名
    wm = watermark_result or {}
    if wm.get('official'):
        if wm.get('method') in ('signature', 'manifest', 'wm_comment', 'wm_meta'):
            reasons.append(f'命中官方水印/签名（{wm.get("reason", "")}）')
        else:
            reasons.append(f'命中官方标识，需人工复核（{wm.get("reason", "")}）')

    # ② 结构合规
    structure = _check_structure(plugin_dir)
    report['structure'] = structure
    reasons.extend(structure)

    # ③ 官方插件相似度比对
    official_lib = build_official_fingerprint(plugins_root or
        os.path.join(os.path.dirname(__file__), '..', 'plugins'))
    similarity = _check_similarity(plugin_dir, official_lib)
    report['similarity'] = similarity
    if similarity:
        reasons.append(f'与官方插件文件完全一致 {len(similarity)} 处，需人工复核')

    # ④ 危险代码扫描
    dangerous = _scan_dangerous(plugin_dir)
    report['dangerous'] = dangerous
    reasons.extend(f'危险代码：{d}' for d in dangerous)

    # ⑤ AST 级危险扫描（补充正则盲区）
    ast_dangerous = _scan_dangerous_ast(plugin_dir)
    report['ast_dangerous'] = ast_dangerous
    reasons.extend(f'危险代码：{d}' for d in ast_dangerous)

    # ⑥ 权限一致性校验（声明必须覆盖实际使用，进入人工复核）
    perm_issues = _check_permission_consistency(plugin_dir, _load_plugin_meta(plugin_dir))
    report['permission_consistency'] = perm_issues
    reasons.extend(perm_issues)

    # 判定优先级：reject > manual > pass
    status = 'pass'
    for r in reasons:
        if ('官方水印/签名' in r or r.startswith('缺少') or '非法' in r
                or r.startswith('危险代码')):
            status = 'reject'
            break
    if status == 'pass':
        for r in reasons:
            if '人工复核' in r or '完全一致' in r or '权限不一致' in r:
                status = 'manual'
                break

    return {'status': status, 'reasons': reasons, 'report': report}
