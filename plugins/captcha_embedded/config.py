#!/usr/bin/env python3
"""Configuration — captcha embedded plugin（2026-08-06 从 captcha-service/config.py 迁入）"""
import os

# Puzzle 静态资源目录：图片资产自包含于插件 images/ 目录（2026-08-06 修复 C9），
# 可通过环境变量 IMAGE_DIR 覆盖（如需要指向共享图片库时）。
IMAGE_DIR = os.getenv(
    "IMAGE_DIR",
    os.path.normpath(os.path.join(os.path.dirname(__file__), 'images'))
)

# Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CAPTCHA_TTL = 120          # challenge expires in 2 min
RATE_LIMIT_TTL = 300       # IP window 5 min
MAX_FAILS = 5              # max fails per IP in window
BLOCK_TTL = 3600           # IP blocklist auto-release after 1 hour (2026-08-29)

# Puzzle
TOLERANCE = 4              # ±px
PIECE_MIN_X = 40           # min piece position
TRACK_WIDTH = 340          # canvas width (must match frontend)
CANVAS_HEIGHT = 190        # canvas height

# Security（延迟校验：模块导入不因缺少环境变量而崩溃，见 get_secret_key()）
SECRET_KEY = os.getenv("CAPTCHA_SECRET_KEY")
HASH_ALGO = "sha256"


def get_secret_key() -> str:
    """延迟获取密钥 — 仅在生成/校验 token 时校验（2026-08-06 修复 C8）。

    避免 CI/CD 语法检查、单元测试、PluginManager 扫描插件目录、
    开发环境首次启动时因缺少 CAPTCHA_SECRET_KEY 而导入崩溃。
    允许在导入后再设置环境变量（首次调用时重新读取）。
    """
    global SECRET_KEY
    if not SECRET_KEY:
        SECRET_KEY = os.getenv("CAPTCHA_SECRET_KEY")
    if not SECRET_KEY:
        raise RuntimeError(
            "CAPTCHA_SECRET_KEY environment variable is required. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return SECRET_KEY

# Risk scoring
RISK_THRESHOLD = 0.7       # score >= → pass
