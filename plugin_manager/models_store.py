#!/usr/bin/env python3
"""
Plugin Manager — License & Store 数据库模型
=============================================
存放于主库 data/x7k2m9a4.db。

表:
  - plugin_licenses: 已激活的 License 记录
  - store_plugins:   远程商店插件缓存（本地镜像）
"""

import os
import json
from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, Dict, Any, List

from .models import get_registry_db


# ── 枚举 ──────────────────────────────────────────────────────────────

class LicenseType(str, Enum):
    FREE = 'free'           # 免费
    ONETIME = 'onetime'     # 一次性买断
    SUBSCRIPTION = 'sub'    # 订阅
    TRIAL = 'trial'         # 试用


class LicenseStatus(str, Enum):
    INACTIVE = 'inactive'           # 未激活
    ACTIVE = 'active'               # 正常
    EXPIRED = 'expired'             # 过期
    REVOKED = 'revoked'             # 吊销
    PENDING = 'pending'             # 待激活
    GRACE = 'grace'                 # 离线宽容期内


class PriceInterval(str, Enum):
    ONETIME = 'onetime'
    MONTHLY = 'month'
    YEARLY = 'year'


# ── DDL ───────────────────────────────────────────────────────────────

LICENSE_STORE_DDL = """
-- License 记录表
CREATE TABLE IF NOT EXISTS plugin_licenses (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plugin_id       TEXT NOT NULL,                  -- 插件 identifier
    license_key     TEXT NOT NULL UNIQUE,            -- License Key
    license_type    TEXT NOT NULL DEFAULT 'free'
                    CHECK(license_type IN ('free','onetime','sub','trial')),
    license_status  TEXT NOT NULL DEFAULT 'pending'
                    CHECK(license_status IN ('inactive','active','expired','revoked','pending','grace')),
    site_id         TEXT NOT NULL,                   -- 站点唯一标识
    site_name       TEXT DEFAULT '',                 -- 站点名称（客户自定义）
    customer_email  TEXT DEFAULT '',                 -- 购买者邮箱
    max_sites       BIGINT NOT NULL DEFAULT 1,      -- 最大激活站点数
    activated_at    TEXT,                            -- 首次激活时间
    expires_at      TEXT,                            -- 过期时间
    trial_ends_at   TEXT,                            -- 试用截止时间
    last_validated  TEXT,                            -- 最后验证时间
    offline_token   TEXT DEFAULT '',                 -- 离线 token（加密）
    grace_until     TEXT,                            -- 离线宽容截止时间
    order_id        TEXT DEFAULT '',                 -- 关联订单号
    subscription_id TEXT DEFAULT '',                 -- 关联订阅 ID
    auto_renew      BIGINT NOT NULL DEFAULT 0,      -- 是否自动续费
    metadata        TEXT DEFAULT '{}',               -- 扩展信息 JSON
    created_at      TEXT DEFAULT NOW(),
    updated_at      TEXT DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plugin_licenses_plugin
    ON plugin_licenses(plugin_id);
CREATE INDEX IF NOT EXISTS idx_plugin_licenses_key
    ON plugin_licenses(license_key);
CREATE INDEX IF NOT EXISTS idx_plugin_licenses_site
    ON plugin_licenses(site_id);

-- 商店插件缓存表
CREATE TABLE IF NOT EXISTS store_plugins (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identifier      TEXT NOT NULL UNIQUE,            -- 插件标识
    name            TEXT NOT NULL,                   -- 显示名称
    name_i18n_key   TEXT DEFAULT '',                 -- 名称 i18n 查找键（商店展示名按 identifier 解析）
    description     TEXT DEFAULT '',
    version         TEXT NOT NULL DEFAULT '0.1.0',
    author          TEXT DEFAULT '',
    author_url      TEXT DEFAULT '',
    icon_url        TEXT DEFAULT '',
    price_type      TEXT NOT NULL DEFAULT 'free'
                    CHECK(price_type IN ('free','onetime','sub','trial')),
    price_amount    BIGINT DEFAULT 0,               -- 价格（分）
    price_interval  TEXT DEFAULT 'onetime'
                    CHECK(price_interval IN ('onetime','month','year')),
    price_quarter_fen BIGINT DEFAULT 0,             -- 季价（分，订阅插件三档价之一）
    price_year_fen    BIGINT DEFAULT 0,             -- 年价（分，订阅插件三档价之一）
    compatible_editions TEXT DEFAULT '[]',          -- JSON array：适用版本（pro/standard/edge...）
    trial_days      BIGINT DEFAULT 0,               -- 试用天数
    download_url    TEXT DEFAULT '',                 -- 下载地址
    package_hash    TEXT DEFAULT '',                 -- 包签名哈希
    file_size       BIGINT DEFAULT 0,               -- 文件大小（bytes）
    category        TEXT DEFAULT '',
    tags            TEXT DEFAULT '[]',               -- JSON array
    min_app_version TEXT DEFAULT '0.10.0',
    depends_on      TEXT DEFAULT '{}',               -- JSON
    screenshots     TEXT DEFAULT '[]',
    readme_url      TEXT DEFAULT '',
    tagline         TEXT DEFAULT '',                 -- 宣传语（AI 提取/手写）
    tagline_i18n_key TEXT DEFAULT '',                -- 宣传语 i18n 查找键
    tagline_font_size TEXT DEFAULT '16px',           -- 宣传语字号
    tagline_color   TEXT DEFAULT '#ffffff',          -- 宣传语字体颜色
    tagline_subtitle TEXT DEFAULT '',                -- 宣传语副标题（第二行，≤64字符）
    tagline_subtitle_font_size TEXT DEFAULT '14px',   -- 副标题字号
    usage_guide     TEXT DEFAULT '',                 -- 插件使用说明（富文本 HTML，独立于 readme，同步知识库）
    readme_cache    TEXT DEFAULT '',                 -- README 缓存（服务端代理，多命名抓取，前端详情走本地端点）
    downloads       BIGINT DEFAULT 0,
    rating          DOUBLE PRECISION DEFAULT 0.0,
    review_count    BIGINT DEFAULT 0,               -- 评价总数
    enabled         BIGINT NOT NULL DEFAULT 1,      -- 是否上架
    catalog_managed BIGINT NOT NULL DEFAULT 1,      -- 1=目录同步管理（可被 sync 覆盖）；0=直写条目（sync 跳过覆盖）
    created_at      TEXT DEFAULT NOW(),
    updated_at      TEXT DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plugin_reviews (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plugin_identifier TEXT NOT NULL,
    user_id           BIGINT NOT NULL,
    user_name         TEXT DEFAULT '',
    rating            BIGINT NOT NULL CHECK(rating >= 1 AND rating <= 5),
    content           TEXT DEFAULT '',
    version           TEXT DEFAULT '',
    is_active         BIGINT DEFAULT 1,
    reply_content     TEXT DEFAULT '',
    reply_at          TEXT,
    created_at        TEXT DEFAULT NOW(),
    UNIQUE(plugin_identifier, user_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_plugin
    ON plugin_reviews(plugin_identifier, is_active, created_at);

CREATE INDEX IF NOT EXISTS idx_store_plugins_category
    ON store_plugins(category);
CREATE INDEX IF NOT EXISTS idx_store_plugins_price
    ON store_plugins(price_type);

-- 插件审核队列表（VeroRun 插件审核网关 · 批次2）
-- 上传插件先进 pending 队列，AI 规则审核 + 人工审批通过后才安装
CREATE TABLE IF NOT EXISTS plugin_submissions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identifier      TEXT NOT NULL,                  -- 插件标识
    name            TEXT DEFAULT '',
    version         TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','approved','rejected')),
    submitter       TEXT DEFAULT '',                 -- 上传者（JWT 用户名）
    submitter_id    TEXT DEFAULT '',                 -- 上传者 user_id
    file_path       TEXT DEFAULT '',                 -- 暂存目录（plugins/.pending/<id>/）
    file_size       BIGINT DEFAULT 0,               -- 上传包大小（bytes）
    wm_method       TEXT DEFAULT '',                 -- 水印检测方式
    wm_reason       TEXT DEFAULT '',
    -- P0-C：上架元数据（第三方开发者提交物完整采集）
    description     TEXT DEFAULT '',                 -- 描述
    category        TEXT DEFAULT '',                 -- 分类
    tagline         TEXT DEFAULT '',                 -- 宣传语
    screenshots     TEXT DEFAULT '[]',               -- 截图 JSON array
    readme_url      TEXT DEFAULT '',                 -- 文档 URL
    compatible_editions TEXT DEFAULT '[]',           -- 适用版本 JSON array
    min_app_version TEXT DEFAULT '',                 -- 最低应用版本
    agent_role      TEXT DEFAULT '',                 -- agent 核心角色
    capabilities    TEXT DEFAULT '[]',               -- capabilities JSON array
    developer_id    BIGINT DEFAULT 0,                -- P0-A 归属开发者 id（0=官方/未归属）
    price_type      TEXT DEFAULT 'free',             -- 定价类型（free/onetime/subscription）
    price_amount    BIGINT DEFAULT 0,                -- 价格（fen）
    price_interval  TEXT DEFAULT 'onetime',          -- 订阅周期（monthly/quarterly/yearly/onetime）
    audit_status    TEXT NOT NULL DEFAULT 'pending'
                    CHECK(audit_status IN ('pending','pass','manual','reject')),
    audit_report    TEXT DEFAULT '{}',               -- 审核报告 JSON
    audit_reasons   TEXT DEFAULT '[]',               -- 审核理由 JSON array
    review_comment  TEXT DEFAULT '',
    reviewed_at     TEXT,
    created_at      TEXT DEFAULT NOW(),
    updated_at      TEXT DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_submissions_status
    ON plugin_submissions(status);

-- 开发者中心：store_developers 表（单账号双身份，绑定统一 JWT user_id）
-- P0-B：开发者 = 普通用户的身份升级，复用统一 JWT SSO，不新建独立账号体系。
CREATE TABLE IF NOT EXISTS store_developers (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id           BIGINT NOT NULL UNIQUE,   -- 关联统一账号（JWT user_id）
    display_name      TEXT NOT NULL,            -- 开发者品牌名
    slug              TEXT NOT NULL UNIQUE,     -- URL 唯一标识 [a-z0-9_-]
    email             TEXT DEFAULT '',          -- 审计 P1-3 修复：注册时落库的验证邮箱（主库 users.email 来源）
    bio               TEXT DEFAULT '',
    website           TEXT DEFAULT '',
    email_verified    BIGINT NOT NULL DEFAULT 0, -- P0-3：邮箱验证通过后置 1
    verify_token      TEXT DEFAULT '',           -- P0-3：一次性邮箱验证令牌（哈希存储）
    verify_expires    TEXT DEFAULT '',           -- P0-3：令牌过期时间
    verify_level      TEXT NOT NULL DEFAULT 'free'
                      CHECK(verify_level IN ('free','email','identity','org')),
    payout_ratio      BIGINT NOT NULL DEFAULT 80,   -- 分成比例%（开发者侧）
    payout_account    TEXT DEFAULT '{}',            -- 结算账户 JSON（加密存储）
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK(status IN ('active','suspended','banned')),
    agreement_signed_at TEXT,
    created_at        TEXT DEFAULT NOW(),
    updated_at        TEXT DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_developers_user ON store_developers(user_id);
CREATE INDEX IF NOT EXISTS idx_developers_status ON store_developers(status);

-- 版本管理：store_plugin_versions 表（P1：第三方插件多版本历史）
-- status: pending=待审 / approved=已审通过 / rejected=驳回 / live=已上架 / retired=已下架
CREATE TABLE IF NOT EXISTS store_plugin_versions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plugin_id      TEXT NOT NULL,                  -- 插件 identifier
    developer_id   BIGINT DEFAULT 0,               -- 归属开发者（store_developers.id）
    version        TEXT NOT NULL,                  -- semver（允许 X.Y.Z-prerelease）
    changelog      TEXT DEFAULT '',                -- 更新日志
    package_hash   TEXT DEFAULT '',                -- 包 SHA256
    file_size      BIGINT DEFAULT 0,               -- 包大小（bytes）
    download_url   TEXT DEFAULT '',                -- 下载地址
    file_path      TEXT DEFAULT '',                -- 审计 B2 修复：本地上传包存储路径（plugins/.versions/...）
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','approved','rejected','live','retired')),
    created_at     TEXT DEFAULT NOW(),
    updated_at     TEXT DEFAULT NOW(),
    UNIQUE(plugin_id, version)
);

CREATE INDEX IF NOT EXISTS idx_pv_plugin ON store_plugin_versions(plugin_id, status);

-- 收益分成：developer_payouts 表（P2：80/20 分成结算记录）
-- 按月聚合 plugin_payment_orders + plugin_subscriptions 支付流水生成，
-- 金额口径统一 fen。UNIQUE(developer_id, period) 保证幂等重跑。
CREATE TABLE IF NOT EXISTS developer_payouts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    developer_id    BIGINT NOT NULL,           -- store_developers.id
    period          TEXT NOT NULL,             -- 结算周期 'YYYY-MM'
    gross_fen       BIGINT DEFAULT 0,          -- 该周期总流水（分）
    ratio           BIGINT DEFAULT 80,         -- 分成比例%（开发者侧）
    amount_fen      BIGINT DEFAULT 0,          -- 应付 = gross*ratio/100（分）
    fee_fen         BIGINT DEFAULT 0,          -- 渠道费/税（分）
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','paid','void')),
    paid_at         TEXT,
    created_at      TEXT DEFAULT NOW(),
    UNIQUE(developer_id, period)
);

CREATE INDEX IF NOT EXISTS idx_payouts_dev
    ON developer_payouts(developer_id, status);

-- 提现申请（P2-2：开发者发起提现，金额口径统一 fen）
CREATE TABLE IF NOT EXISTS developer_withdrawals (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    developer_id   BIGINT NOT NULL,           -- store_developers.id
    amount_fen     BIGINT NOT NULL DEFAULT 0, -- 申请提现金额（分）
    account        TEXT DEFAULT '',           -- 申请时结算账户快照（JSON）
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','processing','paid','rejected','cancelled')),
    applied_at     TEXT DEFAULT NOW(),
    processed_at   TEXT,
    created_at     TEXT DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_withdrawals_dev
    ON developer_withdrawals(developer_id, status);

-- 轻量技能层（P0-1）：社区 UGC，Markdown front-matter + 正文，低门槛发布
CREATE TABLE IF NOT EXISTS store_skills (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identifier          TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    description         TEXT DEFAULT '',
    tagline             TEXT DEFAULT '',
    tags                TEXT DEFAULT '[]',          -- JSON array
    content_md          TEXT NOT NULL,              -- front-matter + markdown 正文
    author_developer_id BIGINT DEFAULT 0,           -- store_developers.id（0=官方）
    version             TEXT NOT NULL DEFAULT '1.0.0',
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected','removed')),
    audit_status        TEXT NOT NULL DEFAULT 'pending'
                        CHECK(audit_status IN ('pending','pass','manual','reject')),
    audit_note          TEXT DEFAULT '',
    rating              DOUBLE PRECISION DEFAULT 0.0,
    installs            BIGINT DEFAULT 0,
    created_at          TEXT DEFAULT NOW(),
    updated_at          TEXT DEFAULT NOW(),
    published_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_store_skills_status
    ON store_skills(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_store_skills_author
    ON store_skills(author_developer_id);

-- P2-5: MCP 集成（插件声明 MCP server，Agent 会话消费其工具）
CREATE TABLE IF NOT EXISTS plugin_mcp_servers (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plugin_id    TEXT NOT NULL,
    server_name  TEXT NOT NULL DEFAULT 'default',
    config       TEXT NOT NULL DEFAULT '{}',  -- JSON: {command, args[], env{}, transport}
    enabled      BIGINT NOT NULL DEFAULT 1,
    created_at   TEXT DEFAULT NOW(),
    updated_at   TEXT DEFAULT NOW(),
    UNIQUE(plugin_id, server_name)
);
CREATE INDEX IF NOT EXISTS idx_plugin_mcp_enabled
    ON plugin_mcp_servers(plugin_id, enabled);

-- ── 技能管理（Skill Management）：依赖边 / 安装记录 / 提交流水 ──────
-- 对齐技能依赖声明；kind 区分插件/技能/角色/能力四类依赖
CREATE TABLE IF NOT EXISTS skill_requirements (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    skill_id    BIGINT NOT NULL REFERENCES store_skills(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK(kind IN ('plugin','skill','role','capability')),
    slug        TEXT NOT NULL,
    version_spec TEXT DEFAULT ''          -- 复合约束串，如 ">=2.0.0, <3.0.0"；空=不限
);
CREATE INDEX IF NOT EXISTS idx_skill_req_target ON skill_requirements(kind, slug);
CREATE INDEX IF NOT EXISTS idx_skill_req_skill  ON skill_requirements(skill_id);

-- 安装记录：版本钉死 + 按站点预留（P0 单站点 site_id=0）
CREATE TABLE IF NOT EXISTS skill_installations (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    skill_id     BIGINT NOT NULL REFERENCES store_skills(id) ON DELETE CASCADE,
    site_id      BIGINT NOT NULL DEFAULT 0,
    version      TEXT NOT NULL,
    enabled      BIGINT NOT NULL DEFAULT 1,
    installed_at TEXT DEFAULT NOW(),
    updated_at   TEXT DEFAULT NOW(),
    UNIQUE(skill_id, site_id)
);
CREATE INDEX IF NOT EXISTS idx_skill_inst_site ON skill_installations(site_id, enabled);

-- 提交流水：用户上传/提交先进队列，审核通过后晋升 store_skills
CREATE TABLE IF NOT EXISTS skill_submissions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identifier     TEXT NOT NULL,
    site_id        BIGINT NOT NULL DEFAULT 0,
    author_id      BIGINT NOT NULL DEFAULT 0,
    content_md     TEXT NOT NULL,
    skill_json     TEXT DEFAULT '{}',
    audit_status   TEXT NOT NULL DEFAULT 'pending'
                   CHECK(audit_status IN ('pending','pass','manual','reject')),
    audit_reasons  TEXT DEFAULT '[]',
    review_comment TEXT DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK(status IN ('pending','approved','rejected','removed')),
    created_at     TEXT DEFAULT NOW(),
    reviewed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_skill_sub_status ON skill_submissions(status, created_at);

-- ── v1.8 动态分类与分流（轻量增量方案 §3 / 标准 §18）──────────────────────
-- 说明：两张卫星表落在商店库；store_plugins 元数据仍是唯一商品事实源。
--       plugin_distribution_rules.identifier 为 UNIQUE → 与 store_plugins 严格 1:1，
--       仅承载「分流附加语义」，不存在双写漂移。
-- 约束：全部 IF NOT EXISTS，可空；不新增任何既有表约束。
CREATE TABLE IF NOT EXISTS plugin_categories (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,            -- 分类键，[a-z0-9_]+
    label           TEXT NOT NULL DEFAULT '',        -- 展示名（zh-CN）
    label_i18n_key  TEXT DEFAULT '',                 -- i18n 查找键
    emoji           TEXT DEFAULT '',                 -- emoji 占位符
    icon_svg        TEXT DEFAULT '',                 -- 图标 path 片段（内置 7 枚由前端 fallback 提供）
    grad_from       TEXT DEFAULT '#3b82f6',          -- Banner 渐变起色
    grad_to         TEXT DEFAULT '#8b5cf6',          -- Banner 渐变止色
    default_tagline TEXT DEFAULT '',                 -- 类别默认宣传语（标准 §14.7.3 第 4 档）
    sort_order      BIGINT NOT NULL DEFAULT 100,
    enabled         BIGINT NOT NULL DEFAULT 1,
    builtin         BIGINT NOT NULL DEFAULT 0,       -- 1=内置（不可删）
    is_official     BIGINT NOT NULL DEFAULT 0,
    created_at      TEXT DEFAULT NOW(),
    updated_at      TEXT DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_plugin_categories_order ON plugin_categories(enabled, sort_order);

CREATE TABLE IF NOT EXISTS plugin_distribution_rules (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identifier  TEXT NOT NULL UNIQUE,                -- 插件标识（1:1 对应 store_plugins）
    editions    TEXT NOT NULL DEFAULT '[]',          -- JSON：适用版侧；[] = 继承 yaml
    profiles    TEXT NOT NULL DEFAULT '[]',          -- JSON：适用环境画像；[] = 不限
    categories  TEXT NOT NULL DEFAULT '[]',          -- JSON：归属分类（指向 plugin_categories.key）
    channel     TEXT NOT NULL DEFAULT 'stable'
                CHECK(channel IN ('stable','beta','canary')),
    priority    BIGINT NOT NULL DEFAULT 100,         -- 预留：同插件多规则优先级
    hidden      BIGINT NOT NULL DEFAULT 0,           -- 1=强制隐藏（覆盖一切）
    note        TEXT DEFAULT '',
    updated_by  TEXT DEFAULT '',
    updated_at  TEXT DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_plugin_dist_rules_hidden ON plugin_distribution_rules(hidden, identifier);

-- ── 发行版矩阵（方案Ⅰ：业务版 × 形态 拆行；官方版/网站版「只排不白」）────
-- 定位：v1.8 动态分流的「发行版默认范围」权威源。与 plugin_distribution_rules
--       （单插件覆盖层）互补，二者皆读同一 resolve() 优先级链（标准 §18.2）。
--       default_exclude 仅存「本版本默认排除」；官方版/网站版=全量可选 → 不设 include。
--       edition 为部署侧 VR_EDITION 的实际取值（如 standard-web / research-desktop），
--       命中即裁；edition_catalog 无记录 → 回落 yaml 语义，行为与 v1.8 一致。
-- 约束：全部 IF NOT EXISTS，可空；不新增任何既有表约束。
CREATE TABLE IF NOT EXISTS edition_catalog (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition         TEXT NOT NULL UNIQUE,            -- 复合键：standard-web / pro-web /
                                                    --   research-desktop / finance-desktop /
                                                    --   official / edge（未来）
    label           TEXT NOT NULL DEFAULT '',        -- 展示名（标准网站版…）
    form_factor     TEXT NOT NULL DEFAULT '',        -- desktop | web | edge
    enabled         BIGINT NOT NULL DEFAULT 1,
    default_exclude TEXT NOT NULL DEFAULT '[]',      -- JSON 数组：本版本默认排除插件
    note            TEXT DEFAULT '',
    updated_by      TEXT DEFAULT '',
    created_at      TEXT DEFAULT NOW(),
    updated_at      TEXT DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_edition_catalog_enabled ON edition_catalog(enabled, edition);
"""


