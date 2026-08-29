#!/usr/bin/env python3
"""
build_guardian.py — Nuitka 编译 VeroGuard 为独立二进制（Phase 5）
==================================================================
将 veroguard/ 编译为单一可执行文件，对抗逆向工程。

前置条件:
    pip install nuitka

用法:
    python3 veroguard/compile/build_guardian.py
    # 输出: veroguard/dist/verorun-guardian.bin

注意:
    - 编译后需在目标 Linux 服务器上测试
    - 首次编译需下载 Python 依赖的头文件，耗时较长
    - 建议在 CI/CD 中执行，而非开发机
"""
import os
import subprocess
import sys
import shutil

GUARDIAN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(GUARDIAN_DIR)
OUTPUT_DIR = os.path.join(GUARDIAN_DIR, 'dist')
ENTRY_POINT = os.path.join(GUARDIAN_DIR, 'guardian.py')
OUTPUT_BIN = os.path.join(OUTPUT_DIR, 'verorun-guardian.bin')

# ── Nuitka 编译参数 ──
NUITKA_ARGS = [
    sys.executable, '-m', 'nuitka',
    '--standalone',
    '--onefile',
    '--follow-imports',
    # 包含项目内部模块
    '--include-package=veroguard',
    '--include-package=veroguard.modules',
    # 嵌入数据文件
    '--include-data-file=' + os.path.join(GUARDIAN_DIR, 'data', 'manifest.json') + '=data/manifest.json',
    '--include-data-file=' + os.path.join(GUARDIAN_DIR, 'data', 'manifest.json.sig') + '=data/manifest.json.sig',
    # 输出
    f'--output-dir={OUTPUT_DIR}',
    f'--output-filename=verorun-guardian.bin',
    # 安全：移除调试信息
    '--remove-output',
    '--no-deployment',
    # 优化
    '--assume-yes-for-downloads',
    # 入口
    ENTRY_POINT,
]


def check_nuitka():
    """检查 Nuitka 是否安装"""
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'nuitka', '--version'],
            capture_output=True, text=True, timeout=10
        )
        print(f"[OK] Nuitka version: {result.stdout.strip()}")
        return True
    except Exception as e:
        print(f"[FAIL] Nuitka not installed: {e}")
        print("  Install with: pip install nuitka")
        return False


def clean_output():
    """清理上次编译输出"""
    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
        print(f"[OK] Cleaned output: {OUTPUT_DIR}")
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def build():
    """执行编译"""
    print(f"[i] Entry point: {ENTRY_POINT}")
    print(f"[i] Output:     {OUTPUT_BIN}")

    clean_output()

    print("[i] Running Nuitka...")
    result = subprocess.run(NUITKA_ARGS, cwd=PROJECT_DIR)

    if result.returncode == 0 and os.path.exists(OUTPUT_BIN):
        size_mb = os.path.getsize(OUTPUT_BIN) / (1024 * 1024)
        print(f"\n[OK] Build successful: {OUTPUT_BIN} ({size_mb:.1f} MB)")
        # 显示 file 信息
        try:
            file_info = subprocess.run(
                ['file', OUTPUT_BIN], capture_output=True, text=True
            )
            print(f"     {file_info.stdout.strip()}")
        except Exception:
            pass
    else:
        print(f"\n[FAIL] Build failed (exit code: {result.returncode})")
        sys.exit(1)


if __name__ == '__main__':
    if not check_nuitka():
        sys.exit(1)
    build()
