# Analytics (analytics)

## 概述

Analytics 是 VeroRun 的服务端无 Cookie 分析中间件插件，提供完整的网站访问数据采集、存储、聚合与可视化能力。插件通过无 Cookie 的轻量级追踪方式，在不依赖客户端 Cookie 的前提下实现 PV/UV 统计、访问者会话识别、页面级行为分析、地理位置解析以及趋势分析等功能。

版本：**1.7.0**

## 功能特性

- **无 Cookie 追踪**：基于服务端指纹（IP + User-Agent 组合哈希）实现访问者识别，无需依赖客户端 Cookie，符合隐私合规要求
- **PV/UV 统计**：精确记录页面浏览量（Page View）和独立访客数（Unique Visitor）
- **访问者会话管理**：基于时间窗口自动识别和合并访问者会话
- **页面级统计**：按页面路径、来源、设备类型等维度进行细粒度统计分析
- **地理位置解析**：集成 ip2region 库，通过 IP 地址解析访问者地理位置（国家/省份/城市），支持本地离线上传 .xdb（中国区友好）
- **用户代理解析**：内置 UA 解析器，识别浏览器、操作系统、设备类型
- **趋势分析**：提供按时间维度（小时/天/周/月）的访问趋势数据
- **实时仪表盘**：通过后台管理面板嵌入展示实时和历史的分析数据
- **后台聚合**：独立的聚合线程每 60 秒自动运行，将原始追踪数据聚合为统计指标
- **Workflow 集成**：通过 workflow_nodes 模块支持在工作流中调用分析数据

## 架构设计

### 数据库策略

插件使用**独立数据库**，使用 PostgreSQL 的 `analytics` schema 进行数据存储。

### 模块结构

```
┌─────────────────────────────────────────────────┐
│                  middleware.py                    │
│           (请求拦截与原始数据采集)                  │
└─────────────────────┬───────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────┐
│                   tracker.py                     │
│              (行为追踪与事件记录)                   │
└─────────────────────┬───────────────────────────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
┌──────────────┐ ┌──────────┐ ┌──────────────┐
│  ua_parser   │ │  geoip   │ │  models.py   │
│  (UA 解析)    │ │ (IP 定位) │ │  (11张分析表) │
└──────────────┘ └──────────┘ └──────┬───────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────┐
│                 processor.py                     │
│            (后台聚合线程 / 每60秒运行)              │
└─────────────────────┬───────────────────────────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
┌──────────────┐ ┌──────────┐ ┌──────────────┐
│  dashboard   │ │   cli    │ │ workflow_    │
│  (仪表盘注入) │ │ (命令行)  │ │ nodes (工作流) │
└──────────────┘ └──────────┘ └──────────────┘
```

### 11 张分析表

插件在 PostgreSQL `analytics` schema 中维护以下数据表：

| 表名 | 用途 |
|------|------|
| `analytics_logs` | 原始访问日志 |
| `analytics_visitor_sessions` | 访客会话聚合 |
| `analytics_hourly_stats` | 按小时聚合统计 |
| `analytics_daily_stats` | 按天聚合统计 |
| `analytics_page_stats` | 页面维度统计 |
| `analytics_source_stats` | 来源维度统计 |
| `analytics_geo_stats` | 地理位置统计 |
| `analytics_device_stats` | 设备/浏览器/OS 统计 |
| `analytics_events` | 自定义事件记录 |
| `analytics_alerts` | 告警记录 |
| `analytics_privacy_config` | 隐私配置 |

## 目录结构

```
analytics/
├── __init__.py              # 插件入口，注册 Hook 与中间件
├── models.py                # 11 张分析数据表的 ORM 模型定义
├── middleware.py             # 服务端无 Cookie 分析中间件
├── processor.py             # 后台聚合处理线程（每 60 秒运行）
├── tracker.py               # 事件追踪器，记录原始行为数据
├── geoip.py                 # IP 地理位置解析（基于 ip2region）
├── ua_parser.py             # User-Agent 解析器
├── routes.py                # 仪表盘 Blueprint 路由（register_routes）
├── cli.py                   # 命令行工具（CLI 命令）
├── workflow_nodes.py        # Workflow 引擎集成节点
├── migrate_analytics.py     # 数据库迁移脚本
├── plugin.json              # 插件元数据配置
├── data/
│   └── ip2region_v4.xdb     # ip2region IP 地理位置数据库
├── ip2region/
│   ├── __init__.py
│   ├── searcher.py          # ip2region 查询引擎
│   └── util.py              # ip2region 工具函数
├── i18n/
│   ├── en.yml               # 英文国际化
│   └── zh-CN.yml            # 中文国际化
├── migrations/
│   └── 001_initial.sql      # 数据库版本迁移（初始 schema）
├── static/
│   ├── js/                  # 前端本地化依赖（echarts/chart.js/tsparticles）与仪表盘 JS
│   ├── china.json           # 中国地图数据
│   └── world.json           # 世界地图数据
└── templates/
    └── analytics.html       # 管理后台仪表盘模板
```

## 安装与启用

### 安装

插件已包含在 VeroRun 的默认插件目录中，无需额外安装步骤。

### 启用

1. 确保 PostgreSQL 数据库中存在 `analytics` schema
2. 运行数据库迁移脚本：

```bash
python -m plugins.analytics.migrate_analytics
```

3. 在 VeroRun 管理后台 "插件管理" 页面中启用 Analytics 插件
4. 中间件将在启用后自动开始拦截请求并采集数据

### 本地开发

本地开发时，插件会自动使用 SQLite 数据库 `data/analytics.db`。无需额外配置即可运行。

## 配置说明

在 `plugin.json` 中配置以下参数：

