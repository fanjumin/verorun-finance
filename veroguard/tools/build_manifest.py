#!/usr/bin/env python3
"""
build_manifest.py — 生成 Ed25519 签名版完整性基准清单（Task 5）
===================================================================
用法（发版时运行，需配置 RELEASE_SIGN_KEY）:
    RELEASE_SIGN_KEY=<hex 私钥> python3 veroguard/tools/build_manifest.py \
        --project-dir /opt/verorun \
        --output veroguard/data/manifest.json
    # 输出: veroguard/data/manifest.json + manifest.json.sig（随代码提交分发）

服务器端验签（安装/更新脚本调用）:
    python3 veroguard/tools/build_manifest.py --verify --project-dir /opt/verorun

扫描核心文件（含 plugins/ 下自动发现的插件核心文件 *.py / plugin.json /
migrations/*.sql），计算 SHA256，生成签名清单。私钥 RELEASE_SIGN_KEY 仅存
CI/发版机，服务器端无私钥，本地无法重生成有效清单。
"""
import os
import sys
import json
import hashlib
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# ── 发布签名公钥（Ed25519，hex）──
# 与 deploy/scripts/sign_release.py / veroguard/config.py 同款；
# 私钥 RELEASE_SIGN_KEY 仅存 CI/发版机/本机外部文件，永不入库。
RELEASE_VERIFY_KEY = "a467ea79346e26f8c4fb75ecc07b400b3af86f7c9a7aab9878bb2754b8107ef4"

# ── 需要保护的核心文件（相对于 PROJECT_DIR） ──
PROTECTED_FILES = [
    # Python 核心源代码
    "auth_server.py",
    "auth-center/models/database.py",
    "auth-center/services/jwt_service.py",
    "auth-center/services/unified_auth_service.py",
    "auth-center/services/license_service.py",
    "main_site/app.py",
    "admin/app.py",
    "plugin_manager/manager.py",
    "plugin_manager/license.py",
    # 守护进程自身
    "veroguard/guardian.py",
    "veroguard/config.py",
    "veroguard/modules/health.py",
    "veroguard/modules/integrity.py",
    "veroguard/modules/fingerprint.py",
    "veroguard/modules/communicator.py",
    "veroguard/modules/executor.py",
    "veroguard/modules/runtime.py",
    "veroguard/modules/self_protect.py",
    # 关键插件核心
    "plugins/health_check/checkers.py",
    "plugins/health_check/ai_fixer.py",
    "plugins/health_check/models.py",
    "plugins/vault/dumper.py",
    # chatbot 客户端交付资产：widget 直接下发访客浏览器并执行，
    # 属安全关键路径（P1-7 修复所在），故显式加入保护——不落入 templates 排除区。
    "plugins/chatbot/templates/widget/chatbot-widget.js",
    # 部署脚本
    "deploy/install.sh",
]

def _discover_plugin_files(project_dir: str) -> list:
    """动态发现插件核心文件：*.py + plugin.json + migrations/*.sql。

    排除资源/生成目录（templates/static/i18n/data/workspace 等），
    这些不属于安全关键路径，避免清单体积膨胀与频繁误报。
    """
    plugins_dir = os.path.join(project_dir, "plugins")
    if not os.path.isdir(plugins_dir):
        return []
    found = []
    _skip_dirs = {
        "__pycache__", "_templates", "templates", "static",
        "i18n", "data", "workspace", "screenshots", "images",
        "agents", "prompts",
    }
    for entry in sorted(os.listdir(plugins_dir)):
        pdir = os.path.join(plugins_dir, entry)
        if not os.path.isdir(pdir) or entry.startswith("_"):
            continue
        for root, dirs, files in os.walk(pdir):
            dirs[:] = [d for d in dirs
                       if d not in _skip_dirs and not d.startswith("__")]
            for fname in sorted(files):
                if (fname.endswith(".py") or fname == "plugin.json"
                        or fname.endswith(".sql")):
                    found.append(os.path.relpath(os.path.join(root, fname),
                                                 project_dir))
    return found


# ── 各文件前缀的严重级别 ──
SEVERITY_MAP = [
    ("auth_server.py", "critical"),
    ("auth-center/", "critical"),
    ("plugin_manager/", "critical"),
    ("veroguard/", "critical"),
    ("deploy/", "critical"),
]


def get_severity(filepath: str) -> str:
    for prefix, level in SEVERITY_MAP:
        if filepath.startswith(prefix):
            return level
    return "warning"


