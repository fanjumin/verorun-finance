# Currency Converter (currency_converter)

## 概述

Currency Converter（多币种转换）是 VeroRun 的国际化价格展示插件，接管系统价格展示层，根据用户偏好或 GeoIP 自动检测，将价格实时换算为本地货币显示。插件使用 PostgreSQL 独立 schema（`currency_converter`）存储数据，当前版本 **v1.1.0**，由管理员在后台启用。

## 功能特性

- **多币种展示**：支持 15 种主流货币（CNY、USD、EUR、JPY、GBP、HKD、KRW、AUD、CAD、SGD、THB、MYR、PHP、IDR、VND），可配置启用范围
- **实时汇率**：从 `frankfurter.app` 和 `open.er-api.com` 双 API 源获取汇率，支持自动降级，定时刷新
- **用户偏好**：登录用户可设置首选货币，持久化存储偏好
- **GeoIP 自动检测**：根据访客 IP 地理位置自动推荐本地货币，新访客无需手动选择
- **前端组件**：`currency_widget.js` 提供前端货币切换组件，用户可随时切换显示币种
- **定时同步**：`scheduler.py` 定时从外部 API 同步最新汇率，可配置刷新间隔

## 架构设计

### 数据库策略

使用 **PostgreSQL 独立 schema**（`currency_converter`，单库多 Schema 隔离架构，插件标准 §9.1），存储汇率缓存、用户偏好等数据。插件完全独立于主库，仅在用户偏好查询时按需读取主库用户信息。

### 模块结构

```
currency_converter/
├── __init__.py              # 插件入口，CurrencyConverterPlugin 类定义
├── models.py                # 数据模型，数据库初始化，汇率缓存表
├── routes.py                # Flask 蓝图路由，管理后台 API
├── services.py              # 核心服务层，汇率转换、API 同步、货币格式化
├── scheduler.py             # 定时任务，定期刷新汇率数据
├── templates/
│   └── admin_currency.html  # 管理后台界面
├── static/
│   └── currency_widget.js   # 前端货币切换组件
└── i18n/
    ├── en.yml
    └── zh-CN.yml
```

## 目录结构

| 文件/目录 | 说明 |
|-----------|------|
| `__init__.py` | 插件入口，定义 `CurrencyConverterPlugin` 类，处理生命周期 |
| `models.py` | 数据模型，提供 `init_db()` 初始化数据库，汇率缓存和用户偏好表 |
| `routes.py` | 路由层，提供管理后台 API 和用户偏好 API |
| `services.py` | 核心服务，汇率转换逻辑、API 数据获取、价格格式化 |
| `scheduler.py` | 定时调度器，定期从外部 API 同步汇率 |
| `templates/admin_currency.html` | 管理后台页面 |
| `static/currency_widget.js` | 前端货币切换组件脚本 |
| `i18n/en.yml` | 英文翻译 |
| `i18n/zh-CN.yml` | 中文翻译 |

## 安装与启用

### 安装

插件已内置在 `plugins/currency_converter/` 目录下。VeroRun 启动时会自动扫描并注册。

### 启用

插件默认未启用，需在管理后台手动启用。启用时执行：

1. 调用 `init_db()` 初始化 PostgreSQL 独立 schema（`currency_converter`）
2. 配置服务层（`services.configure()`），设置基础货币、缓存 TTL 等
3. 从数据库加载已有汇率缓存
4. 异步执行首次汇率同步（`sync_rates()`）
5. 注册 Flask 蓝图路由

## 配置说明

`plugin.json` 中的配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `primary_api` | string | `https://api.frankfurter.app/latest` | 主汇率 API 地址 |
| `fallback_api` | string | `https://open.er-api.com/v6/latest` | 备用汇率 API 地址 |
| `base_currency` | string | `CNY` | 系统基础货币（数据库中存储的货币） |
| `refresh_interval_minutes` | integer | 60 | 汇率刷新间隔（分钟，最小 15） |
| `cache_ttl_minutes` | integer | 60 | 汇率缓存有效期（分钟） |
| `default_currency` | string | `CNY` | 新访客默认显示货币 |
| `enable_geoip` | boolean | true | 是否启用 GeoIP 自动检测 |
| `enabled_currencies` | array | 15 种货币 | 启用货币列表 |

权限配置：

| 权限标识 | 说明 |
|----------|------|
| `api:read` | 读取业务数据 |
| `user:profile` | 读取/写入用户偏好设置 |
| `network:request` | 发起外部网络请求（汇率 API） |

### Dashboard 统计声明

插件通过 `plugin.json` 的 `dashboard.stats` 声明以下统计指标（插件标准 §2.3），由管理后台 Dashboard 聚合渲染：

| 指标键 | 标题 | 类型 | 说明 |
|--------|------|------|------|
| `currency_rates` | Currency Rates | counter | 已同步的汇率币种数 |
| `last_sync` | Last Sync | gauge | 最近一次同步的 Unix 时间戳（秒） |

## API 端点

### 提供的 Hook 接口

| Hook 名称 | 功能描述 |
|-----------|----------|
| `currency/convert` | 将指定金额从一种货币转换为另一种 |
| `currency/rates` | 获取当前所有可用汇率 |
| `currency/preference` | 获取或设置用户的货币偏好 |

### 管理后台路由

提供管理后台 API，支持汇率管理、货币配置等操作。管理后台嵌入地址：`/admin/currency/`

## 依赖关系

### 事件监听

| 事件名称 | 处理逻辑 |
|----------|----------|
| `user/login` | 用户登录时，读取其货币偏好并应用到当前会话 |
| `user/registered` | 新用户注册时，根据 GeoIP 设置初始货币偏好 |

### 事件提供

本插件向事件总线提供 `currency/convert`、`currency/rates`、`currency/preference` 三个 Hook，供价格展示层和其他插件调用。

### 外部依赖

- **Frankfurter API**（`api.frankfurter.app`）：主汇率数据源，免费开源汇率 API
- **Exchange Rate API**（`open.er-api.com`）：备用汇率数据源，自动降级切换
- 依赖 VeroRun 核心框架的 `BasePlugin`、事件总线、i18n 模块

### 菜单集成

- **菜单组**：Business Center
- **菜单项**：Currency Settings（图标：currency）
- **嵌入地址**：`/admin/currency/`

## 许可证

作为 VeroRun 项目的一部分，遵循项目统一许可证。