# ── 初始化 ─────────────────────────────────────────────────────────────

# ── B1 修复：plugin_submissions 幂等补列迁移 ──────────────────────────
# PG 的 CREATE TABLE IF NOT EXISTS 不会为已存在的旧表补列；
# 以下语句保证新旧库启动即自愈（ADD COLUMN IF NOT EXISTS 幂等）。
_SUBMISSIONS_COLUMN_MIGRATIONS = [
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS "
    "audit_status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS audit_report TEXT DEFAULT '{}'",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS audit_reasons TEXT DEFAULT '[]'",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS review_comment TEXT DEFAULT ''",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS reviewed_at TEXT",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS wm_method TEXT DEFAULT ''",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS wm_reason TEXT DEFAULT ''",
    # P0-A：提审归属（第三方开发者提交物关联 store_developers.id，0=官方/未归属）
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS developer_id BIGINT DEFAULT 0",
    # P0-C：上架元数据列（第三方提交物完整采集；旧库缺列会导致 developer_submit 500）
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS category TEXT DEFAULT ''",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS tagline TEXT DEFAULT ''",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS screenshots TEXT DEFAULT '[]'",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS readme_url TEXT DEFAULT ''",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS compatible_editions TEXT DEFAULT '[]'",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS min_app_version TEXT DEFAULT ''",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS agent_role TEXT DEFAULT ''",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS capabilities TEXT DEFAULT '[]'",
    # 审计 P1-1 修复：定价申报列（真实提审链路 developer_submit 采集 price_type/price_amount/price_interval）
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS price_type TEXT DEFAULT 'free'",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS price_amount BIGINT DEFAULT 0",
    "ALTER TABLE plugin_submissions ADD COLUMN IF NOT EXISTS price_interval TEXT DEFAULT 'onetime'",
]

