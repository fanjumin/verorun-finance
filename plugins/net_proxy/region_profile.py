"""net_proxy — 地域画像封装。

**禁止另造地域探测实现**（插件标准 §18.3 / 方案 §7.7）：
本模块只是一层薄封装，把 `plugin_manager.distribution.current_profile()`
的能力以插件内稳定的接口暴露给 rules.py / channels.py 使用。

`current_profile()` 的既有语义（唯一事实源）：
    优先 `VR_PROFILE` → 未声明时复用 `plugin_manager/region.py::is_cn_region()`
    → 两者皆不可得返回 `''`（= 不限，环境维度不参与过滤）。

本模块额外只做两件事（都属插件自己的语义，不动内核实现）：
  1. 把 `''` 归一化为 `'any'`（通道侧 region 缺省值就是 `any`，语义等价）；
  2. 提供 `match_profile()` 做「规则/通道的画像标签是否命中当前环境」判定。
"""

from plugin_manager.distribution import current_profile as _core_profile

import json

__all__ = [
    'ANY',
    'VALID_CHANNEL_REGIONS',
    'current_profile',
    'is_any',
    'match_profile',
    'matches_any_profile',
]

# 通道 region 的缺省/不限值
ANY = 'any'

# 通道允许的 region 取值（§7.3 保存校验用）
VALID_CHANNEL_REGIONS = ('cn', 'os', ANY)


def current_profile() -> str:
    """当前环境画像标签；不可得时返回 `'any'`（不限）。

    Returns:
        str: `'cn'` / `'os'` 等具体画像，或 `'any'` 表示不限。
    """
    try:
        p = _core_profile()
    except Exception:
        return ANY
    return p or ANY


def is_any(value: str) -> bool:
    """该标签是否表示「不限」。"""
    return (value or '').strip().lower() in ('', ANY, '*')


def match_profile(tag: str) -> bool:
    """单个标签是否命中当前环境。

    空 / `any` / `*` 一律视为不限（命中）。
    """
    if is_any(tag):
        return True
    return (tag or '').strip().lower() == current_profile()


def _split_tags(tags):
    """把标签入参归一化为 list[str]。

    兼容三种既有存法：
      * JSON 文本（DB 列存法，如 `'["llm"]'`）—— channels.py::dump_tags 的产出；
      * 逗号 / 空白分隔（人工在配置里手写）；
      * 已解析的序列。

    兼容 JSON 是必须的：`proxy_rules.profile_tags` / `proxy_channels.profile_tags`
    存的就是 JSON 文本，若按逗号切分会把 `'["cn"]'` 当成一个整体标签，
    导致任何配了 profile_tags 的规则**静默不命中**（P3 自测实测到该缺陷）。
    """
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = tags.strip()
        if not raw:
            return []
        if raw.startswith('['):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (list, tuple)):
                return [str(t).strip() for t in parsed if str(t).strip()]
        return [t for t in raw.replace(',', ' ').split() if t]
    if not isinstance(tags, (list, tuple, set)):
        return [str(tags).strip()] if str(tags).strip() else []
    return [str(t).strip() for t in tags if str(t).strip()]


def matches_any_profile(tags) -> bool:
    """列表式判定：**空列表 = 不限（命中）**；非空则任一命中即可。

    Args:
        tags: JSON 文本 / 逗号或空格分隔字符串 / list / tuple / None
    """
    parts = _split_tags(tags)
    if not parts:
        return True
    cur = current_profile()
    for t in parts:
        tl = t.strip().lower()
        if tl in ('', ANY, '*') or tl == cur:
            return True
    return False
