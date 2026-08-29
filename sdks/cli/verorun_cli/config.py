import os

import yaml

from .constants import CONFIG_DIR, CONFIG_FILE, DEFAULT_CONFIG
from .output import warn


class Config:
    """CLI 本机配置（~/.verorun/config.yaml）。与服务器 .env 无关。"""

    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self._data = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return _deep_copy(DEFAULT_CONFIG)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            merged = _deep_copy(DEFAULT_CONFIG)
            _merge(merged, data)
            return merged
        except Exception as exc:
            # 配置损坏不应让 CLI 崩溃，但需明确告知用户而非静默吞掉，
            # 否则用户会误以为自己改的配置生效了。
            warn(f"配置文件 {self.path} 解析失败，已回退默认配置：{exc}")
            return _deep_copy(DEFAULT_CONFIG)

    # dotted key 访问，如 "server.base_url"
    def get(self, dotted, default=None):
        cur = self._data
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def set(self, dotted, value):
        keys = dotted.split(".")
        cur = self._data
        for k in keys[:-1]:
            nxt = cur.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[k] = nxt
            cur = nxt
        cur[keys[-1]] = value
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)

    def as_dict(self):
        return self._data


def _deep_copy(d):
    import copy
    return copy.deepcopy(d)


def _merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