# ── 商店表幂等补列迁移（tagline / i18n 键 / last_sync_ts）────────────
_STORE_COLUMN_MIGRATIONS = [
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS name_i18n_key TEXT DEFAULT ''",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS tagline TEXT DEFAULT ''",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS tagline_i18n_key TEXT DEFAULT ''",
    # 标语字号/颜色（手动标语样式；旧库缺列会导致保存与目录同步 500）
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS tagline_font_size TEXT DEFAULT '16px'",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS tagline_color TEXT DEFAULT '#ffffff'",
    # 双行标语：副标题 + 独立字号（问题1，2026-08-29）
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS tagline_subtitle TEXT DEFAULT ''",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS tagline_subtitle_font_size TEXT DEFAULT '14px'",
    # 插件使用说明（问题3，2026-08-29）
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS usage_guide TEXT DEFAULT ''",
    # README 服务端代理缓存（问题2 方案A，2026-08-29）
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS readme_cache TEXT DEFAULT ''",
    # P0-2：目录同步时间戳持久化（TEXT 存 ISO 时间串，与 store_plugins 其余时间列一致）
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS last_sync_ts TEXT DEFAULT ''",
    # 订阅三档价 + 适用版本（阶段1：定价机制完整化）
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS price_quarter_fen BIGINT DEFAULT 0",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS price_year_fen BIGINT DEFAULT 0",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS compatible_editions TEXT DEFAULT '[]'",
    # ── P0-A：官方/第三方标识 ────────────────────────────────────────────
    # is_official: 1=官方插件（白名单或目录来源 store），0=第三方开发者上架
    # developer_id: 关联 store_developers.id（0=官方/未归属）
    # developer_org: 开发者/组织展示名（冗余，避免每行 join）
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS is_official BIGINT NOT NULL DEFAULT 1",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS developer_id BIGINT DEFAULT 0",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS developer_org TEXT DEFAULT ''",
    # P0-A：直写兜底标记（--local-only 直写条目置 0，sync 跳过覆盖防清除）
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS catalog_managed BIGINT NOT NULL DEFAULT 1",
    # P1-3：Coze 式配额声明（工具数 / 依赖包大小 KB / 声明 QPS）
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS max_tools BIGINT DEFAULT 100",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS max_dependencies_kb BIGINT DEFAULT 204800",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS declared_qps BIGINT DEFAULT 50",
    # ── v1.8：动态分类与分流关联列（轻量增量方案 §3.3）──────────────────────
    # distribution_note：分流备注（与 plugin_distribution_rules.note 互补，主表侧留痕）
    # release_channel ：发布渠道，与 plugin_distribution_rules.channel 同义，便于 SQL 直查
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS distribution_note TEXT DEFAULT ''",
    "ALTER TABLE store_plugins ADD COLUMN IF NOT EXISTS release_channel TEXT DEFAULT 'stable'",
]

