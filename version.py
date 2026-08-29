"""Version management — single source of truth for the version string."""
import os
import subprocess

_VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION')


def get_version() -> str:
    """Read the current version number from the VERSION file."""
    try:
        with open(_VERSION_FILE, 'r') as f:
            return f.read().strip()
    except:
        return '0.0.0'


def get_edition() -> str:
    """返回发行版标识（统一走 agent_matrix.current_edition() 归一化：
    edu→research / pro→finance，未设置视为 standard；agent_matrix 不可用时兜底 VR_EDITION）。"""
    try:
        from agent_matrix.models import current_edition
        return current_edition()
    except Exception:
        return (os.environ.get('VR_EDITION', '').strip().lower() or 'standard')


def get_build_id() -> str:
    """返回构建标识 VR-{semver}-{commit8}；无 git 环境时回退 VR_BUILD_ID 或 UNKNOWN。"""
    commit = ''
    try:
        _root = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
            timeout=10, cwd=_root,
        )
        commit = out.stdout.strip()
    except Exception:
        commit = ''
    if not commit:
        commit = os.environ.get('VR_BUILD_ID', '')
    return f'VR-{__version__}-{(commit[:8].upper() if commit else "UNKNOWN")}'


__version__ = get_version()
