#!/usr/bin/env python3
"""
Plugin Manager — Plugin Downloader
===================================
Downloads plugin packages from remote store, verifies SHA256 integrity,
extracts archives safely (Zip Slip protection), and cleans up temp files.

Supports: .zip, .tar.gz, .tgz
"""

import os
import time
import hashlib
import shutil
import tarfile
import zipfile
import tempfile
import logging
import ipaddress
import socket
from http.client import RemoteDisconnected
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Max download size: 200 MB
MAX_DOWNLOAD_SIZE = 200 * 1024 * 1024
# 下载总预算（墙钟秒，主源 + 回退源合计，可用 PLUGIN_DOWNLOAD_TIMEOUT 覆盖）。
# 必须小于上层网关/worker 超时：否则面板会在后端仍在下载时先行超时，
# 表现为"下载间歇失败"（504）——PB-2；同时硬约束整体耗时（旧实现仅约束
# "每次尝试开始"，慢速滴流可让总耗时无界，且回退源会再获得一份完整预算）。
DOWNLOAD_TIMEOUT = int(os.environ.get('PLUGIN_DOWNLOAD_TIMEOUT', '120'))
# 单次 socket 操作超时上限（连接 + 每次读）：对端挂死时快速失败
DOWNLOAD_SOCKET_TIMEOUT = 30
# 单源下载尝试次数（含首次）：连接层异常做有限重试，整体仍受 DOWNLOAD_TIMEOUT 预算约束
DOWNLOAD_RETRIES = 2
DOWNLOAD_RETRY_DELAY = 1.0


