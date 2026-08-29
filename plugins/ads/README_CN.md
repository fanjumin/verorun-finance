# Ad Management (ads)

## 概述

Ad Management（广告管理）是 VeroRun 的广告投放管理插件，负责广告位管理、区域（zones）管理、广告展示/点击统计，并提供 AI-Ready 的 API 接口。插件数据存储于主库 PostgreSQL 的独立 `ads` schema（四个表：`ad_placements`、`ad_zones`、`ad_stats`、`ad_clicks`），通过插件框架管理生命周期。

| 属性 | 值 |
|------|-----|
| 标识 | `ads` |
| 版本 | 1.2.0 |
| 数据库 | PostgreSQL，schema `ads` |
| 菜单组 | AI & Content |
| 菜单键 | `ads` |

## 功能特性

- **广告位管理**：支持创建、编辑、删除广告位，按区域（zones）分类管理，支持尺寸、位置、定向规则、排期、权重、频次上限
- **区域管理**：区域可分组广告位，删除区域前自动检查引用（有广告引用时拒绝删除）
- **展示/点击统计**：累计计数 + 每日统计（`ad_stats`）+ 点击明细采样（`ad_clicks`）
- **AI-Ready API**：`ai_tools.py` 提供标准化接口（增删改查、统计、效果分析、代码片段生成），供 AI 代理调用
- **多站点支持**：`site_key` 结合 `site_domains` 实现多租户投放
- **管理后台**：内置管理界面 `admin_ads.html`，支持广告、区域、统计、设置四个面板
- **前端渲染组件**：`render_ads.html` 宏 + `ads.js` 异步渲染，支持图片与代码类广告
- **国际化**：`i18n/en.yml`、`i18n/zh-CN.yml`

## 架构设计

### 数据库策略

使用主库 PostgreSQL 的独立 **`ads` schema**（与主库解耦，仅共享连接配置）。连接通过平台统一工厂 `plugins/_base/db.get_raw_connection()` 创建，按**线程隔离**并带存活探活（兼容 gunicorn pre-fork）。

| 表 | 说明 |
|----|------|
| `ad_placements` | 广告位（名称、站点、区域、位置、页面、类型、素材、定向、排期、权重、频次、计数） |
| `ad_zones` | 广告区域（站点、标识、尺寸、状态） |
| `ad_stats` | 每个广告每日展示/点击（`(ad_id, stat_date)` 唯一） |
| `ad_clicks` | 点击明细采样（哈希 IP、User-Agent、Referer），用于刷量排查 |

### 模块结构

```
plugins/ads/
├── __init__.py          # 插件入口，AdsPlugin 类定义，生命周期管理
├── models.py            # 数据模型：连接管理、schema 初始化、共享 CRUD、统计
├── routes.py            # Flask 蓝图路由：管理后台与公开 API
├── ai_tools.py          # AI 工具函数，暴露给 AI 代理的标准接口
├── plugin.json          # 插件元数据
├── README.en.md         # 英文文档
├── README_CN.md         # 中文文档
├── templates/
│   ├── admin_ads.html   # 管理后台界面模板（内联 JS）
│   └── render_ads.html  # 前端广告渲染宏
├── static/
│   └── ads.js           # 前端广告渲染与统计脚本
└── i18n/
    ├── en.yml           # 英文翻译
    └── zh-CN.yml        # 中文翻译
```

## 安装与启用

### 安装

插件已内置在 `plugins/ads/` 目录下。VeroRun 启动时会自动扫描并注册插件。

### 启用

插件默认启用（`enabled: true`）。启用时执行以下步骤：

1. 调用 `init_ad_db()` 初始化 `ads` schema 与表（幂等，`IF NOT EXISTS` + 动态加列，支持平滑升级）
2. 注入 i18n 翻译函数
3. 注册 Flask 蓝图路由

数据库连接使用平台统一 PG 环境变量（`PG_HOST`/`PG_PORT`/`PG_DB`/`PG_USER`/`PG_PASSWORD`）。

### 手动控制

在管理后台的插件管理页面，可以手动启用/禁用此插件。禁用后广告位将不再渲染，但数据库和数据保留。

## 配置说明

插件配置（可通过管理后台"统计 > 设置"面板或 `plugin.json` 修改）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `default_width` | integer | 320 | 默认广告宽度（像素） |
| `default_height` | integer | 0 | 默认广告高度（0 = 自适应） |
| `max_placements` | integer | 50 | 最大广告位数量限制 |

## API 端点

### 提供的 Hook 接口

| Hook 名称 | 功能描述 |
|-----------|----------|
| `ads/get_placements` | 获取广告位列表（经 AI 工具） |
| `ads/render_ad` | 渲染指定广告位 HTML |
| `ads/get_stats` | 获取展示/点击统计 |
| `ads/record_impression` | 记录一次展示事件 |
| `ads/record_click` | 记录一次点击事件 |

### 管理后台路由（需管理员鉴权 `_require_admin`）

- `GET    /admin/ads/`          -- 广告位列表（分页）
- `POST   /admin/ads/`          -- 创建广告位
- `PUT    /admin/ads/<id>`      -- 更新广告位（动态字段）
- `DELETE /admin/ads/<id>`      -- 删除广告位（级联清理统计与点击明细）
- `GET|POST /admin/ads/zones`   -- 区域列表 / 创建
- `PUT|DELETE /admin/ads/zones/<id>` -- 更新 / 删除区域（有引用时禁止删除）
- `GET    /admin/ads/api/v1/stats` -- 统计查询
- `GET|POST /admin/ads/settings`   -- 插件设置

### 公开路由

- `GET  /admin/ads/api/v1/ads?page=&position=&site_key=&zone_id=&limit=` -- 前端渲染广告（limit 默认 50，上限 200）
- `POST /admin/ads/api/v1/stats/impression` -- 展示上报（限流：60 次/分/IP）
- `POST /admin/ads/api/v1/stats/click`      -- 点击上报（限流：30 次/分/IP）

## 依赖关系

- 依赖 VeroRun 核心框架的 `BasePlugin`、`i18n`、模板引擎
- 依赖 `psycopg2`（平台统一依赖）
- 共享数据库工厂 `plugins/_base/db.get_raw_connection()`

### 菜单集成

- **菜单组**：AI & Content
- **菜单项**：Ad Management

## 隐私说明

- 点击明细中的客户端 IP 以 **SHA-256 哈希** 形式存储（非明文），降低 PII 暴露风险，同时保留同源刷量识别能力
- 若服务欧盟用户，请在隐私政策中声明广告点击数据收集行为，并提供数据删除机制（GDPR）

## 许可证

作为 VeroRun 项目的一部分，遵循项目统一许可证。
