# Subscription（subscription）

## 概述

Subscription（统一按需订阅管理）是 VeroRun 的核心订阅计费插件，采用按 Feature/SKU 独立订阅模式。支持双环境支付路由（CN 环境使用支付宝/微信支付，INTL 环境使用 Stripe/PayPal），实现按需付费的灵活计费体系。

| 属性      | 值                                |
|-----------|-----------------------------------|
| 标识符    | `subscription`                    |
| 版本      | 1.1.0                             |
| 数据库    | 主库 `subscription` schema（PostgreSQL） |
| 菜单分组  | Business Center（管理后台）       |

## 功能特性

- **按 Feature/SKU 独立订阅**：每个功能或 SKU 独立计费，用户可按需订阅，无需购买固定套餐
- **双环境支付路由**：自动根据 `DEPLOY_MARKET` 环境变量路由支付渠道（CN → 支付宝/微信支付；INTL → Stripe/PayPal）
- **试用期支持**：可配置免费试用天数（`trial_days`）
- **宽限期管理**：订阅到期后有可配置的宽限期（`grace_days`），避免服务立即中断
- **自动续费**：支持默认开启自动续费（`auto_renew_default`）
- **订阅全生命周期管理**：提供 `subscribe`、`cancel`、`renew` 全流程 Hook
- **定时任务**：通过 `register_jobs()` 注册到期检查与自动续费重试任务
- **回调并发安全**：支付回调使用行级锁（`SELECT ... FOR UPDATE`）防止重复处理
- **网关配置兜底**：网关密钥优先读环境变量，缺失时回退到 `system_config` 表
- **全量 i18n**：用户门户与管理后台均支持中英双语

## 架构设计

### 数据库策略

使用**主库独立 schema**（PostgreSQL，`subscription` schema），通过 `plugins/_base/db.py` 的统一连接工厂 `get_raw_connection()` 连接，存储订阅记录、SKU 目录、支付订单等核心数据。订阅状态查询时可跨 schema 读取主库用户信息。

### 模块结构

```
subscription/
├── __init__.py                # 插件入口，SubscriptionPlugin 类定义
├── models.py                  # 数据模型，DDL 初始化，种子数据
├── routes.py                  # Flask 蓝图路由，订阅页面与 API
├── services.py                # 核心服务层，订阅业务逻辑
├── scheduler.py               # 定时调度器，到期检查与自动续费重试
├── gateways/
│   ├── __init__.py            # 支付渠道路由与公共工具（含 system_config 兜底）
│   ├── alipay.py              # 支付宝支付网关
│   ├── wechat.py              # 微信支付网关
│   ├── stripe.py              # Stripe 支付网关
│   └── paypal.py              # PayPal 支付网关
├── templates/
│   ├── subscribe.html         # 用户订阅页面
│   └── subscribe_admin.html   # 管理后台页面
├── i18n/
│   ├── en.yml                 # 英文翻译
│   └── zh-CN.yml              # 中文翻译
├── migrations/                # 数据库迁移预留目录
├── screenshots/               # 商店页截图
├── README.en.md               # 英文说明
└── README_CN.md               # 中文说明
```

## 目录结构

| 文件/目录 | 说明 |
|-----------|------|
| `__init__.py` | 插件入口，定义 `SubscriptionPlugin` 类，处理生命周期 |
| `models.py` | 数据模型层，提供 `init_tables()` 初始化 schema，`seed_default_items()` 填充种子数据 |
| `routes.py` | 路由层，提供订阅页面和 API 端点 |
| `services.py` | 核心服务层，`SubscriptionService` 实现订阅的创建、查询、取消、续费等逻辑 |
| `scheduler.py` | 定时调度器，通过 `register_jobs()` 注册到期检查与自动续费重试 |
| `gateways/__init__.py` | 支付渠道路由、占位符检测、`system_config` 配置兜底 |
| `gateways/alipay.py` | 支付宝支付网关实现 |
| `gateways/wechat.py` | 微信支付网关实现 |
| `gateways/stripe.py` | Stripe 支付网关实现 |
| `gateways/paypal.py` | PayPal 支付网关实现 |
| `templates/subscribe.html` | 用户订阅页面模板 |
| `templates/subscribe_admin.html` | 管理后台页面模板 |
| `i18n/en.yml` | 英文翻译 |
| `i18n/zh-CN.yml` | 中文翻译 |
| `migrations/` | 数据库迁移预留目录 |
| `screenshots/` | 商店详情页截图 |

