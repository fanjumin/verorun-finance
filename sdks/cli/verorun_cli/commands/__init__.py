"""命令包：集中注册所有子命令到根 group。"""

from . import auth_cmd, automation_cmd, chat_cmd, config_cmd, agents_cmd
from . import plugin_cmd, session_cmd, status_cmd, task_cmd

# 注册顺序即 help 中展示顺序
_MODULES = [
    auth_cmd,
    chat_cmd,
    agents_cmd,
    session_cmd,
    task_cmd,
    automation_cmd,
    plugin_cmd,
    status_cmd,
    config_cmd,
]


def register_all(group):
    for mod in _MODULES:
        mod.register(group)
