#!/usr/bin/env python3
"""
regenerate_manifest.py — 幂等重签 VeroGuard 完整性清单（发版/CI 操作工具）
=====================================================================
目标：把 build_manifest.py 的“生成 + 签名 + 验签”封装成一条在发版/CI 上
可重复执行、缺私钥即失败（绝不覆盖现网有效清单）的幂等命令。

用法（需具备 RELEASE_SIGN_KEY，仅发版机/CI 持有；服务器端无私钥）：
    RELEASE_SIGN_KEY=<hex 私钥种子> python veroguard/tools/regenerate_manifest.py \
        --project-dir <仓库根目录>

行为（幂等契约）：
    1. 未配置 RELEASE_SIGN_KEY / RELEASE_SIGN_KEY_FILE → 立即 exit(1)，
       不产出任何 manifest 文件，绝不把“未签名清单”压上现网；
    2. 重新扫描 PROTECTED_FILES + 动态发现的插件核心文件，计算 SHA256；
    3. 写入 veroguard/data/manifest.json + manifest.json.sig；
    4. 用 RELEASE_VERIFY_KEY 对本地产物立即自检，失败则报错退出。
    可安全重复执行：每次成功都产出自洽且通过验签的清单，无遗留副作用。

说明：本地环境无私钥时永远走第 1 条失败路径——这正是设计意图，
有效签名清单只能由持有私钥的发版机/CI 生成。
"""
import argparse
import importlib.util
import os
import sys


def _load_build_manifest(tools_dir: str):
    """不依赖包布局，直接按文件路径加载 build_manifest.py。"""
    path = os.path.join(tools_dir, "build_manifest.py")
    spec = importlib.util.spec_from_file_location("build_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    parser = argparse.ArgumentParser(
        description="幂等重签 VeroGuard 完整性清单（发版/CI）"
    )
    parser.add_argument("--project-dir", required=True, help="仓库根目录")
    args = parser.parse_args()

    tools_dir = os.path.dirname(os.path.abspath(__file__))
    bm = _load_build_manifest(tools_dir)

    # 幂等前提：无私钥立即失败，禁止覆盖现网有效清单
    if bm.signing_key() is None:
        print("[FAIL] 未配置 RELEASE_SIGN_KEY / RELEASE_SIGN_KEY_FILE，"
              "无法重签（禁止覆盖现网有效清单）。")
        sys.exit(1)

    out = os.path.join(args.project_dir, "veroguard", "data", "manifest.json")
    manifest = bm.build_manifest(args.project_dir)
    if bm.write_signed_manifest(manifest, out) != "signed":
        print("[FAIL] 重签失败。")
        sys.exit(1)
    print(f"[OK] 重签完成 build_id={manifest.get('build_id')} "
          f"total_files={manifest.get('total_files')}")

    # 自检：用 RELEASE_VERIFY_KEY 对刚写入的清单验签，确保交付物有效
    canonical_bytes = open(out, encoding="utf-8").read().strip().encode("utf-8")
    sig_bytes = bytes.fromhex(open(out + ".sig", encoding="utf-8").read().strip())
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(bm.RELEASE_VERIFY_KEY))
    pub.verify(sig_bytes, canonical_bytes)
    print("[OK] 自检验签通过（manifest.json / manifest.json.sig）")


if __name__ == "__main__":
    main()