"""
Avatar Service — 服务端 SVG 头像生成器
=======================================
用于没有自定义头像时，根据用户名/ID 生成风格统一的首字母头像。

用法:
    avatar_url = get_avatar_url({'nickname': '张三', 'avatar_url': None})
    # -> '/avatar/gen/张三' (若无自定义头像)

    浏览器直接访问 /avatar/gen/<seed> 返回 SVG 图片
"""

import hashlib

# 12 种渐变色板 — 与 DiceBear initials 风格协调
_COLORS = [
    ('#00f5ff', '#0099ff'),  # 电光蓝
    ('#a020f0', '#7b2ff7'),  # 紫罗兰
    ('#00ff9f', '#00cc7a'),  # 霓虹绿
    ('#ff6b6b', '#ee5a24'),  # 珊瑚红
    ('#ffd93d', '#f9a825'),  # 琥珀
    ('#6bcbff', '#3d7bf7'),  # 天蓝
    ('#ff9ff3', '#f368e0'),  # 粉红
    ('#54a0ff', '#2e86de'),  # 蓝色
    ('#5f27cd', '#341f97'),  # 深紫
    ('#01a3a4', '#006266'),  # 青绿
    ('#f0932b', '#d68910'),  # 橘色
    ('#b71540', '#6b0020'),  # 酒红
]


def _pick_color(seed: str):
    """根据 seed 确定性地选色板"""
    h = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
    return _COLORS[h % len(_COLORS)]


def generate_initials_svg(seed: str, size: int = 72) -> str:
    """
    生成首字母头像 SVG
    
    Args:
        seed: 种子字符串（昵称/手机/ID），用于确定颜色和首字母
        size: 图片尺寸（宽高相同，单位 px）
    
    Returns:
        SVG 字符串
    """
    seed = seed or '?'
    initial = seed.strip()[0].upper()
    c1, c2 = _pick_color(seed)
    font_size = int(size * 0.45)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:{c1}"/>
      <stop offset="100%" style="stop-color:{c2}"/>
    </linearGradient>
  </defs>
  <circle cx="{size/2}" cy="{size/2}" r="{size/2}" fill="url(#bg)"/>
  <text x="50%" y="50%" text-anchor="middle" dominant-baseline="central"
        fill="white" font-size="{font_size}" font-weight="700" font-family="system-ui,-apple-system,sans-serif">
    {initial}
  </text>
</svg>'''


def get_avatar_url(user: dict, size: int = 72) -> str:
    """
    获取用户最终头像 URL
    
    优先使用用户自定义头像，若无则返回生成头像地址。
    用户字段: avatar_url, nickname, phone, id, real_name, agent_name
    
    Args:
        user: 用户/Agent/管理员字典
        size: 生成头像尺寸（仅影响生成的头像）
    
    Returns:
        头像 URL 字符串
    """
    # 自定义头像优先
    custom = user.get('avatar_url') or ''
    if custom.strip():
        return custom.strip()

    # 取种子
    seed = (
        user.get('real_name')
        or user.get('display_name')
        or user.get('agent_name')
        or user.get('phone')
        or str(user.get('id', ''))
    )
    return f'/avatar/gen/{seed}'
