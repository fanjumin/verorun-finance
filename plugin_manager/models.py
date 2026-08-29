#!/usr/bin/env python3
"""
Plugin Manager — 数据库模型 & 数据类
=======================================
插件注册表 (plugin_registry) 存放于主库 data/x7k2m9a4.db。

状态图:
    UNKNOWN → INSTALLED ⇄ DISABLED
              INSTALLED → ENABLED → ACTIVE
              ACTIVE → DISABLED
              任何状态(除 UNKNOWN) → UNINSTALLED
"""

import os
import json
import psycopg2
import psycopg2.extras
from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, Dict, Any, List


# ── PG 连接封装 ───────────────────────────────────────────────────────────

class _PgConnection:
    """轻量封装，保持 conn.execute(sql, params) 接口兼容"""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if params is not None:
            cursor.execute(sql.replace('?', '%s'), params)
        else:
            cursor.execute(sql)
        return cursor

    def commit(self):
        self._conn.commit()

    def executescript(self, sql):
        """多语句 DDL 执行（PG 不支持 executescript，用单个 execute 替代）"""
        cursor = self._conn.cursor()
        cursor.execute(sql)
        self._conn.commit()
        cursor.close()

    def close(self):
        self._conn.close()


# ── 数据库连接 ────────────────────────────────────────────────────────────

@contextmanager
def get_registry_db():
    """获取主库 PG 连接（用于 plugin_registry 表）"""
    conn = psycopg2.connect(
        host=os.environ.get('PG_HOST', 'localhost'),
        port=os.environ.get('PG_PORT', '5432'),
        dbname=os.environ.get('PG_DB', 'appdb'),
        user=os.environ.get('PG_USER', 'app'),
        password=os.environ.get('PG_PASSWORD', ''),
    )
    wrapped = _PgConnection(conn)
    try:
        yield wrapped
    finally:
        wrapped.close()


# ── 状态枚举 ──────────────────────────────────────────────────────────────

class PluginStatus(str, Enum):
    """插件生命周期状态"""
    UNKNOWN = 'unknown'             # 磁盘发现但未注册
    INSTALLED = 'installed'         # 首次发现，已写入 registry
    ENABLED = 'enabled'             # 用户启用（但未加载）
    ACTIVE = 'active'               # 已加载模块＋注册路由/钩子
    DISABLED = 'disabled'           # 用户手动禁用
    UNINSTALLED = 'uninstalled'     # 已清理（仅保留记录）
    ERROR = 'error'                 # 异常状态

    @classmethod
    def valid_transitions(cls, current: 'PluginStatus', target: 'PluginStatus') -> bool:
        """验证状态转换是否合法"""
        transitions = {
            cls.UNKNOWN:     {cls.INSTALLED, cls.UNINSTALLED},
            cls.INSTALLED:   {cls.ENABLED, cls.DISABLED, cls.UNINSTALLED},
            cls.ENABLED:     {cls.ACTIVE, cls.DISABLED, cls.UNINSTALLED},
            cls.ACTIVE:      {cls.DISABLED, cls.UNINSTALLED},
            cls.DISABLED:    {cls.ENABLED, cls.UNINSTALLED},
            cls.UNINSTALLED: set(),
            cls.ERROR:       {cls.INSTALLED, cls.ENABLED, cls.DISABLED, cls.UNINSTALLED},
        }
        return target in transitions.get(current, set())

    def can_transition_to(self, target: 'PluginStatus') -> bool:
        return self.valid_transitions(self, target)


# ── 数据类 ────────────────────────────────────────────────────────────────

