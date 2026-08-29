#!/usr/bin/env python3
"""
VeroGuard — 环境指纹采集模块（Phase 3）
=============================================
采集服务器硬件/网络指纹，生成唯一 machine_id。
用于心跳上报时标识客户实例。
"""
import hashlib
import platform
import socket
import uuid


def get_primary_mac() -> str:
    """获取主网卡 MAC 地址"""
    try:
        # VR-SEC (V5): 原 `mac != uuid.getnode()` 二次调用恒相等，主路径永不生效；
        # 改为校验有效 MAC（非 0、非全 1），与 license.py `_get_mac_address` 一致
        mac = uuid.getnode()
        if mac and mac != 0xFFFFFFFFFFFF:
            return f'{mac:012x}'
    except Exception:
        pass
    # 回退：通过 socket 获取
    try:
        import fcntl
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        info = fcntl.ioctl(s.fileno(), 0x8927,
                           struct.pack('256s', b'eth0'))
        return ':'.join(f'{b:02x}' for b in info[18:24])
    except Exception:
        return 'unknown'


def get_cpu_serial() -> str:
    """获取 CPU 序列号（Linux /proc/cpuinfo）"""
    try:
        with open('/proc/cpuinfo', 'r') as f:
            for line in f:
                if line.startswith('Serial'):
                    return line.split(':')[1].strip()
    except Exception:
        pass
    return 'unknown'


def get_root_disk_serial() -> str:
    """获取根磁盘序列号（通过 udevadm）"""
    try:
        import subprocess
        result = subprocess.run(
            ['udevadm', 'info', '--query=property',
             '--name=/dev/sda'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if 'ID_SERIAL_SHORT=' in line:
                return line.split('=')[1].strip()
    except Exception:
        pass
    return 'unknown'


def get_public_ip() -> str:
    """获取公网 IP"""
    try:
        import urllib.request
        resp = urllib.request.urlopen('https://api.ipify.org', timeout=5)
        return resp.read().decode().strip()
    except Exception:
        pass
    # 回退：本机局域网 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'unknown'


def get_docker_container_id() -> str:
    """获取 Docker 容器 ID（如在容器内运行）"""
    try:
        with open('/proc/self/cgroup', 'r') as f:
            for line in f:
                if 'docker' in line:
                    return line.strip().split('/')[-1][:12]
    except Exception:
        pass
    return ''


def generate_machine_id() -> str:
    """生成唯一机器标识 SHA256(MAC + CPU + Disk)[:32]"""
    raw = f"{get_primary_mac()}-{get_cpu_serial()}-{get_root_disk_serial()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def collect() -> dict:
    """采集完整环境指纹"""
    return {
        "machine_id": generate_machine_id(),
        "hostname": socket.gethostname(),
        "mac_address": get_primary_mac(),
        "cpu_serial": get_cpu_serial(),
        "disk_serial": get_root_disk_serial(),
        "ip_address": get_public_ip(),
        "os_version": platform.platform(),
        "docker_id": get_docker_container_id(),
    }