## 安装与启用

插件已内置在 `plugins/subscription/` 目录下。VeroRun 启动时会自动扫描并注册。

插件默认启用（`enabled: true`）。启用时执行：

1. 调用 `init_tables()` 初始化 `subscription` schema 与数据表
2. 调用 `seed_default_items()` 填充默认 SKU 目录
3. 初始化插件 i18n 翻译函数（注入到 routes 和 services 模块）
4. 通过 `register_jobs()` 注册定时任务（到期检查、自动续费重试）
5. 注册 Flask 蓝图路由

## 配置说明

`plugin.json` 中的配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `trial_days` | integer | 0 | 免费试用天数（0 = 无试用期） |
| `grace_days` | integer | 3 | 到期后宽限期天数（宽限期内不禁用服务） |
| `auto_renew_default` | boolean | true | 新订阅默认是否开启自动续费 |

权限配置：

| 权限标识 | 说明 |
|----------|------|
| `api:read` | 读取订阅状态和 SKU 信息 |
| `api:write` | 创建/修改/取消订阅 |
| `admin:access` | 管理 SKU 目录和订阅配置 |

## API 端点

### 提供的 Hook 接口

| Hook 名称 | 功能描述 |
|-----------|----------|
| `subscription/has` | 检查用户是否拥有指定 Feature/SKU 的有效订阅 |
| `subscription/list` | 获取用户的所有订阅列表 |
| `subscription/subscribe` | 为用户创建新的订阅 |
| `subscription/cancel` | 取消用户的订阅 |
| `subscription/renew` | 续费用户的订阅 |

### 事件监听

本插件不监听外部事件，所有生命周期行为由提供的 Hook 与定时任务驱动。

### 管理后台路由

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/plugin/subscription/admin/` | 订阅管理后台页面 |
| GET/POST | `/plugin/subscription/admin/items` | SKU 目录管理 |
| DELETE | `/plugin/subscription/admin/items/<item_key>` | 删除 SKU |
| GET | `/plugin/subscription/admin/users` | 用户订阅列表 |
| GET | `/plugin/subscription/admin/orders` | 订单列表 |
| POST | `/plugin/subscription/admin/orders/<order_no>/refund` | 订单退款 |

### 用户端路由

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/plugin/subscription/portal` | 用户订阅门户页面 |
| GET | `/plugin/subscription/api/items` | 获取可订阅 SKU 列表 |
| GET | `/plugin/subscription/api/my` | 获取当前用户订阅 |
| GET | `/plugin/subscription/api/check/<item_key>` | 检查功能权限 |
| POST | `/plugin/subscription/api/subscribe` | 订阅 |
| POST | `/plugin/subscription/api/cancel` | 取消订阅 |
| POST | `/plugin/subscription/api/renew` | 续费 |
| GET | `/plugin/subscription/api/orders` | 获取当前用户订单 |
| POST | `/plugin/subscription/api/notify/{alipay,wechat,stripe,paypal}` | 支付网关回调 |

### 定时任务

通过 `register_jobs()` 注册：

1. **到期检查**：定期扫描并处理到期订阅
2. **自动续费重试**：对欠费/到期订阅重试创建续费订单

## 依赖关系

### 外部依赖

- **支付宝**：CN 环境支付网关，依赖支付宝开放平台 API
- **微信支付**：CN 环境支付网关，依赖微信支付 API V3
- **Stripe**：INTL 环境支付网关，依赖 Stripe API
- **PayPal**：INTL 环境支付网关，依赖 PayPal REST API
- 依赖 VeroRun 核心框架的 `BasePlugin`、事件总线、i18n 模块
- PostgreSQL 主库连接（`plugins/_base/db.py`）

### 菜单集成

- 菜单分组：Business Center
- 管理入口：`/plugin/subscription/admin/`（通过 `menu` 字段注册到管理后台）

## 许可证

作为 VeroRun 项目的一部分，遵循项目统一许可证。
