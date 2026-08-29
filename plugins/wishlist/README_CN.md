# 收藏/心愿单 (wishlist)

## 概述

收藏/心愿单（wishlist）是 VeroRun 的商品收藏与心愿清单管理插件，允许用户收藏感兴趣的商品、管理心愿清单。插件使用独立 PostgreSQL schema `wishlist` 存储数据，路由在 `__init__.py` 中以内联 Blueprint 方式定义，无管理菜单。

## 功能特性

- **收藏/取消收藏**：一键收藏或取消收藏商品，支持 toggle 模式
- **心愿清单管理**：分页展示收藏列表，关联商品实时信息（价格、库存、销量等）
- **批量状态检查**：支持批量检查多个商品的收藏状态，用于列表页收藏图标展示
- **收藏计数**：提供收藏数量统计接口
- **跨库关联**：收藏数据独立存储于 PG `wishlist` schema，查询时只读关联主库商品信息（标题、价格、缩略图等）

## 架构设计

### 数据库策略

使用**独立 PostgreSQL schema** `wishlist`（单库多 Schema 架构，见插件标准 §9.1）存储收藏记录。查询时**只读**主库 `public.products` 表，获取商品的实时价格、库存、销量等信息。

### 模块结构

```
wishlist/
├── __init__.py          # 插件入口 + 路由定义，WishlistPlugin 类 + Blueprint
├── models.py            # 数据模型，数据库初始化
├── migrations/          # Schema 迁移脚本
│   └── v1.0.0_init.sql
└── i18n/
    ├── en.yml
    └── zh-CN.yml
```

## 目录结构

| 文件/目录 | 说明 |
|-----------|------|
| `__init__.py` | 插件入口 + 路由定义，`WishlistPlugin` 类定义，`register_routes()` 内联定义 Blueprint 和所有 API 端点 |
| `models.py` | 数据模型层，提供 `get_db()`（独立 schema）、`get_main_db()`（主库只读）、`init_db()` 初始化数据库 |
| `migrations/` | 版本化 Schema 迁移脚本目录 |
| `i18n/en.yml` | 英文翻译（英文源串 identity 映射） |
| `i18n/zh-CN.yml` | 中文翻译（英文源串 → 中文映射） |

## 安装与启用

### 安装

插件已内置在 `plugins/wishlist/` 目录下。VeroRun 启动时会自动扫描并注册。

### 启用

插件默认启用（`enabled: true`）。启用时执行：

1. 调用 `init_db()` 幂等初始化 PostgreSQL `wishlist` schema 与表
2. 注册 Flask 蓝图路由（路由前缀：`/plugin/wishlist`）

## 配置说明

`plugin.json` 中提供 `max_page_size` 配置项（最大分页大小，默认 50），通过管理后台设置页调整。

权限配置（插件标准 §10.2 权限模型）：

| 权限标识 | 说明 |
|----------|------|
| `api:read` | 可读取业务数据（商品、收藏列表） |
| `user:profile` | 可读取用户基本信息（JWT 中的用户标识） |

## API 端点

### 提供的 Hook 接口

| Hook 名称 | 功能描述 |
|-----------|----------|
| `wishlist/sync` | 同步用户的收藏数据，用于多端/离线数据同步场景 |

### 用户端 API

通过 Blueprint（路由前缀 `/plugin/wishlist`）提供以下端点（均需 JWT 鉴权）：

| 路由 | 方法 | 说明 |
|------|------|------|
| `/api/list` | GET | 获取当前用户的收藏列表（分页，关联商品实时信息） |
| `/api/toggle` | POST | 切换收藏/取消收藏状态 |
| `/api/check` | POST | 批量检查多个商品的收藏状态 |
| `/api/count` | GET | 获取当前用户的收藏总数 |

## 依赖关系

### 事件提供

本插件向事件总线提供 `wishlist/sync` Hook，用于收藏数据同步场景。

### 外部依赖

- 依赖 VeroRun 核心框架的 `BasePlugin`、JWT 服务（`validate_token`）、i18n 模块
- 数据存储依赖 PostgreSQL（经 `plugins/_base/db.py` 的 `get_raw_connection()` 统一连接）
- 无外部第三方服务依赖

### 菜单集成

本插件**无管理菜单**，所有功能通过 API 和前端页面集成。

## Dashboard 统计

插件通过 `get_dashboard_stats()` 向管理后台提供统计指标：

| 指标 key | 说明 | 类型 |
|----------|------|------|
| `total_favorites` | 收藏总数 | counter |
| `active_users` | 有收藏的用户数 | counter |

## 许可证

作为 VeroRun 项目的一部分，遵循项目统一许可证。