def _validate_public_url(url: str) -> None:
    """SSRF 防护（§11.3）：仅允许 http/https，且目标主机必须解析为公网地址。

    拒绝：私网（10/8、172.16/12、192.168/16）、回环（127/8、::1）、
    链路本地（169.254/16、fe80::/10）、保留地址与组播地址。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f'仅允许 http/https 协议，当前: {parsed.scheme!r}')
    host = parsed.hostname
    if not host:
        raise ValueError(f'无效 URL（缺少主机名）: {url}')
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (socket.gaierror, ValueError) as e:
        raise ValueError(f'无法解析插件下载主机: {host}') from e
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast):
        raise ValueError(f'拒绝下载: 目标为内网/保留地址 {host} ({ip})')


def download_plugin(download_url: str, dest_dir: str,
                    expected_hash: str = '',
                    fallback_url: str = '') -> str:
    """Download a plugin archive and extract it to dest_dir.

    Args:
        download_url: URL of the plugin archive (primary source)
        dest_dir: Absolute path to plugins/<identifier>/
        expected_hash: Optional SHA256 hex digest for integrity check
        fallback_url: Optional fallback URL tried on network failure
            (e.g. mirror source down -> original GitHub source)

    Returns:
        Absolute path to the extracted plugin directory

    Raises:
        ValueError: Hash mismatch, Zip Slip detected, or invalid archive
        URLError: Network error (also raised from fallback if retried)
        HTTPError: HTTP error from remote
    """
    # ── Download（主源下载；网络异常时自动回退备用源）────────
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix='.plugin')
        os.close(fd)

        def _fetch(src_url: str, deadline: float) -> None:
            """单次下载（网络异常向上抛出，由外层决定是否重试/回退）。

            deadline 为整段下载（含回退源）共享的绝对墙钟上限：
            读循环每轮校验，超预算立即以 URLError 失败，避免无界挂起。
            """
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise URLError(f'download budget exhausted '
                               f'({DOWNLOAD_TIMEOUT}s): {src_url}')
            logger.info(f'Downloading {src_url} -> {tmp_path}')

            # ── SSRF 防护：仅允许公网地址 ──────────────────────
            _validate_public_url(src_url)

            req = Request(src_url, headers={
                'User-Agent': 'VeroRun-PluginManager/1.0',
            })

            # 单次 socket 操作超时取「剩余预算」与 DOWNLOAD_SOCKET_TIMEOUT 的较小值
            sock_timeout = max(1, int(min(remaining, DOWNLOAD_SOCKET_TIMEOUT)))
            with urlopen(req, timeout=sock_timeout) as resp:
                content_length = resp.headers.get('Content-Length')
                if content_length and int(content_length) > MAX_DOWNLOAD_SIZE:
                    raise ValueError(f'Plugin too large: {content_length} bytes (max {MAX_DOWNLOAD_SIZE})')

                downloaded = 0
                with open(tmp_path, 'wb') as f:
                    while True:
                        if time.monotonic() > deadline:
                            raise URLError(
                                f'download exceeded time budget ({DOWNLOAD_TIMEOUT}s, '
                                f'{downloaded} bytes received): {src_url}')
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > MAX_DOWNLOAD_SIZE:
                            raise ValueError(f'Download exceeded max size {MAX_DOWNLOAD_SIZE}')
                        f.write(chunk)

            logger.info(f'Downloaded {downloaded} bytes')

        def _try_fetch(src_url: str, deadline: float) -> None:
            """单源下载 + 连接层有限重试（与回退源共享同一 wall-clock 预算，不放大上限）。

            重试覆盖 http.client.RemoteDisconnected（对端提前断连，非 URLError 子类）
            与超时等传输层异常；HTTP 状态错误（4xx/5xx）不重试，交由外层回退备用源。
            """
            last_exc = None
            for attempt in range(1, DOWNLOAD_RETRIES + 1):
                if time.monotonic() >= deadline:
                    break
                try:
                    _fetch(src_url, deadline)
                    return
                except HTTPError:
                    raise
                except (URLError, RemoteDisconnected, socket.timeout, OSError) as e:
                    last_exc = e
                    logger.warning(f'Download attempt {attempt}/{DOWNLOAD_RETRIES} '
                                   f'failed for {src_url}: {e}')
                    if attempt < DOWNLOAD_RETRIES:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            break
                        time.sleep(min(DOWNLOAD_RETRY_DELAY * attempt, left))
            if last_exc is None:
                raise URLError(f'download timed out (budget {DOWNLOAD_TIMEOUT}s): {src_url}')
            raise last_exc

        # 主源 + 回退源共享同一份预算（旧实现两份独立预算，最坏耗时翻倍）
        deadline = time.monotonic() + DOWNLOAD_TIMEOUT
        try:
            _try_fetch(download_url, deadline)
        except (URLError, HTTPError, socket.timeout, OSError) as e:
            if (fallback_url and fallback_url != download_url
                    and time.monotonic() < deadline):
                logger.warning(f'Primary download failed ({e}); '
                               f'falling back to {fallback_url}')
                _try_fetch(fallback_url, deadline)
            else:
                raise

        # ── SHA256 verification ───────────────────────────────
        if expected_hash:
            sha256 = hashlib.sha256()
            with open(tmp_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    sha256.update(chunk)
            actual_hash = sha256.hexdigest()
            if actual_hash != expected_hash:
                raise ValueError(
                    f'SHA256 mismatch: expected {expected_hash[:16]}..., '
                    f'got {actual_hash[:16]}...'
                )
            logger.info(f'SHA256 verified: {expected_hash[:16]}...')

        # ── Extract ────────────────────────────────────────────
        os.makedirs(dest_dir, exist_ok=True)
        _extract_archive(tmp_path, dest_dir)

        logger.info(f'Extracted to {dest_dir}')
        return dest_dir

    finally:
        # ── Cleanup ────────────────────────────────────────────
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _extract_archive(archive_path: str, dest_dir: str):
    """Extract archive to dest_dir with Zip Slip protection.

    Ensures all extracted files stay within dest_dir.
    """
    dest_dir = os.path.realpath(dest_dir)

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, 'r') as zf:
            for member in zf.infolist():
                member_path = os.path.realpath(os.path.join(dest_dir, member.filename))
                if not member_path.startswith(dest_dir + os.sep) and member_path != dest_dir:
                    raise ValueError(f'Zip Slip detected: {member.filename}')
                zf.extract(member, dest_dir)
    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, 'r:*') as tf:
            for member in tf.getmembers():
                member_path = os.path.realpath(os.path.join(dest_dir, member.name))
                if not member_path.startswith(dest_dir + os.sep) and member_path != dest_dir:
                    raise ValueError(f'Tar Slip detected: {member.name}')
                tf.extract(member, dest_dir)
    else:
        raise ValueError(f'Unsupported archive format: {archive_path}')