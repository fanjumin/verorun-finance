# 1688 供应链采集 (ali_api)

## 概述

1688 供应链采集插件（ali_api）是 VeroRun 的 1688（阿里巴巴中国站）商品数据采集与供应链管理插件，提供商品搜索、AI 智能优化、本地商城一键发布等功能。版本 2.1.0，使用独立数据库 `ali_api.db`。

## 功能特性

- **1688 商品搜索**：对接 1688 Open API，支持关键词搜索、分类浏览、商品详情获取
- **AI 智能优化**：利用 AI 自动优化商品标题、描述、主图，提升本地商城转化率
- **本地商城发布**：将 1688 货源商品一键发布到 VeroRun 商城，自动填充商品信息
- **以图搜货**：通过 `image_search_service.py` 实现图片搜索，上传图片即可找到同款货源
- **智能缓存**：`cache_service.py` 提供多级缓存，减少 API 调用次数，提升响应速度
- **频率限制**：`rate_limiter.py` 实现 API 调用频率控制，防止触发 1688 平台限流
- **评价分析**：`review_service.py` 分析 1688 商品评价，辅助选品决策
- **自动采购单**：监听 `ORDER_PAID` 事件，自动为涉及 1688 货源的订单创建采购单草稿

## 架构设计

### 数据库策略

使用**独立数据库** `ali_api.db`（SQLite），完全与主库解耦。同时**跨库读取**主库中的 `order_items`、`products`、`ali_api_items` 等表实现业务联动。

### 模块结构

```
ali_api/
├── __init__.py                   # 插件入口，AliApiPlugin 类定义
├── models.py                     # 数据模型，AliPurchaseOrder 等
├── config.py                     # 配置管理，API 网关等
├── plugin_i18n.py                # i18n 桥接模块
├── routes/
│   ├── __init__.py
│   └── admin.py                  # 管理后台路由与蓝图
├── services/
│   ├── __init__.py
│   ├── alibaba_client.py         # 1688 API 客户端（v1）
│   ├── alibaba_client_v2.py      # 1688 API 客户端（v2，升级版）
│   ├── ai_processor.py           # AI 商品信息优化处理器
│   ├── image_search_service.py   # 以图搜货服务
│   ├── cache_service.py          # 多级缓存服务
│   ├── rate_limiter.py           # API 频率限制器
│   └── review_service.py         # 评价分析服务
├── templates/
│   └── ali_admin/
│       └── index.html            # 管理后台界面
├── static/
│   └── ali_console.js            # 管理后台前端脚本
└── i18n/
    ├── en.yml
    └── zh-CN.yml
```

## 目录结构

| 文件/目录 | 说明 |
|-----------|------|
| `__init__.py` | 插件入口，定义 `AliApiPlugin` 类，处理生命周期与 ORDER_PAID 事件监听 |
| `models.py` | 数据模型，`AliPurchaseOrder` 采购单 CRUD，数据库初始化 |
| `config.py` | 配置管理，读取 API 网关地址等设定 |
| `plugin_i18n.py` | i18n 桥接，将插件翻译函数注入到服务层 |
| `routes/admin.py` | 管理后台路由，提供 `ali_admin_bp` 蓝图 |
| `services/alibaba_client.py` | 1688 Open API 客户端（v1），封装搜索、详情等接口 |
| `services/alibaba_client_v2.py` | 1688 API 客户端（v2），增强版接口封装 |
| `services/ai_processor.py` | AI 处理器，自动优化商品标题/描述/主图 |
| `services/image_search_service.py` | 以图搜货服务，上传图片匹配 1688 商品 |
| `services/cache_service.py` | 缓存服务，减少重复 API 调用 |
| `services/rate_limiter.py` | 频率限制器，控制 API 调用速率 |
| `services/review_service.py` | 评价分析服务，分析商品评价数据 |
| `templates/ali_admin/index.html` | 管理后台界面 |
| `static/ali_console.js` | 管理后台前端脚本 |
| `i18n/en.yml` | 英文翻译 |
| `i18n/zh-CN.yml` | 中文翻译 |
| `ali_api.db` | 独立 SQLite 数据库文件 |

## 安装与启用

### 安装

插件已内置在 `plugins/ali_api/` 目录下。VeroRun 启动时会自动扫描并注册。

### 启用

插件默认启用（`enabled: true`）。启用时执行：

1. 调用 `init_tables()` 初始化独立数据库 `ali_api.db`
2. 设置 i18n 桥接（`set_plugin(self)`）
3. 注册 `ORDER_PAID` 事件监听器，用于自动创建采购单
4. 注册管理后台路由蓝图

### 配置要求

在使用前需在管理后台配置 1688 Open API 的 `AppKey` 和 `AppSecret`，否则无法调用 1688 API。

## 配置说明

`plugin.json` 中的配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `api_gateway` | string | `https://gw.open.1688.com/openapi` | 1688 Open API 网关地址 |

权限配置：

| 权限标识 | 说明 |
|----------|------|
| `network.request` | 允许发起外部网络请求（调用 1688 API） |
| `shop.product.write` | 允许写入商品数据（发布到本地商城） |

## API 端点

### 提供的 Hook 接口

本插件不通过 Hook 机制对外提供接口，所有功能通过管理后台和内部事件机制实现。

### 管理后台路由

通过 `ali_admin_bp` 蓝图注册，提供以下主要 API 端点：

- 商品搜索与列表
- 商品详情获取
- AI 优化处理
- 本地商城发布
- 采购单管理

管理后台嵌入地址：`/admin/ali-api/`

## 依赖关系

### 事件监听

| 事件名称 | 处理逻辑 |
|----------|----------|
| `ORDER_PAID` | 订单支付后，检查订单商品是否涉及 1688 货源（通过 `features.ali_source` 标识），若是则自动创建 `AliPurchaseOrder` 采购单草稿，包含供应商信息、数量、价格等 |

### 事件提供

本插件不向事件总线提供 Hook 接口。

### 外部依赖

- **1688 Open API**：依赖阿里巴巴 1688 开放平台 API，需申请 AppKey 和 AppSecret
- 依赖 VeroRun 核心框架的 `BasePlugin`、事件总线（`EventName.ORDER_PAID`）、i18n 模块

### 菜单集成

- **菜单组**：Business Center
- **菜单项**：1688 供应链采集（图标：plugins）
- **嵌入地址**：`/admin/ali-api/`

## 许可证

作为 VeroRun 项目的一部分，遵循项目统一许可证。