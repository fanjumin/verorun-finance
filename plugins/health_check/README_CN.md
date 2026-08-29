# Health Check (health_check)

## 概述

Health Check 是 VeroRun 的自动化健康巡检插件，提供可扩展的系统健康检查框架，支持定时自动巡检、仪表盘可视化、多渠道异常告警、Workflow 引擎集成以及 AI 辅助修复能力。适用于生产环境的系统运维监控场景。

版本：**1.4.0**

## 功能特性

- **可扩展检查框架**：基于插件化设计，支持自定义健康检查器（Checker），可灵活扩展检查项
- **定时自动巡检**：内置调度器，支持按 cron 表达式定时执行健康检查任务
- **仪表盘可视化**：通过管理后台嵌入展示系统健康评分和历史趋势
- **多渠道异常告警**：支持邮件、站内信、Webhook、飞书、钉钉 5 种告警通道
- **Workflow 引擎集成**：健康检查结果可作为 Workflow 触发条件，与其他业务系统联动
- **AI 辅助修复**：内置 AI 修复器，可根据检查结果自动建议或执行修复操作
- **服务发现**：自动发现需要监控的服务和组件
- **健康评分**：综合多项检查指标，生成系统健康评分

## 架构设计

### 数据库策略

插件使用 PostgreSQL 的 `health` schema 进行数据存储，共包含 **8 张数据表**。本地开发环境使用 SQLite 文件 `data/health.db`。

### 模块结构

```
┌─────────────────────────────────────────────────────────────┐
│                     scheduler_setup.py                       │
│                  (定时巡检调度器初始化)                          │
└─────────────────────────────┬───────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌──────────────────┐ ┌────────────────┐ ┌──────────────────┐
│   discovery.py   │ │  checkers.py   │ │   metrics.py     │
│   (服务发现)      │ │  (检查器框架)   │ │   (指标采集)      │
└──────────────────┘ └──────┬─────────┘ └──────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                       models.py                              │
│                   (8 张数据库表 ORM)                           │
└─────────────────────────────┬───────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌──────────────────┐ ┌────────────────┐ ┌──────────────────┐
│   alerter.py     │ │  ai_fixer.py   │ │   routes.py      │
│   (多渠道告警)    │ │  (AI 辅助修复)  │ │   (管理后台路由)   │
│ 邮件/站内信/      │ │                │ │                  │
│ Webhook/飞书/钉钉 │ │                │ │                  │
└──────────────────┘ └────────────────┘ └──────────────────┘
```

### 8 张数据表

| 表名 | 用途 |
|------|------|
| `health_checks` | 健康检查项定义 |
| `health_check_results` | 检查结果记录 |
| `health_alerts` | 告警记录 |
| `health_alert_rules` | 告警规则配置 |
| `health_alert_channels` | 告警通道配置 |
| `health_schedules` | 巡检调度计划 |
| `health_metrics` | 健康指标时序数据 |
| `health_components` | 被监控组件注册表 |

## 目录结构

```
health_check/
├── __init__.py              # 插件入口，注册 Hook 与路由
├── models.py                # 8 张数据表的 ORM 模型定义
├── routes.py                # 管理后台 API 路由
├── checkers.py              # 可扩展检查器框架
├── discovery.py             # 服务与组件自动发现
├── alerter.py               # 多渠道告警发送器（邮件/站内信/Webhook/飞书/钉钉）
├── ai_fixer.py              # AI 辅助修复引擎
├── metrics.py               # 健康指标采集与计算
├── scheduler_setup.py       # 定时巡检调度器设置
├── plugin.json              # 插件元数据配置
├── DEVELOPER.md             # 开发者文档
├── data/
│   └── health.db            # 本地开发 SQLite 数据库
├── i18n/
│   ├── en.yml               # 英文国际化
│   └── zh-CN.yml            # 中文国际化
└── templates/
    └── health.html          # 管理后台仪表盘模板
```

## 安装与启用

### 安装

插件已包含在 VeroRun 的默认插件目录中，无需额外安装步骤。

### 启用

1. 确保 PostgreSQL 数据库中存在 `health` schema
2. 在 VeroRun 管理后台 "插件管理" 页面中启用 Health Check 插件
3. 插件启用后，调度器将自动启动并按配置的 cron 表达式执行巡检
4. 管理后台 "Monitoring & Data" 菜单组将出现 Health Check 入口

### 本地开发

本地开发时，插件会自动使用 SQLite 数据库 `data/health.db`。

## 配置说明

在 `plugin.json` 中配置以下参数：

```json
{
  "name": "health_check",
  "version": "1.4.0",
  "database": {
    "type": "postgresql",
    "schema": "health"
  },
  "scheduler": {
    "enabled": true,
    "cron": "*/5 * * * *"
  },
  "alert_channels": {
    "email": { "enabled": true },
    "site_message": { "enabled": true },
    "webhook": { "enabled": false, "url": "" },
    "feishu": { "enabled": false, "webhook_url": "" },
    "dingtalk": { "enabled": false, "webhook_url": "" }
  },
  "ai_fixer": {
    "enabled": true,
    "auto_fix": false
  }
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `database.schema` | PostgreSQL schema 名称 | `health` |
| `scheduler.enabled` | 是否启用定时巡检 | `true` |
| `scheduler.cron` | 巡检 cron 表达式 | `*/5 * * * *`（每5分钟） |
| `alert_channels.email.enabled` | 是否启用邮件告警 | `true` |
| `alert_channels.site_message.enabled` | 是否启用站内信告警 | `true` |
| `alert_channels.webhook.enabled` | 是否启用 Webhook 告警 | `false` |
| `alert_channels.feishu.enabled` | 是否启用飞书告警 | `false` |
| `alert_channels.dingtalk.enabled` | 是否启用钉钉告警 | `false` |
| `ai_fixer.enabled` | 是否启用 AI 修复器 | `true` |
| `ai_fixer.auto_fix` | 是否自动执行修复 | `false` |

## API 端点

### Hook 提供

| Hook 标识符 | 类型 | 说明 |
|-------------|------|------|
| `health/run_check` | Hook | 手动触发一次健康检查 |
| `health/get_status` | Hook | 获取当前系统健康状态 |
| `health/get_trend` | Hook | 获取健康趋势数据 |

### 管理后台

| 路径 | 说明 |
|------|------|
| `/admin/health/` | 健康检查仪表盘（嵌入页面） |

### Filter 注册

| Filter 标识符 | 说明 |
|---------------|------|
| `dashboard.data` | 模块级注册，向管理后台仪表盘注入健康评分 |

## 依赖关系

### 内部依赖

- VeroRun 核心框架：Hook 系统、事件总线、调度器
- 管理后台（auth-center）：仪表盘嵌入与菜单渲染
- **email** 插件：邮件告警通道
- **im_gateway** 插件：飞书/钉钉告警通道

### 外部依赖

- **PostgreSQL**：生产环境数据存储（`health` schema）

### 被依赖

- **Workflow 引擎**：通过 Hook 调用健康检查结果作为工作流触发条件
- **analytics** 插件：健康检查可读取分析数据作为参考指标

### 菜单

- **菜单组**：`Monitoring & Data`
- **嵌入 URL**：`/admin/health/`

## 许可证

本插件为 VeroRun 项目的一部分，遵循 VeroRun 项目的整体许可证协议。