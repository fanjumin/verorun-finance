# Payment Gateway (payment)

## 概述

Payment Gateway（支付网关）是 VeroRun 的支付配置管理插件，提供支付宝、微信支付、Stripe、PayPal 四大支付渠道的配置管理功能。插件使用独立 PostgreSQL schema `payment`，完全独立于主库，支持从主库迁移已有配置。

## 功能特性

- **多渠道支付配置**：支持支付宝、微信支付、Stripe、PayPal 四大支付渠道的配置管理
- **市场自动检测**：根据站点域名自动区分 CN 市场（显示支付宝/微信）和 INTL 市场（显示 Stripe/PayPal）
- **独立数据隔离**：支付配置和日志存储在独立 PostgreSQL schema `payment`，与主库完全解耦
- **配置迁移**：支持从主库 `system_config` 迁移已有支付配置到独立 schema
- **支付日志**：记录所有支付配置变更和支付请求日志，便于审计
- **管理后台**：内置 `admin_payment.html` 管理界面，支持可视化配置各支付渠道

## 架构设计

### 数据库策略

使用**独立 PostgreSQL schema** `payment`，通过 `plugins/_base/db.py` 的 `get_raw_connection()` 连接（§9.1/§11.2），与主库完全解耦。支持 `migrate_from_main_db()` 从主库迁移已有配置到独立 schema，实现平滑过渡。

核心表包括：
- `payment_configs`：支付渠道配置（商户 ID、密钥、回调地址等）
- `payment_logs`：支付请求和配置变更日志

### 模块结构

```
payment/
├── __init__.py              # 插件入口，PaymentPlugin 类定义
├── models.py                # 数据模型，schema 初始化，迁移逻辑
├── services.py              # 核心服务层，委托 auth-center 支付逻辑
├── migrations/              # schema 迁移脚本目录（§10.6）
├── routes/
│   ├── __init__.py
│   └── admin.py             # 管理后台路由与蓝图
├── templates/
│   └── admin_payment.html   # 管理后台界面
└── i18n/
    ├── en.yml
    └── zh-CN.yml
```

## 目录结构

| 文件/目录 | 说明 |
|-----------|------|
| `__init__.py` | 插件入口，定义 `PaymentPlugin` 类，处理生命周期，提供支付相关对外接口 |
| `models.py` | 数据模型层，提供 `init_payment_tables()` 初始化 schema，`migrate_from_main_db()` 迁移逻辑 |
| `services.py` | 核心服务层，委托 auth-center 支付创建/确认/回调验证 |
| `migrations/` | schema 迁移脚本目录（§10.6） |
| `routes/admin.py` | 管理后台路由，提供 `payment_admin_bp` 蓝图 |
| `templates/admin_payment.html` | 管理后台页面 |
| `i18n/en.yml` | 英文翻译 |
| `i18n/zh-CN.yml` | 中文翻译 |

## 安装与启用

### 安装

插件已内置在 `plugins/payment/` 目录下。VeroRun 启动时会自动扫描并注册。

### 启用

插件默认启用。启用时执行：

1. 调用 `init_payment_tables()` 初始化独立 schema `payment`
2. 调用 `migrate_from_main_db()` 从主库迁移已有支付配置
3. 注册 Flask 蓝图路由 `/admin/payment/*`

## 配置说明

`plugin.json` 中插件级配置项为 `default_currency`（默认 `CNY`），各支付渠道凭证通过管理后台界面操作。

权限配置：

| 权限标识 | 说明 |
|----------|------|
| `api:read` | 读取支付配置与日志 |
| `api:write` | 写入支付配置 |
| `admin:access` | 访问管理后台支付页面 |

## API 端点

### 提供的 Hook 接口

| Hook 名称 | 功能描述 |
|-----------|----------|
| `payment/create` | 创建支付订单，发起支付请求 |
| `payment/confirm` | 确认支付结果，更新订单状态 |
| `payment/verify_notify` | 验证支付回调通知的签名和有效性 |

### 管理后台路由

通过 `payment_admin_bp` 蓝图注册，路由前缀为 `/admin/payment/`，提供：

- 支付渠道配置的 CRUD
- 支付日志查询
- 市场自动检测（CN/INTL）

## 依赖关系

### 事件监听

本插件不监听任何系统事件。

### 事件提供

本插件向事件总线提供 `payment/create`、`payment/confirm`、`payment/verify_notify` 三个 Hook，供订单系统和各支付渠道调用。

### 外部依赖

- **支付宝**：依赖支付宝开放平台 API
- **微信支付**：依赖微信支付 API V3
- **Stripe**：依赖 Stripe API
- **PayPal**：依赖 PayPal REST API
- 依赖 VeroRun 核心框架的 `BasePlugin`、i18n 模块

### 菜单集成

- **菜单组**：Business Center
- **菜单项**：Payment Config（图标：plugins）
- **管理地址**：`#payment`

## 许可证

作为 VeroRun 项目的一部分，遵循项目统一许可证。