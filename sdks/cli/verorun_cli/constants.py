"""CLI 常量。仅本机客户端相关，与服务器 .env / 核心配置完全无关。"""

from __future__ import annotations

import os

APP_NAME = "verorun"

# 版本号策略：CLI 是 VeroRun 系统的配套客户端，**复用系统主版本号**（仓库根
# `VERSION` 文件，由 version.py 统一管理，当前 0.59.5），避免用户看到系统
# 0.59.5 却看到 CLI 独立的 0.2.0 而产生"割裂/过期"误解。
#
# 读取方式：从本包位置向上两级定位仓库根 `VERSION`（sdks/cli -> sdks -> 根）。
# 这是**只读**文件访问，不 import 任何核心模块、不引入核心依赖，满足"零入侵"约束。
# 若 CLI 被单独打包发布（脱离仓库树），则回退到下方 PACKAGED_VERSION 常量
# （由构建流程在 sdist/wheel 阶段注入，保持与系统发版一致）。
_PACKAGED_VERSION = "0.59.6"


def _resolve_version() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    # sdks/cli/verorun_cli/constants.py -> 上两级为仓库根
    root_version = os.path.normpath(os.path.join(here, "..", "..", "..", "VERSION"))
    try:
        with open(root_version, "r", encoding="utf-8") as fh:
            candidate = fh.read().strip()
            if candidate:
                return candidate
    except OSError:
        pass
    return _PACKAGED_VERSION


VERSION = _resolve_version()

# CLI 本机配置目录（位于用户家目录，不写入仓库）
CONFIG_DIR = os.path.expanduser("~/.verorun")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.yaml")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token.json")
CRED_FILE = os.path.join(CONFIG_DIR, "credentials.json")

# 默认配置
DEFAULT_CONFIG = {
    "server": {
        "base_url": "",        # 空 -> 取 VERORUN_BASE_URL，否则 http://localhost:8084
        "verify_ssl": True,
    },
    "auth": {
        "method": "password",  # password | sms | apikey
        "username": "",
    },
    "agent": {
        "default_agent_id": None,
    },
    "output": {
        "format": "rich",      # rich | json
        "color": True,
    },
}

# agent 级 API Key 前缀（POST /agent/<id>/keys/create 返回 ek-...）
AGENT_APIKEY_PREFIX = "ek-"

# API Key 最短长度（前缀 + 随机串）；仅用于本地格式自检，不代表服务端规则
AGENT_APIKEY_MIN_LEN = 16

# 默认请求超时（秒）；同步 /chat 走 Master 编排，可能较慢
DEFAULT_TIMEOUT = 300

# JWT exp 判定容差（秒），避免本机与服务端时钟微小偏差导致误判过期
JWT_LEEWAY = 30

USER_AGENT = f"{APP_NAME}-cli/{VERSION}"
