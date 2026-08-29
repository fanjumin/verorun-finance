"""VeroRun AI 命令行客户端。

薄客户端：仅通过 HTTP 调用 VeroRun 服务端已有的 REST 端点，
不 import 任何核心模块、不连数据库、不注册插件、不新建服务。
"""

from .constants import VERSION as __version__

__all__ = ["__version__"]