# ── P0-B：store_developers 幂等补列迁移（新表无历史数据，按惯例提供）──
_DEVELOPER_COLUMN_MIGRATIONS = [
    # 审计 P1-3 修复：验证邮箱落库（旧表缺列导致注册 INSERT 500）
    "ALTER TABLE store_developers ADD COLUMN IF NOT EXISTS email TEXT DEFAULT ''",
    "ALTER TABLE store_developers ADD COLUMN IF NOT EXISTS payout_ratio BIGINT DEFAULT 80",
    "ALTER TABLE store_developers ADD COLUMN IF NOT EXISTS payout_account TEXT DEFAULT '{}'",
    "ALTER TABLE store_developers ADD COLUMN IF NOT EXISTS verify_level TEXT DEFAULT 'free'",
    "ALTER TABLE store_developers ADD COLUMN IF NOT EXISTS verify_token TEXT DEFAULT ''",
    "ALTER TABLE store_developers ADD COLUMN IF NOT EXISTS verify_expires TEXT DEFAULT ''",
]


def _migrate_store_columns(conn):
    """幂等补列：对已存在的旧 store_plugins 表补齐新列。"""
    for stmt in _STORE_COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception as _e:
            print(f'[PluginManager] ⚠️ store migration skipped: {_e}')


