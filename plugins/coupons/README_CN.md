# 智能优惠券引擎 (coupons)

## 概述

智能优惠券引擎（coupons）是 VeroRun 的智能营销插件，提供场景化优惠券管理、AI 智能推荐与订阅联动功能。插件数据存储于 VeroRun 共享 PostgreSQL 主库（通过统一数据库抽象层访问），由 `CouponEngine` 和 `AICouponRecommender` 两大核心引擎驱动。

| 属性     | 值                     |
|----------|------------------------|
| 标识     | `coupons`              |
| 版本     | 1.1.0                  |
| 分类     | shop                   |
| 图标     | coupons                |
| 数据库   | PostgreSQL（共享主库）  |
| 菜单组   | Business Center        |
| 菜单项   | `coupons_plugin`       |

## 功能特性

- **场景券管理**：支持按场景（如新用户、节日促销、会员日等）定义优惠券，自动匹配发放条件
- **AI 智能推荐**：`AICouponRecommender` 基于用户行为和购买历史，智能推荐最适合的优惠券
- **订阅联动**：支持与订阅系统联动，订阅用户可享受专属优惠券
- **优惠券验证**：提供 `coupon/validate` Hook，供购物车/结算流程实时校验优惠券有效性
- **优惠券核销**：提供 `coupon/apply` Hook，在订单确认时自动应用优惠券并计算折扣
- **管理后台**：内置 `admin_coupons.html` 管理界面，支持可视化管理优惠券

## 架构设计

### 数据库策略

使用**共享 PostgreSQL 主库**存储优惠券定义、发放记录、使用记录等核心数据（表：`coupons`、`coupon_redemptions`、`schema_meta`），同时**跨库只读**主库中的用户、订单、商品等数据，用于智能推荐和场景匹配。

### 模块结构

```
coupons/
├── __init__.py                # 插件入口，CouponPlugin 类定义，初始化引擎
├── models.py                  # 数据模型，数据库初始化与 CRUD 操作
├── routes.py                  # Flask 蓝图路由，管理后台与用户 API
├── engine.py                  # CouponEngine 核心引擎，优惠券逻辑处理
├── ai_recommender.py          # AICouponRecommender AI 推荐引擎
├── scene.py                   # 场景定义与匹配逻辑
├── templates/
│   ├── admin_coupons.html     # 管理后台界面
│   └── _ai_recommend.html     # AI 推荐模板片段
├── i18n/
│   ├── en.yml
│   └── zh-CN.yml
└── migrations/                # Schema 迁移 SQL
```

## 目录结构

| 文件/目录 | 说明 |
|-----------|------|
| `__init__.py` | 插件入口，定义 `CouponPlugin` 类，初始化 `CouponEngine` 和 `AICouponRecommender` 单例 |
| `models.py` | 数据模型层，提供 `init_db()` 初始化表结构，`get_db()` 和 `get_main_db()` 访问器 |
| `routes.py` | 路由层，提供 `coupon_bp` 蓝图，`init_routes()` 注入引擎依赖 |
| `engine.py` | 核心引擎，`CouponEngine` 类实现优惠券的校验、应用、统计等核心逻辑 |
| `ai_recommender.py` | AI 推荐引擎，`AICouponRecommender` 类实现基于用户行为的智能推荐 |
| `scene.py` | 场景模块，定义场景类型和匹配规则 |
| `templates/admin_coupons.html` | 管理后台页面 |
| `templates/_ai_recommend.html` | AI 推荐模板片段 |
| `i18n/en.yml` | 英文翻译 |
| `i18n/zh-CN.yml` | 中文翻译 |
| `migrations/` | 版本迁移 SQL 文件 |

## 安装与启用

### 安装

插件已内置在 `plugins/coupons/` 目录下。VeroRun 启动时会自动扫描并注册。

### 启用

插件默认启用（`enabled: true`）。启用时执行：

1. 调用 `init_db()` 在共享主库中创建表结构
2. 创建 `CouponEngine` 单例，注入数据库访问器和 i18n 翻译函数
3. 创建 `AICouponRecommender` 单例，关联引擎
4. 调用 `init_routes()` 将引擎注入到路由层
5. 注册 Flask 蓝图路由

## 配置说明

`plugin.json` 中无额外配置项（`config: {}`），所有配置通过管理后台界面操作。

权限配置：

| 权限标识 | 说明 |
|----------|------|
| `order.read` | 读取订单数据（用于校验和推荐） |
| `order.write` | 写入订单数据（用于应用优惠券） |
| `user.read` | 读取用户数据（用于场景匹配和推荐） |
| `admin:access` | 访问管理后台优惠券面板 |

## API 端点

### 提供的 Hook 接口

| Hook 名称 | 功能描述 |
|-----------|----------|
| `coupon/validate` | 校验优惠券是否有效（检查有效期、使用条件、库存等） |
| `coupon/apply` | 应用优惠券到订单，计算折扣金额并标记使用 |

### 管理后台路由

通过 `coupon_bp` 蓝图注册，提供优惠券 CRUD、发放记录、使用统计等管理 API。

### 用户 API

- `POST /api/coupons/validate` -- 校验优惠码
- `POST /api/coupons/apply` -- 应用到当前购物车
- `GET  /api/coupons/recommend` -- 获取 AI 推荐优惠券

## 依赖关系

### 外部依赖

- 依赖 VeroRun 核心框架的 `BasePlugin`、事件总线、i18n 模块
- 数据存储依赖共享 PostgreSQL 主库（统一数据库抽象层）
- 无外部第三方服务依赖

### 菜单集成

- **菜单组**：Business Center
- **菜单项**：Coupon Management（图标：coupons）
- **管理地址**：`#coupons_plugin`

## 许可证

作为 VeroRun 项目的一部分，遵循项目统一许可证。
