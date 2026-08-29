"""兼容层：使 `pip install -e .` 生成 `verorun` 控制台入口，并可 `python -m verorun_cli`。

版本号复用系统主版本号（仓库根 VERSION 文件），不写死，保持与系统发版一致。
"""

import os

from setuptools import setup, find_packages

_PACKAGED_FALLBACK = "0.59.6"


def _read_repo_version() -> str:
    """从 sdks/cli 向上两级定位仓库根 VERSION（只读，不 import 核心）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root_version = os.path.normpath(os.path.join(here, "..", "..", "VERSION"))
    try:
        with open(root_version, "r", encoding="utf-8") as fh:
            candidate = fh.read().strip()
            if candidate:
                return candidate
    except OSError:
        pass
    return _PACKAGED_FALLBACK


setup(
    name="verorun-cli",
    version=_read_repo_version(),
    packages=find_packages(include=["verorun_cli", "verorun_cli.*"]),
    entry_points={
        "console_scripts": [
            "verorun = verorun_cli.cli:main",
        ],
    },
)