def _migrate_submissions_columns(conn):
    """幂等补列：对已存在的旧 plugin_submissions 表补齐新列。"""
    for stmt in _SUBMISSIONS_COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception as _e:
            print(f'[PluginManager] ⚠️ submissions migration skipped: {_e}')


def _migrate_developers_columns(conn):
    """幂等补列：对已存在的旧 store_developers 表补齐新列。"""
    for stmt in _DEVELOPER_COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception as _e:
            print(f'[PluginManager] ⚠️ developers migration skipped: {_e}')


# ── P3 合规加固：plugin_reviews 幂等补列（差评预警 + 举报标记）────────
_REVIEWS_COLUMN_MIGRATIONS = [
    "ALTER TABLE plugin_reviews ADD COLUMN IF NOT EXISTS flagged BIGINT DEFAULT 0",
]


# ── 审计 B2 修复：store_plugin_versions 幂等补列（本地版本包路径）────
_VERSIONS_COLUMN_MIGRATIONS = [
    "ALTER TABLE store_plugin_versions ADD COLUMN IF NOT EXISTS file_path TEXT DEFAULT ''",
]

# ── 技能管理：store_skills 幂等补列（P0 技能依赖/来源/提示词/站点）────
_SKILL_COLUMN_MIGRATIONS = [
    "ALTER TABLE store_skills ADD COLUMN IF NOT EXISTS requirements TEXT DEFAULT '{}'",
    "ALTER TABLE store_skills ADD COLUMN IF NOT EXISTS permissions  TEXT DEFAULT '[]'",
    "ALTER TABLE store_skills ADD COLUMN IF NOT EXISTS source       TEXT NOT NULL DEFAULT 'community'",
    "ALTER TABLE store_skills ADD COLUMN IF NOT EXISTS signature    TEXT DEFAULT ''",
    "ALTER TABLE store_skills ADD COLUMN IF NOT EXISTS site_id      BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE store_skills ADD COLUMN IF NOT EXISTS prompt_md    TEXT DEFAULT ''",
    "ALTER TABLE store_skills ADD COLUMN IF NOT EXISTS schema_ver   TEXT NOT NULL DEFAULT '0.9'",
]

