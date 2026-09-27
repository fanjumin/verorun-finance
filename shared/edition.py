#!/usr/bin/env python3
"""发行版（edition）标识归一化 —— 全站唯一事实源。

本模块**只允许依赖标准库**：不得 import DB / i18n / Web 框架。
原因：license、部署脚本、CLI、guardian 等进程需要在没有数据库上下文的
情况下判定发行版；一旦依赖重模块，判定会因 import 失败而 fail-closed 误判。
（2026-09-18：该逻辑原内联在 agent_matrix/models.py，而该模块 import 时会执行
DB 迁移，导致无 DB 环境下 is_enterprise_edition() 抛错并返回 False。）

agent_matrix/models.py 再导出本模块的全部公开符号，既有调用方无需改动。
**禁止**在其它位置重复定义归一化逻辑（第二处定义会导致口径分裂）。
"""
import os

# ── 发行版 edition 归一化（单一事实源：VR_EDITION / RELEASE_EDITION / DEPLOY_TYPE）──
# 现行发行版 ID 共 7 个（定稿后不再变更；显示名走 i18n，与 ID 解耦）：
#   enterprise 企业版 / standard 标准版 / pro 专业版 / finance 金融版 /
#   research 科研版 / minipro 小程序版 / edge 边缘版
# 注意：pro（专业版）与 finance（金融版）是两个**独立** ID，不可再合并。
_EDITION_IDS = ('enterprise', 'standard', 'pro', 'finance', 'research', 'minipro', 'edge')

# 历史旧名 / 别名 → 现行 ID（大小写不敏感）
# 'official' 为 enterprise 的历史别名，过渡期保留；显式写入 enterprise 同样有效。
_EDITION_ALIASES = {
    'official': 'enterprise',
    'enterprise-web': 'enterprise',
    'standard-web': 'standard',
    'pro-web': 'pro',
    'finance-desktop': 'finance',
    'research-desktop': 'research',
    'edu': 'research',
    'edu-desktop': 'research',
    'mini': 'minipro',
    'vr_test_edge': 'edge',
    'edge-computing': 'edge',
}


def normalize_edition(e) -> str:
    """归一化发行版标识（大小写不敏感）：旧名/别名→现行 ID，空→standard。"""
    e = (e or '').strip().lower()
    return _EDITION_ALIASES.get(e, e) or 'standard'


def current_edition() -> str:
    """全系统发行版唯一判定：VR_EDITION → RELEASE_EDITION → DEPLOY_TYPE。"""
    return normalize_edition(os.getenv('VR_EDITION') or os.getenv('RELEASE_EDITION')
                             or os.getenv('DEPLOY_TYPE') or '')


# ── canonical ID → 磁盘产物名（**唯一**桥接点）──
# canonical ID（_EDITION_IDS，7 个）与磁盘产物命名历史上分叉：
#   deploy/editions/<stem>.yaml、agent_matrix/roles/<stem>/
# 磁盘侧仍沿用旧名 / 连字符名（official / finance-desktop / research-desktop）。
# 二者必须显式桥接，**禁止**在各消费点自行字符串拼接：`f'{current_edition()}.yaml'`
# 这类写法在 ID 收敛为 finance/research 后必然落空（E-1：金融桌面版「订阅到期即锁」
# 与 include 白名单同时静默失效）。新增或改名产物只改这张表。
_EDITION_ARTIFACT_STEMS = {
    'enterprise': 'official',
    'standard': 'standard',
    'finance': 'finance-desktop',
    'research': 'research-desktop',
    # pro（专业版）是金融桌面版的**旧商用名**，与 finance 共用同一套磁盘产物。
    # 真实代码依据（3 处，均早于本次修复）：
    #   deploy/entrypoint.sh:83 case 分支把 finance|research|pro|finance-desktop|research-desktop 归一处理
    #   agent_matrix/seed_prompts.py:88 「('pro','finance','finance-desktop') → finance-desktop」
    #   .github/workflows/sync-to-pro.yml:99 产物写 .verorun-edition=finance-desktop
    # 另有 2 处只是**注释声称**（非实现，不得再充作依据）：version.py:19 docstring、
    #   store.py:42 注释 —— 其中后者写的「pro→finance」与 store.py:285-287 的实现矛盾
    #   （_EDITION_ALIASES 里没有 pro，normalize_edition('pro') == 'pro'）。
    # 口径边界（第五轮 N5-5 澄清）：pro 在 **canonical ID 维度**是与 finance 并列的
    # 独立 ID（见 _EDITION_IDS），插件 compatible_editions 按该口径过滤；
    # 本表只决定它的**磁盘产物解析**，不合并 ID。
    'pro': 'finance-desktop',
}


def edition_artifact_stem(edition: str = None) -> str:
    """canonical ID → 磁盘产物名主干；未登记的一律同名直传。"""
    e = edition or current_edition()
    return _EDITION_ARTIFACT_STEMS.get(e, e)