```json
{
  "name": "analytics",
  "version": "1.7.0",
  "database": {
    "type": "postgresql",
    "schema": "analytics"
  },
  "aggregation": {
    "interval_seconds": 60
  },
  "middleware": {
    "enabled": true,
    "exclude_paths": ["/admin/*", "/static/*", "/api/health"]
  },
  "ip2region": {
    "db_path": "data/ip2region_v4.xdb"
  }
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `database.schema` | PostgreSQL schema 名称 | `analytics` |
| `aggregation.interval_seconds` | 聚合线程运行间隔（秒） | `60` |
| `middleware.enabled` | 是否启用中间件 | `true` |
| `middleware.exclude_paths` | 排除的路径模式列表 | 管理后台与静态资源 |
| `ip2region.db_path` | ip2region 数据库文件路径 | `data/ip2region_v4.xdb` |

## API 端点

### Hook 提供

| Hook 标识符 | 类型 | 说明 |
|-------------|------|------|
| `analytics/track_event` | Hook | 手动记录自定义分析事件 |
| `analytics/get_realtime` | Hook | 获取实时分析数据（当前在线人数、今日 PV/UV） |
| `analytics/get_trend` | Hook | 获取指定时间范围的分析趋势数据 |

### 管理后台

| 路径 | 说明 |
|------|------|
| `/admin/analytics/` | 分析仪表盘（嵌入页面） |

### Filter 注册

| Filter 标识符 | 说明 |
|---------------|------|
| `dashboard.data` | 模块级注册，向管理后台仪表盘注入分析数据摘要 |

## 依赖关系

### 内部依赖

- VeroRun 核心框架：中间件注册、Hook 系统、事件总线
- 管理后台（auth-center）：仪表盘嵌入与菜单渲染

### 外部依赖

- **ip2region**：IP 地理位置解析库，使用 `data/ip2region_v4.xdb` 离线数据库
- **PostgreSQL**：生产环境数据存储（`analytics` schema）

### 被依赖

- **health_check** 插件：可通过 `analytics/get_trend` Hook 获取访问趋势进行健康分析
- **Workflow 引擎**：通过 `workflow_nodes.py` 在工作流中调用分析数据

### 菜单

- **菜单组**：`Monitoring & Data`
- **嵌入 URL**：`/admin/analytics/`

## 更新日志

### v1.7.0 (2026-08-18)

**系统风格集成**

- **原生嵌入管理后台**：Analytics 仪表盘不再加载独立 iframe，`l_analytics()` 直接将纯 HTML 片段注入主内容区，完整继承系统组件样式（`.cd/.st/.ai-tab-bar/.ai-tab/.btn/.in/.sl/.ta/.bdg/.modal-box/.toast`）。
- **移除独立风格**：删除自绘霓虹主题（粒子、扫描线、硬编码色值），仪表盘全程使用系统 CSS 变量。
- **前端健壮性**：事件绑定统一收拢到 `initAnalyticsDashboard()`（每次进入可安全重入），toast 改名 `analyticsToast` 避免污染后台全局作用域；独立完整页仍作为兜底可用。

### v1.6.2 (2026-08-18)

**稳定性与体验修复**

- **PG schema 自愈**：插件启动/首次使用时对 11 张表幂等补齐缺失列（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），修复旧版本服务器升级后仪表盘 API 全部 500 的问题。
- **JSON 错误响应**：仪表盘/API 异常统一返回 JSON 并记录到 `data/logs/plugins/analytics.log`，不再返回导致前端 JSON 解析失败的 HTML 500 页面。
- **前端健壮性**：`api()` 增加非 JSON / HTTP 错误兜底，并回退使用父页面 JWT token（修复 srcdoc iframe 首帧 401）。
- **ip2region 离线上传**：设置 → GeoIP 新增 "Upload .xdb" 按钮，支持本地导入 ip2region 数据库（中国区友好，无需访问 GitHub/Gitee）。
- **样式对齐系统**：仪表盘改用系统 design-system 变量，移除自绘粒子/扫描线霓虹装饰。

### v1.5.2 (2026-08-06)

**Bug 修复：仪表盘自轮询被计为 PV**

仪表盘自身的 `/admin/analytics/api/v1/*` 轮询接口会被记录为页面浏览（仪表盘打开时约每小时 1300 次），拉高 PV/会话数。

- 默认 `exclude_paths` 增加 `/admin/analytics/*`，仪表盘自身 API 流量不再被统计。
- 若数据库已有隐私配置行，需手动更新 `analytics_privacy_config.exclude_paths` 并重启服务（重新初始化不会覆盖已有行）。

### v1.5.1 (2026-08-06)

**Bug 修复：聚合数据虚高**

修复了一个关键 bug：后台聚合线程每 60 秒会把整个当前小时的统计重复叠加一次，导致 PV、会话数等指标被放大约 50~60 倍。

- `upsert_hourly` / `upsert_page_stat` / `upsert_source` / `upsert_geo` / `upsert_device` 由**累加语义**改为**覆盖语义**：每次运行全量重算整个统计窗口，重复运行收敛到真实值。
- 页面/来源/地理/设备统计改为在**日级聚合**中全量重算（整日重算 + 覆盖），不再在小时级重复累加。
- `AnalyticsProcessor._aggregate_daily()` 增加可选 `date_str` 参数，支持历史日期重算。

> **升级提示**：v1.5.1 之前积累的统计为虚高数据。升级后请清空聚合表（`analytics_hourly_stats`、`analytics_daily_stats`、`analytics_page_stats`、`analytics_source_stats`、`analytics_geo_stats`、`analytics_device_stats`），从零开始积累真实数据。原始日志保留不受影响。

## 许可证

本插件为 VeroRun 项目的一部分，遵循 VeroRun 项目的整体许可证协议。