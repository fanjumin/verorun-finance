#!/usr/bin/env python3
"""
VeroGuard — 统一守护进程配置（修正版）
============================================
所有参数优先级：环境变量 > 默认值
"""

import os

# ── 健康监控 ───────────────────────────────────
TARGETS = [
    "http://127.0.0.1:8085/health",
    "http://127.0.0.1:8081/health",
    "http://127.0.0.1:8084/health",
    "http://127.0.0.1:8083/health",
]

# 修正：使用 install.sh 创建的实际 systemd 服务名
SERVICE_MAP = {
    "http://127.0.0.1:8085/health": "verorun-health",
    "http://127.0.0.1:8081/health": "verorun-main",
    "http://127.0.0.1:8084/health": "verorun-admin",
    "http://127.0.0.1:8083/health": "verorun-auth",
}

CHECK_INTERVAL  = int(os.getenv('GUARDIAN_CHECK_INTERVAL', '30'))
MAX_FAILURES    = int(os.getenv('GUARDIAN_MAX_FAILURES', '3'))
COOLDOWN_SECS   = int(os.getenv('GUARDIAN_COOLDOWN', '300'))
ROLLBACK_TAG    = os.getenv('GUARDIAN_ROLLBACK_TAG', 'stable')
WEBHOOK_URL     = os.getenv('GUARDIAN_WEBHOOK_URL', '')
PROJECT_DIR     = os.getenv('GUARDIAN_PROJECT_DIR',
                    '/opt/verorun')
LOG_FILE        = os.getenv('GUARDIAN_LOG_FILE',
                    '/var/log/verorun-guardian.log')
# 远程回滚默认关闭：raw.githubusercontent 仅服务公开仓库，分发仓库为私有（raw 404），
# 需部署方显式配置可用的 GUARDIAN_GITHUB_RAW 才启用远程回滚。
GITHUB_RAW_BASE = os.getenv('GUARDIAN_GITHUB_RAW', '')

# ── 回滚文件列表（修正点 6：扩展覆盖守护进程自身） ──
FILES_TO_ROLLBACK = [
    # 守护进程自身
    "veroguard/guardian.py",
    "veroguard/config.py",
    "veroguard/modules/health.py",
    "veroguard/modules/integrity.py",
    "veroguard/modules/fingerprint.py",
    "veroguard/modules/communicator.py",
    "veroguard/modules/executor.py",
    "veroguard/modules/runtime.py",
    # 核心服务文件
    "auth_server.py",
    "auth-center/models/database.py",
    "auth-center/services/jwt_service.py",
    "auth-center/services/license_service.py",
    "main_site/app.py",
    "admin/app.py",
    "plugin_manager/manager.py",
    "plugin_manager/license.py",
    # health check 插件
    "plugins/health_check/routes.py",
    "plugins/health_check/checkers.py",
    "plugins/health_check/ai_fixer.py",
    "plugins/health_check/models.py",
    "plugins/health_check/templates/health.html",
]

# ── 完整性校验 ─────────────────────────────────
INTEGRITY_CHECK_INTERVAL = int(os.getenv('GUARDIAN_INTEGRITY_INTERVAL', '300'))

# 完整性 fail-closed（检测即阻断）：
#   GUARDIAN_INTEGRITY_BLOCK=1 时，连续 BLOCK_THRESHOLD 次 critical 违规进入受限态；
#   进入受限态后，连续 BLOCK_RECOVER 次干净检查自动恢复（防误杀 + 自愈）。
INTEGRITY_BLOCK_ON_CRITICAL = os.getenv('GUARDIAN_INTEGRITY_BLOCK', '1') == '1'
INTEGRITY_BLOCK_THRESHOLD   = int(os.getenv('GUARDIAN_INTEGRITY_BLOCK_THRESHOLD', '2'))
INTEGRITY_BLOCK_RECOVER     = int(os.getenv('GUARDIAN_INTEGRITY_BLOCK_RECOVER', '3'))

# ── 心跳上报 ───────────────────────────────────
# 注意：心跳上报依赖官方端 VeroGuard 服务（api.verorun.cn / api.verorun.com）。
# 本地/LAN 部署（无官方端）时心跳不可用；完整性校验清单 manifest.json/.sig
# 由发版流程生成并随代码分发，服务器端禁止本地重生成（无 RELEASE_SIGN_KEY）。
HEARTBEAT_INTERVAL = int(os.getenv('GUARDIAN_HEARTBEAT_INTERVAL', '300'))
PROBE_SECRET       = os.getenv('PROBE_SECRET', '')
DEPLOYMENT_CODE    = os.getenv('DEPLOYMENT_CODE', '')

# ── 远程命令安全（VR-SEC V4）────────────────────
# VG_OPS_CONFIRM: 破坏性命令（shutdown/self_destruct）本地二次确认的运维密钥，
# 与 PROBE_SECRET 分离；未配置时破坏性命令一律拒绝（fail-closed）。
VG_OPS_CONFIRM    = os.getenv('VG_OPS_CONFIRM', '')
COMMAND_AUDIT_LOG = os.getenv('GUARDIAN_COMMAND_AUDIT_LOG',
                              '/var/log/verorun-command-audit.log')

# ── 发布签名公钥（Ed25519，与 deploy/scripts/sign_release.py 同款）──
# 完整性清单 manifest.json/.sig 的验签公钥；私钥 RELEASE_SIGN_KEY 仅存 CI/发版机，永不入库。
RELEASE_VERIFY_KEY = "a467ea79346e26f8c4fb75ecc07b400b3af86f7c9a7aab9878bb2754b8107ef4"


def _get_remote_url() -> str:
    """获取 VeroGuard 远程端点（区域感知）。
    环境变量 GUARDIAN_REMOTE_URL 覆盖优先（向后兼容）。
    GUARDIAN_REMOTE_URL=off|disabled|none → 返回空串，心跳上报被跳过（企业完全离线场景）。
    容错：Nuitka 编译时 plugin_manager 不可导入，回退直接读环境变量。
    """
    override = os.getenv('GUARDIAN_REMOTE_URL', '').strip().lower()
    if override in ('off', 'disabled', 'none'):
        # 完全离线：REMOTE_URL 置空后 communicator.send_heartbeat 检测空值即跳过上报
        return ''
    if override:
        return override
    try:
        import sys
        import os as _os
        _verorun_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _verorun_root not in sys.path:
            sys.path.insert(0, _verorun_root)
        from plugin_manager.region import get_veroguard_url
        return get_veroguard_url()
    except ImportError:
        region = os.getenv('APP_REGION', 'global')
        return 'https://api.verorun.cn' if region == 'cn' else 'https://api.verorun.com'

REMOTE_URL = _get_remote_url()

# ── 状态文件路径 ───────────────────────────────
STATUS_FILE = os.getenv('GUARDIAN_STATUS_FILE',
                '/var/run/verorun-guardian/status.json')