# ── v1.8：动态分类 / 动态分流 幂等迁移 + 内置分类种子 ──────────────────────
# 两张卫星表由 LICENSE_STORE_DDL 的 CREATE TABLE IF NOT EXISTS 保证存在；
# 本列表用于为**已存在的旧表**补列（PG 的 CREATE TABLE IF NOT EXISTS 不补列）。
_DISTRIBUTION_COLUMN_MIGRATIONS = [
    # 预留：未来新增列在此追加，形如
    # "ALTER TABLE plugin_categories ADD COLUMN IF NOT EXISTS xxx TEXT DEFAULT ''",
]

# 内置 7 类分类种子（与 store_importer.CATEGORY_ENUM 一一对应，builtin=1）。
# 说明：ON CONFLICT DO NOTHING → 只补缺失键，**不覆盖管理员已改的展示名 / emoji / 渐变**。
#       icon_svg 故意留空：内置图标由前端 _CAT_ICONS 兜底，避免把长 SVG 路径写进 SQL。
#       渐变取值与标准 §14.7.1 内置默认表一致。
_BUILTIN_CATEGORY_SEED = (
    ('system',       '系统',      '⚙️', '#4F6EF7', '#1E2A78', 10),
    ('shop',         '商城',      '🛒', '#12B886', '#0B7285', 20),
    ('content',      '内容',      '📝', '#FF6B35', '#E0316B', 30),
    ('ai_agent',     'AI 智能体',  '🤖', '#845EF7', '#D6336C', 40),
    ('social',       '社媒',      '📱', '#22B8CF', '#4C6EF5', 50),
    ('tools',        '工具',      '🔧', '#748FFC', '#364FC7', 60),
    ('supply_chain', '供应链',    '📦', '#F59F00', '#E8590C', 70),
)

# 发行版矩阵内置种子（方案Ⅰ：业务版 × 形态 拆行；「只排不白」）。
# ON CONFLICT DO NOTHING → 只补缺失 edition，不覆盖管理员已改的排除清单/label。
# 边缘插件（iot_hub/ros_bridge/cogevolution_substrate）对所有现行发行版一律排除：
#   官方版 / 网站版 = 全量可选 → 仅预填边缘排除；
#   research/finance 桌面版同理（连同各自已有的业务排除由管理员后续在后台维护）。
# 未来边缘版（edge）未立项，不在种子内（避免同步写死非边缘插件的反向排除）。
_EDGE_ONLY_EXCLUDES = ('iot_hub', 'ros_bridge', 'cogevolution_substrate')
_EDITION_CATALOG_SEED = (
    ('standard-web',       '标准网站版', 'web',     _EDGE_ONLY_EXCLUDES),
    ('pro-web',            '专业网站版', 'web',     _EDGE_ONLY_EXCLUDES),
    ('research-desktop',   '科研桌面版', 'desktop', _EDGE_ONLY_EXCLUDES),
    ('finance-desktop',    '金融桌面版', 'desktop', _EDGE_ONLY_EXCLUDES),
    ('official',           '官方版',     'web',     _EDGE_ONLY_EXCLUDES),
)


