"""pytest 引导：把 sdks/cli 加入 sys.path 以便 import verorun_cli。

CI 的 tests.yml 并不会运行 pytest（只做 i18n 与签名检查），
因此本目录完全离线、可独立运行，不影响现有流水线。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