def _git_commit(project_dir: str) -> str:
    """读取当前 git 提交（用于 build_id 溯源）。非 git 环境返回空串。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=10, cwd=project_dir,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def build_manifest(project_dir: str) -> dict:
    """扫描 PROJECT_DIR 下的 PROTECTED_FILES + 动态发现的插件核心文件，生成清单"""
    # 静态清单 + 动态发现的插件核心文件（去重）
    files_to_scan = list(PROTECTED_FILES)
    for rel in _discover_plugin_files(project_dir):
        if rel not in files_to_scan:
            files_to_scan.append(rel)

    files = []
    for rel_path in files_to_scan:
        abs_path = os.path.join(project_dir, rel_path)
        if not os.path.exists(abs_path):
            print(f"  [SKIP] {rel_path} — 文件不存在")
            continue
        with open(abs_path, "rb") as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
        # 插件核心文件按 high 级别监控（静态清单已命中的保持原级别）
        # M3 修复（2026-08-29）：统一使用 '/' 分隔的相对路径——Windows 下
        # os.path.relpath 返回反斜杠，会导致 manifest path 与 SEVERITY_MAP 前缀
        # 不匹配（plugins\\x 匹配不到 plugins/），且 veroguard 校验时误报。
        rel_slash = rel_path.replace(os.sep, "/")
        severity = get_severity(rel_slash)
        if rel_slash.startswith("plugins/") and severity == "warning":
            severity = "high"
        files.append({
            "path": rel_slash,
            "hash": file_hash,
            "severity": severity,
        })
        print(f"  [OK] {rel_path} → {file_hash[:16]}...")

    # ── 版本标识（VR-SEC：build_id/semver/edition，用于版本区间校验与降级检测）──
    version_file = os.path.join(project_dir, "VERSION")
    semver = "0.0.0"
    if os.path.isfile(version_file):
        with open(version_file, "r", encoding="utf-8") as f:
            semver = (f.read().strip() or "0.0.0")
    commit = _git_commit(project_dir)
    edition = os.getenv("VR_EDITION") or os.getenv("RELEASE_EDITION") or "standard"

    return {
        "version": 1,
        "generated_at": datetime.now().isoformat(),
        "project_dir": project_dir,
        "build_id": f"VR-{semver}-{commit[:8].upper()}" if commit else f"VR-{semver}-UNKNOWN",
        "semver": semver,
        "edition": edition,
        "git_commit": commit,
        "total_files": len(files),
        "files": files,
    }


def canonical_json(obj: dict) -> str:
    """稳定序列化：key 排序 + 紧凑分隔，保证签名/验签字节一致。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def signing_key():
    """读取 Ed25519 私钥种子（RELEASE_SIGN_KEY / RELEASE_SIGN_KEY_FILE）。未配置或非法时返回 None。"""
    seed = os.getenv("RELEASE_SIGN_KEY", "").strip()
    if not seed:
        key_file = os.getenv("RELEASE_SIGN_KEY_FILE", "").strip()
        if key_file and os.path.isfile(key_file):
            seed = Path(key_file).read_text(encoding="utf-8").strip()
    if not seed:
        return None
    try:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
    except ValueError:
        print("[WARN] RELEASE_SIGN_KEY 不是合法的 hex 私钥种子，跳过签名")
        return None


def write_signed_manifest(manifest: dict, output: str) -> str:
    """写入明文 manifest.json + Ed25519 签名 manifest.json.sig。
    无私钥时返回 'unsigned' 且不写文件（避免未签名残留）。
    """
    key = signing_key()
    if key is None:
        return "unsigned"
    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canonical = canonical_json(manifest)
    out_path.write_text(canonical + "\n", encoding="utf-8")
    sig = key.sign(canonical.encode("utf-8"))
    Path(str(out_path) + ".sig").write_text(sig.hex() + "\n", encoding="utf-8")
    print(f"[OK] manifest.json 已签名（build_id={manifest.get('build_id')}，total_files={manifest.get('total_files')}）")
    return "signed"


def main():
    parser = argparse.ArgumentParser(
        description="生成/校验 VeroGuard 完整性基准清单（Ed25519 签名制）"
    )
    parser.add_argument("--project-dir", required=True,
                        help="项目根目录")
    parser.add_argument("--output",
                        help="输出 manifest.json 路径（同目录生成 manifest.json.sig）；--verify 模式不需要")
    parser.add_argument("--verify", action="store_true",
                        help="校验已有 manifest.json/.sig 的 Ed25519 签名（不生成）")
    args = parser.parse_args()

    if args.verify:
        # 验签模式：供安装/更新脚本在服务器上校验随发布分发的清单
        mf = os.path.join(args.project_dir, "veroguard", "data", "manifest.json")
        sig = mf + ".sig"
        if not os.path.exists(mf) or not os.path.exists(sig):
            print(f"[FAIL] manifest.json/.sig 缺失: {mf}")
            sys.exit(1)
        try:
            pub = Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(RELEASE_VERIFY_KEY))
            canonical = open(mf, encoding="utf-8").read().strip()
            sig_bytes = bytes.fromhex(open(sig, encoding="utf-8").read().strip())
            pub.verify(sig_bytes, canonical.encode("utf-8"))
        except Exception as e:
            print(f"[FAIL] manifest 验签失败: {e}")
            sys.exit(1)
        print(f"[OK] manifest 验签通过（{os.path.basename(mf)}）")
        sys.exit(0)

    if not args.output:
        print("[FAIL] --output 必填（生成模式）")
        sys.exit(1)
    print(f"扫描目录: {args.project_dir}")
    manifest = build_manifest(args.project_dir)
    print(f"共 {manifest['total_files']} 个文件")

    if write_signed_manifest(manifest, args.output) == "unsigned":
        print("[FAIL] 未配置 RELEASE_SIGN_KEY，无法生成签名清单（发版必须签名）")
        sys.exit(1)


if __name__ == "__main__":
    main()