def _migrate_distribution_columns(conn):
    """幂等：v1.8 动态分类/分流表补列 + 内置分类种子 + 发行版矩阵种子（失败不阻断启动）。"""
    for stmt in _DISTRIBUTION_COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception as _e:
            print(f'[PluginManager] ⚠️ distribution migration skipped: {_e}')
    try:
        for key, label, emoji, g_from, g_to, order in _BUILTIN_CATEGORY_SEED:
            conn.execute(
                "INSERT INTO plugin_categories "
                "(key, label, emoji, grad_from, grad_to, sort_order, enabled, builtin, is_official) "
                "VALUES (%s,%s,%s,%s,%s,%s,1,1,1) ON CONFLICT(key) DO NOTHING",
                (key, label, emoji, g_from, g_to, order))
    except Exception as _e:
        print(f'[PluginManager] ⚠️ builtin category seed skipped: {_e}')
    try:
        import json as _json
        for edition, label, form, excludes in _EDITION_CATALOG_SEED:
            conn.execute(
                "INSERT INTO edition_catalog "
                "(edition, label, form_factor, default_exclude, enabled) "
                "VALUES (%s,%s,%s,%s,1) ON CONFLICT(edition) DO NOTHING",
                (edition, label, form, _json.dumps(list(excludes), ensure_ascii=False)))
    except Exception as _e:
        print(f'[PluginManager] ⚠️ edition_catalog seed skipped: {_e}')

# 存量回填（幂等：WHERE 条件保证只回填一次；二次执行结果不变）
_SKILL_BACKFILL_SQL = """
UPDATE store_skills
SET source = CASE WHEN author_developer_id = 0 THEN 'official' ELSE 'community' END,
    prompt_md = content_md,
    schema_ver = '0.9'
WHERE schema_ver IS NULL OR schema_ver = ''
"""


def _migrate_reviews_columns(conn):
    """幂等补列：对已存在的旧 plugin_reviews 表补齐 flagged 列。"""
    for stmt in _REVIEWS_COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception as _e:
            print(f'[PluginManager] ⚠️ reviews migration skipped: {_e}')


def _migrate_versions_columns(conn):
    """幂等补列：对已存在的旧 store_plugin_versions 表补齐 file_path 列。"""
    for stmt in _VERSIONS_COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception as _e:
            print(f'[PluginManager] ⚠️ versions migration skipped: {_e}')


def _migrate_skill_columns(conn):
    """幂等补列 + 回填：store_skills 补齐技能管理列；回填存量 0.9 技能。"""
    for stmt in _SKILL_COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception as _e:
            print(f'[PluginManager] ⚠️ skill migration skipped: {_e}')
    try:
        conn.execute(_SKILL_BACKFILL_SQL)   # UPDATE 幂等（WHERE 条件保证只回填一次）
    except Exception as _e:
        print(f'[PluginManager] ⚠️ skill backfill skipped: {_e}')


# ── 并发安全：DDL 迁移用 PG 会话级 advisory lock 串行化 ──────────────
# 多个 gunicorn worker 并发启动时同时执行 CREATE/ALTER 会形成表锁环
# （AccessExclusiveLock ↔ ShareLock）→ DeadlockDetected，导致 PluginManager
# 初始化失败、插件蓝图未注册、/admin/plugins/* 全部被 SPA 兜底吞掉（商店缺失事故）。
# 锁绑定会话：连接关闭/进程崩溃即自动释放，等待方不会饿死。
# 775220 与 manager.py 商店同步循环使用的 775219 区分开。
_DDL_ADVISORY_LOCK_KEY = 775220


def init_license_store_tables():
    """幂等初始化 License + Store 表（并发安全：advisory lock 串行化 DDL）"""
    with get_registry_db() as conn:
        conn.execute('SELECT pg_advisory_lock(%s)', (_DDL_ADVISORY_LOCK_KEY,))
        try:
            conn.executescript(LICENSE_STORE_DDL)
            _migrate_submissions_columns(conn)
            _migrate_store_columns(conn)
            _migrate_developers_columns(conn)
            _migrate_reviews_columns(conn)
            _migrate_versions_columns(conn)
            _migrate_skill_columns(conn)
            # v1.8：动态分类/分流表补列 + 内置分类种子（纯追加，不动既有 6 个迁移）
            _migrate_distribution_columns(conn)
            print(f'[PluginManager] ✅ plugin_licenses + store_plugins tables ready')
            conn.commit()
        finally:
            try:
                conn.execute('SELECT pg_advisory_unlock(%s)', (_DDL_ADVISORY_LOCK_KEY,))
            except Exception:
                pass  # 事务已中止/连接将关闭时，锁由会话结束自动释放


# ── LicenseRecord 数据类 ──────────────────────────────────────────────

@dataclass
class LicenseRecord:
    plugin_id: str
    license_key: str
    license_type: LicenseType = LicenseType.FREE
    license_status: LicenseStatus = LicenseStatus.PENDING
    site_id: str = ''
    site_name: str = ''
    customer_email: str = ''
    max_sites: int = 1
    activated_at: Optional[str] = None
    expires_at: Optional[str] = None
    trial_ends_at: Optional[str] = None
    last_validated: Optional[str] = None
    offline_token: str = ''
    grace_until: Optional[str] = None
    order_id: str = ''
    subscription_id: str = ''
    auto_renew: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    id: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d['license_type'] = self.license_type.value
        d['license_status'] = self.license_status.value
        if isinstance(d.get('metadata'), dict):
            d['metadata'] = json.dumps(d['metadata'], ensure_ascii=False)
        return d

    @classmethod
    def from_row(cls, row: dict) -> 'LicenseRecord':
        return cls(
            id=row['id'],
            plugin_id=row['plugin_id'],
            license_key=row['license_key'],
            license_type=LicenseType(row['license_type']),
            license_status=LicenseStatus(row['license_status']),
            site_id=row.get('site_id', ''),
            site_name=row.get('site_name', ''),
            customer_email=row.get('customer_email', ''),
            max_sites=row.get('max_sites', 1),
            activated_at=row.get('activated_at'),
            expires_at=row.get('expires_at'),
            trial_ends_at=row.get('trial_ends_at'),
            last_validated=row.get('last_validated'),
            offline_token=row.get('offline_token', ''),
            grace_until=row.get('grace_until'),
            order_id=row.get('order_id', ''),
            subscription_id=row.get('subscription_id', ''),
            auto_renew=bool(row.get('auto_renew', 0)),
            metadata=json.loads(row.get('metadata', '{}')),
            created_at=row.get('created_at'),
            updated_at=row.get('updated_at'),
        )


