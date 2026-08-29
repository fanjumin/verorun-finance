# Logistics Express (logistics)

## 概述

Logistics Express（物流配送查询）是 VeroRun 的物流轨迹追踪插件，对接快递鸟（Kdniao）API，支持 600 多家快递公司的物流轨迹实时查询。插件使用独立 PostgreSQL schema（`logistics`）存储查询日志。

## 功能特性

- **物流轨迹查询**：对接快递鸟 API，支持 600+ 快递公司实时物流轨迹查询
- **快递公司识别**：自动识别快递单号对应的快递公司
- **状态文本化**：提供 `logistics/get_shipping_status_text` Hook，将物流状态码转换为可读文本
- **查询日志**：所有查询记录保存到 PostgreSQL `logistics` schema，方便审计和问题排查
- **管理后台**：内置 `admin_logistics.html` 管理界面，支持可视化查询和配置

## 架构设计

### 数据库策略

使用**独立 PostgreSQL schema** `logistics`（通过 `CREATE SCHEMA IF NOT EXISTS logistics`），存储查询日志记录。配置信息（快递鸟商户 ID、API Key）通过系统配置读取，支持环境变量覆盖。

### 模块结构

```
logistics/
├── __init__.py               # 插件入口，LogisticsPlugin 类定义
├── models.py                 # 数据模型，数据库初始化，查询日志表
├── routes.py                 # Flask 蓝图路由，管理后台与查询 API
├── services.py               # 核心服务层，快递鸟 API 调用与轨迹解析
├── templates/
│   └── admin_logistics.html  # 管理后台界面
└── i18n/
    ├── en.yml
    └── zh-CN.yml
```

## 目录结构

| 文件/目录 | 说明 |
|-----------|------|
| `__init__.py` | 插件入口，定义 `LogisticsPlugin` 类，处理生命周期，提供 `get_kdniao_config()` 等对外接口 |
| `models.py` | 数据模型层，提供 `init_logistics_db()` 初始化数据库，定义查询日志表结构 |
| `routes.py` | 路由层，提供 `logistics_bp` 蓝图，暴露查询 API |
| `services.py` | 核心服务层，封装快递鸟 API 调用、轨迹数据解析、状态转换 |
| `templates/admin_logistics.html` | 管理后台页面 |
| `i18n/en.yml` | 英文翻译 |
| `i18n/zh-CN.yml` | 中文翻译 |
| PostgreSQL `logistics` Schema | 独立 PostgreSQL Schema（`CREATE SCHEMA IF NOT EXISTS logistics`） |

## 安装与启用

### 安装

插件已内置在 `plugins/logistics/` 目录下。VeroRun 启动时会自动扫描并注册。

### 启用

插件默认启用（`enabled: true`）。启用时执行：

1. 调用 `init_logistics_db()` 初始化独立 PostgreSQL schema `logistics`（幂等操作：`CREATE SCHEMA IF NOT EXISTS` + `CREATE TABLE IF NOT EXISTS`）
2. 注册 Flask 蓝图路由

### 配置要求

在使用前需在管理后台配置快递鸟商户 ID（`kdniao_eid`）和 API Key（`kdniao_api_key`），否则无法调用快递鸟 API。支持通过环境变量覆盖配置。

## 配置说明

`plugin.json` 中的配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `kdniao_eid` | string | (空) | 快递鸟商户 ID（EBusinessID） |
| `kdniao_api_key` | string | (空) | 快递鸟 API Key |

权限配置：

| 权限标识 | 说明 |
|----------|------|
| `logistics.query` | 允许查询物流轨迹 |
| `network:request` | 允许发起外部 API 请求（快递鸟） |
| `admin:access` | 允许访问管理后台物流页面 |

## API 端点

### 提供的 Hook 接口

| Hook 名称 | 功能描述 |
|-----------|----------|
| `logistics/query_track` | 根据快递单号和快递公司编码查询物流轨迹 |
| `logistics/get_shipping_status_text` | 将物流状态码（如 0/1/2/3）转换为可读文本 |

### 管理后台路由

通过 `logistics_bp` 蓝图注册，提供物流查询 API 和管理界面。

## 依赖关系

### 事件监听

本插件不监听任何系统事件。

### 事件提供

本插件向事件总线提供 `logistics/query_track` 和 `logistics/get_shipping_status_text` 两个 Hook，供订单系统和前端调用。

### 外部依赖

- **快递鸟（Kdniao）API**：依赖快递鸟物流查询 API，需注册商户账号获取 EBusinessID 和 API Key
- 依赖 VeroRun 核心框架的 `BasePlugin`、i18n 模块

### 菜单集成

- **菜单组**：Business Center
- **菜单项**：Logistics Tracking（图标：plugins）

## 许可证

作为 VeroRun 项目的一部分，遵循项目统一许可证。