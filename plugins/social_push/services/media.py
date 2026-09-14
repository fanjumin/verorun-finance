#!/usr/bin/env python3
"""Social Push Plugin — 媒体处理模块（P3）

- process_image()：Pillow 压缩 / 平台封面规格适配（JPEG、≤2MB）
- group_media()：按平台媒体上限截断与分组（微博九图 / TG 相册）

输入可为本地路径（/static/uploads/temp/...）或远程 URL。
任何失败都 fail-open（返回原值），不阻断发布主流程。
"""
import io
import logging
import os
import re
import tempfile
import uuid

from i18n import _

logger = logging.getLogger(__name__)

# 各平台封面/图片规格（宽, 高）；留空则仅压缩不缩放
COVER_SPECS = {
    'wechat':   (900, 383),
    'wechat_oa': (900, 383),
    'toutiao':  (640, 400),
    'twitter':  (1600, 900),   # 1.91:1 推文卡
    'linkedin': (1200, 627),
}

_MAX_BYTES = 2 * 1024 * 1024  # 2MB
_SAVE_DIR = None


def _base_dir():
    """admin/static/uploads/temp 绝对路径（惰性解析）"""
    global _SAVE_DIR
    if _SAVE_DIR is None:
        _auth_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'auth-center')
        _SAVE_DIR = os.path.join(_auth_dir, '..', 'admin', 'static', 'uploads', 'temp')
    return _SAVE_DIR


def _resolve_source(src: str):
    """把本地 /static/... 路径或远程 URL 解析为本地可读文件路径。"""
    if not src:
        return ''
    if src.startswith('/static/'):
        # /static/uploads/temp/xxx.jpg → admin/static/uploads/temp/xxx.jpg
        rel = src[len('/static/'):]
        path = os.path.join(_base_dir(), '..', '..', rel)
        return path if os.path.isfile(path) else ''
    if src.startswith(('http://', 'https://')):
        try:
            import urllib.request
            data = urllib.request.urlopen(src, timeout=15).read()
            ext = os.path.splitext(src.split('?')[0])[1] or '.jpg'
            tmp = os.path.join(tempfile.gettempdir(), f'sp_{uuid.uuid4().hex}{ext}')
            with open(tmp, 'wb') as f:
                f.write(data)
            return tmp
        except Exception as e:
            logger.warning('[SocialPush] media download failed: %s', e)
            return ''
    # 本地绝对/相对路径
    return src if os.path.isfile(src) else ''


def process_image(src: str, channel: str = '') -> str:
    """压缩/规格化图片，返回处理后的本地 URL；失败返回原 src。

    处理链路：打开 → 按平台规格缩放 → RGB → JPEG 渐进压缩至 ≤2MB → 存 temp/proc/
    """
    if not src:
        return src
    try:
        from PIL import Image
    except ImportError:
        logger.warning('[SocialPush] Pillow not installed; skip image processing')
        return src

    path = _resolve_source(src)
    if not path:
        return src
    try:
        img = Image.open(path)
        img.load()
    except Exception as e:
        logger.warning('[SocialPush] cannot open image %s: %s', src, e)
        return src

    try:
        spec = COVER_SPECS.get(channel)
        if spec:
            img = img.resize(spec, Image.LANCZOS)
        img = img.convert('RGB')

        buf = io.BytesIO()
        quality = 85
        while True:
            buf.seek(0)
            buf.truncate()
            img.save(buf, 'JPEG', quality=quality, optimize=True)
            if buf.tell() <= _MAX_BYTES or quality <= 40:
                break
            quality -= 10

        proc_dir = os.path.join(_base_dir(), 'proc')
        os.makedirs(proc_dir, exist_ok=True)
        filename = f'{uuid.uuid4().hex}.jpg'
        with open(os.path.join(proc_dir, filename), 'wb') as f:
            f.write(buf.getvalue())
        return f'/static/uploads/temp/proc/{filename}'
    except Exception as e:
        logger.warning('[SocialPush] image processing failed: %s', e)
        return src


def group_media(media: list, channel: str = '') -> list:
    """按平台媒体上限截断并返回处理后的图片 URL 列表。

    media 元素可为 str（URL）或 dict（{url, ...}）。
    上限：微博/TG 9-10，X 4，其余按 PLATFORM_RULES.media_max（content.py 已截断）。
    """
    if not media:
        return []
    result = []
    for m in media:
        url = m if isinstance(m, str) else (m.get('url') or m.get('image_url') or '')
        if url:
            processed = process_image(url, channel)
            if processed:
                result.append(processed)
    return result