# ── StorePlugin 数据类 ───────────────────────────────────────────────

@dataclass
class StorePlugin:
    identifier: str
    name: str
    name_i18n_key: str = ''
    description: str = ''
    version: str = '0.1.0'
    author: str = ''
    author_url: str = ''
    icon_url: str = ''
    price_type: str = 'free'
    price_amount: int = 0
    price_interval: str = 'onetime'
    price_quarter_fen: int = 0
    price_year_fen: int = 0
    compatible_editions: List[str] = field(default_factory=list)
    trial_days: int = 0
    download_url: str = ''
    package_hash: str = ''
    file_size: int = 0
    category: str = ''
    tags: List[str] = field(default_factory=list)
    min_app_version: str = '0.10.0'
    depends_on: Dict[str, str] = field(default_factory=dict)
    screenshots: List[str] = field(default_factory=list)
    readme_url: str = ''
    tagline: str = ''
    tagline_i18n_key: str = ''
    tagline_font_size: str = '16px'
    tagline_color: str = '#ffffff'
    tagline_subtitle: str = ''
    tagline_subtitle_font_size: str = '14px'
    usage_guide: str = ''
    readme_cache: str = ''
    downloads: int = 0
    rating: float = 0.0
    review_count: int = 0
    enabled: bool = True
    # P0-A：官方/第三方标识
    is_official: bool = True
    developer_id: int = 0
    developer_org: str = ''
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    id: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_row(cls, row: dict) -> 'StorePlugin':
        return cls(
            id=row['id'],
            identifier=row['identifier'],
            name=row['name'],
            name_i18n_key=row.get('name_i18n_key', ''),
            description=row.get('description', ''),
            version=row.get('version', '0.1.0'),
            author=row.get('author', ''),
            author_url=row.get('author_url', ''),
            icon_url=row.get('icon_url', ''),
            price_type=row.get('price_type', 'free'),
            price_amount=row.get('price_amount', 0),
            price_interval=row.get('price_interval', 'onetime'),
            price_quarter_fen=row.get('price_quarter_fen', 0),
            price_year_fen=row.get('price_year_fen', 0),
            compatible_editions=json.loads(row.get('compatible_editions', '[]')),
            trial_days=row.get('trial_days', 0),
            download_url=row.get('download_url', ''),
            package_hash=row.get('package_hash', ''),
            file_size=row.get('file_size', 0),
            category=row.get('category', ''),
            tags=json.loads(row.get('tags', '[]')),
            min_app_version=row.get('min_app_version', '0.10.0'),
            depends_on=json.loads(row.get('depends_on', '{}')),
            screenshots=json.loads(row.get('screenshots', '[]')),
            readme_url=row.get('readme_url', ''),
            tagline=row.get('tagline', ''),
            tagline_i18n_key=row.get('tagline_i18n_key', ''),
            tagline_font_size=row.get('tagline_font_size', '16px'),
            tagline_color=row.get('tagline_color', '#ffffff'),
            tagline_subtitle=row.get('tagline_subtitle', ''),
            tagline_subtitle_font_size=row.get('tagline_subtitle_font_size', '14px'),
            usage_guide=row.get('usage_guide', ''),
            readme_cache=row.get('readme_cache', ''),
            downloads=row.get('downloads', 0),
            rating=row.get('rating', 0.0),
            review_count=row.get('review_count', 0),
            enabled=bool(row.get('enabled', 1)),
            is_official=bool(row.get('is_official', 1)),
            developer_id=row.get('developer_id', 0),
            developer_org=row.get('developer_org', ''),
            created_at=row.get('created_at'),
            updated_at=row.get('updated_at'),
        )


@dataclass
class PluginReview:
    id: Optional[int] = None
    plugin_identifier: str = ''
    user_id: int = 0
    user_name: str = ''
    rating: int = 5
    content: str = ''
    version: str = ''
    is_active: bool = True
    reply_content: str = ''
    reply_at: Optional[str] = None
    created_at: Optional[str] = None
    flagged: bool = False          # 审计 P2-11 修复：风控标记（内容审核命中时置位）

    def to_dict(self) -> dict:
        d = asdict(self)
        d['is_active'] = int(self.is_active)
        d['flagged'] = int(self.flagged)
        return d

    @classmethod
    def from_row(cls, row: dict) -> 'PluginReview':
        return cls(
            id=row['id'],
            plugin_identifier=row['plugin_identifier'],
            user_id=row['user_id'],
            user_name=row.get('user_name', ''),
            rating=row['rating'],
            content=row.get('content', ''),
            version=row.get('version', ''),
            is_active=bool(row.get('is_active', 1)),
            reply_content=row.get('reply_content', ''),
            reply_at=row.get('reply_at'),
            created_at=row.get('created_at'),
            flagged=bool(row.get('flagged', 0)),
        )