@dataclass
class PluginInfo:
    """插件元信息（运行时 + 持久化）"""
    identifier: str                             # 唯一标识
    name: str                                   # 显示名称
    version: str = '0.1.0'                      # 版本号
    author: str = ''                            # 作者
    description: str = ''                       # 描述
    min_app_version: str = '1.0.0'              # 最低应用版本
    path: str = ''                              # 插件目录绝对路径

    # 元数据（完整 plugin.json 内容）
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 当前状态
    status: PluginStatus = PluginStatus.UNKNOWN

    # 依赖声明: {identifier: version_spec}
    dependencies: Dict[str, str] = field(default_factory=dict)

    # 钩子声明
    provides_hooks: List[str] = field(default_factory=list)
    listens_hooks: List[str] = field(default_factory=list)

    # 权限声明
    permissions: List[str] = field(default_factory=list)

    # 配置 Schema
    settings_schema: Dict[str, Any] = field(default_factory=dict)

    # 运行时配置（持久化）
    config: Dict[str, Any] = field(default_factory=dict)

    # 配置页 URL（插件自有管理页，无则降级内联）
    admin_url: str = ''
    admin_label: str = ''

    # 时间戳
    installed_at: Optional[str] = None
    updated_at: Optional[str] = None

    # 错误信息
    last_error: str = ''

    # ★ v1.4 插件来源：store(官方商店) / upload(用户上传自研)
    source: str = 'store'

    # ★ v1.5 展示与分类字段（§5.1/§6.1，与 metadata 冗余、列化以便 SQL 筛选）
    category: str = 'other'
    icon: str = 'plugin'
    tags: List[str] = field(default_factory=list)
    dashboard_meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为 dict（用于 API 响应）"""
        result = asdict(self)
        result['status'] = self.status.value if isinstance(self.status, PluginStatus) else self.status
        return result

    def to_json(self) -> str:
        """序列化为 JSON（用于数据库存储）"""
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, identifier: str, json_str: str) -> 'PluginInfo':
        """从 JSON 字符串反序列化"""
        data = json.loads(json_str)
        data['status'] = PluginStatus(data.get('status', 'unknown'))
        return cls(**data)


# ── 数据库模型 ────────────────────────────────────────────────────────────

# 插件注册表 DDL
PLUGIN_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS plugin_registry (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identifier      TEXT NOT NULL UNIQUE,          -- 插件唯一标识
    name            TEXT NOT NULL,                  -- 显示名称
    version         TEXT NOT NULL DEFAULT '0.1.0', -- 版本号
    author          TEXT DEFAULT '',                -- 作者
    description     TEXT DEFAULT '',                -- 描述
    min_app_version TEXT DEFAULT '1.0.0',           -- 最低应用版本
    path            TEXT DEFAULT '',                -- 插件目录绝对路径

    -- 元数据（完整 plugin.json 内容，JSON）
    metadata        TEXT DEFAULT '{}',

    -- 当前状态
    status          TEXT NOT NULL DEFAULT 'installed'
                    CHECK(status IN (
                        'unknown','installed','enabled',
                        'active','disabled','uninstalled','error'
                    )),

    -- 运行时配置（JSON）
    config          TEXT DEFAULT '{}',

    -- 依赖声明（JSON: {identifier: version_spec}）
    dependencies    TEXT DEFAULT '{}',

    -- 钩子声明（JSON array）
    provides_hooks  TEXT DEFAULT '[]',
    listens_hooks   TEXT DEFAULT '[]',

    -- 权限声明（JSON array）
    permissions     TEXT DEFAULT '[]',

    -- 配置 Schema（JSON）
    settings_schema TEXT DEFAULT '{}',

    -- 时间戳
    installed_at    TEXT DEFAULT NOW(),
    updated_at      TEXT DEFAULT NOW(),

    -- 最后一次错误信息
    last_error      TEXT DEFAULT ''
);

/* ★ v1.4 新增插件来源列 */
ALTER TABLE plugin_registry ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'store';

/* ★ v1.5 展示与分类字段（§5.1/§6.1） */
ALTER TABLE plugin_registry ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'other';
ALTER TABLE plugin_registry ADD COLUMN IF NOT EXISTS icon TEXT DEFAULT 'plugin';
ALTER TABLE plugin_registry ADD COLUMN IF NOT EXISTS tags TEXT DEFAULT '[]';
ALTER TABLE plugin_registry ADD COLUMN IF NOT EXISTS dashboard_meta TEXT DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_plugin_registry_status
    ON plugin_registry(status);

CREATE INDEX IF NOT EXISTS idx_plugin_registry_identifier
    ON plugin_registry(identifier);
"""


def init_plugin_registry_table():
    """初始化 plugin_registry 表（幂等）"""
    with get_registry_db() as conn:
        conn.executescript(PLUGIN_REGISTRY_DDL)
        print('[PluginManager] ✅ plugin_registry table ready')
        conn.commit()
