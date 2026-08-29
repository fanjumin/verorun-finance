# Content Factory (content_factory)

## 概述

Content Factory（内容工厂）是 VeroRun 平台的内容生产与管理中心，提供从多源内容采集、AI 智能加工、审核工作流、多渠道发布到 Skill 推送和静态页面生成的全链路内容处理能力。插件使用独立的 PostgreSQL schema `content_factory`，包含 5 张核心业务表，并通过定时任务（`cron.tick`）实现自动化采集调度。

插件支持 RSS 等多源内容采集，使用通义千问（DashScope）进行 AI 内容加工（摘要、重新排版、关键词提取），配备完整的审核工作流（草稿-提交审核-批准/驳回-发布），并可将加工后的内容推送到 CMS 文章系统、社交媒体平台和 Skill 知识库。

## 功能特性

- **多源内容采集**：支持 RSS 源采集，可配置采集间隔、关键词过滤、最大抓取量
- **AI 智能加工**：基于通义千问的批量 AI 处理，包括摘要生成、内容重写、排版优化
- **AI 排版与配图**：支持 AI 驱动的 HTML 排版修复和 AI 封面图生成（通义万相）
- **审核工作流**：完整的草稿-提交审核-批准/驳回-发布状态流转
- **多渠道发布**：支持内部 CMS 发布、社交媒体分发（通过 social_push 插件）
- **Skill 推送**：将加工内容转化为 Agent Skill，推送到指定 Agent（如 hermes）
- **静态页面生成**：支持生成静态 HTML 页面（文章、分类、文档索引）
- **知识库推送**：将加工内容推送到知识库系统
- **定时采集**：通过 `cron.tick` 端点结合外部 cron 实现定时自动采集
- **独立数据库**：使用 PostgreSQL schema `content_factory`，包含 5 张核心业务表

## 架构设计

```
+--------------------------------------------------------------+
|                        前端管理界面                             |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      路由层 (routes.py)                        |
|  /admin/content-factory/*                                     |
|  +-- /                仪表盘统计                               |
|  +-- /sources         来源管理 CRUD                            |
|  +-- /crawl           触发采集                                 |
|  +-- /contents        原始内容列表                             |
|  +-- /process         触发 AI 加工                             |
|  +-- /processed       加工内容列表与编辑                        |
|  +-- /ai-format       AI 排版                                 |
|  +-- /ai-cover        AI 配图                                 |
|  +-- /review          审核流程                                 |
|  +-- /publish         发布（内部 CMS / 社媒）                   |
|  +-- /push-skill      Skill 推送                              |
|  +-- /generate-static 静态页面生成                             |
|  +-- /push-to-knowledge 知识库推送                             |
|  +-- /cron/tick       定时采集 Tick                           |
|  +-- /api/v1/skills   用户端 Skill 拉取 API                    |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      服务层 (services/)                        |
|  +-- ai_processor.py    AI 批量加工引擎                        |
|  +-- base_collector.py  采集器基类                             |
|  +-- skill_pusher.py    Skill 推送逻辑                         |
|  +-- collectors/                                             |
|      +-- rss_collector.py   RSS 采集器实现                     |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      数据层 (models.py)                        |
|  PG Schema: content_factory                                   |
|  +-- content_sources       内容来源配置                        |
|  +-- raw_contents          原始采集内容                        |
|  +-- processed_contents    加工后内容                          |
|  +-- content_tasks         采集任务记录                        |
|  +-- skill_pushes          Skill 推送记录                      |
+--------------------------------------------------------------+
```

**审核工作流状态机**：

```
draft --> submit_review --> review --> approve --> approved --> publish --> published
  ^                      |           |                                      |
  |                      v           v                                      |
  +--- back_to_draft ----+ rejected  +--- back_to_draft -------------------+
```

## 目录结构

```
content_factory/
+-- README.md                    # 插件文档
+-- plugin.json                  # 插件元数据配置
+-- __init__.py                  # 插件入口，注册蓝图和 Hook
+-- models.py                    # 数据模型（独立库连接、5 张核心表创建）
+-- routes.py                    # 管理端 API 路由（23 个端点）
+-- content_factory.db           # 独立数据库文件（保留用于迁移）
+-- services/
|   +-- __init__.py
|   +-- ai_processor.py          # AI 加工服务（批量处理）
|   +-- base_collector.py        # 采集器抽象基类
|   +-- skill_pusher.py          # Skill 推送服务
|   +-- collectors/
|       +-- __init__.py
|       +-- rss_collector.py     # RSS 采集器实现
+-- i18n/
|   +-- en.yml                   # 英文国际化
|   +-- zh-CN.yml                # 中文国际化
+-- templates/
    +-- admin_contentfactory.html # 管理后台页面模板
```

## 安装与启用

### 前提条件

- VeroRun 平台版本 >= 0.10.0
- DashScope API Key（用于 AI 加工和配图）
- PostgreSQL 数据库

### 安装步骤

1. 将 `content_factory` 目录放置于 `plugins/` 下
2. 确保 `plugin.json` 中 `enabled` 为 `true`
3. 重启应用，插件将自动创建 PostgreSQL schema `content_factory` 并初始化 5 张核心表
4. 在管理后台 "AI & Content" > "Content Factory" 中配置插件参数

### 配置定时采集

插件通过 `cron.tick` Hook 监听外部 cron 触发。配置外部 cron 定时请求：

```
POST /admin/content-factory/cron/tick
Header: X-Cron-Secret: <your-cron-secret>
```

设置环境变量 `CRON_SECRET` 以启用认证。

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `dashscope_text_key` | string | "" | 通义千问 API Key，用于 AI 内容加工 |
| `max_items_per_run` | integer | 10 | 每次采集运行的最大条目数 |
| `skip_review` | boolean | false | 是否跳过人工审核，AI 加工后直接进入已批准状态 |
| `auto_publish` | boolean | false | AI 加工后是否自动发布 |

## API 端点

### 仪表盘与统计

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/content-factory/` | 仪表盘统计（来源数、待处理数、已处理数等） |
| GET | `/admin/content-factory/stats` | 详细统计（含最近采集来源） |

### 来源管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/content-factory/sources` | 列出所有内容来源 |
| POST | `/admin/content-factory/sources` | 新增内容来源 |
| PUT | `/admin/content-factory/sources/<id>` | 更新内容来源 |
| DELETE | `/admin/content-factory/sources/<id>` | 删除内容来源 |

### 采集与加工

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/content-factory/crawl` | 触发指定来源的内容采集 |
| POST | `/admin/content-factory/process` | 批量 AI 加工原始内容 |
| GET | `/admin/content-factory/contents` | 分页查询原始内容列表 |
| DELETE | `/admin/content-factory/contents/<id>` | 删除原始内容及其关联加工记录 |

### AI 排版与配图

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/content-factory/ai-format` | AI 排版修复与结构化 |
| POST | `/admin/content-factory/ai-cover` | AI 生成封面配图 |

### 加工内容管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/content-factory/processed` | 分页查询加工内容列表 |
| GET | `/admin/content-factory/processed/<id>` | 获取单条加工内容详情 |
| PUT | `/admin/content-factory/processed/<id>` | 编辑加工内容 |
| POST | `/admin/content-factory/processed/batch-delete` | 批量删除加工内容 |

### 审核流程

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/content-factory/review` | 执行审核操作（submit_review/approve/reject/back_to_draft） |

### 发布

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/content-factory/publish` | 发布到内部 CMS 或社交媒体平台 |

### Skill 推送

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/content-factory/push-skill` | 将加工内容推送到 Agent Skill |
| GET | `/admin/content-factory/pushed-skills` | 查询已推送的 Skill 列表 |
| DELETE | `/admin/content-factory/pushed-skills/<id>` | 删除 Skill 推送记录 |

### 静态页面与知识库

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/content-factory/generate-static` | 生成静态 HTML 页面 |
| POST | `/admin/content-factory/push-to-knowledge` | 推送加工内容到知识库 |

### 定时采集

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/content-factory/cron/tick` | 定时采集触发端点（需 X-Cron-Secret） |

### 公开 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/content-factory/api/v1/skills` | 用户端拉取 Skill 列表（无需认证） |
| GET | `/admin/content-factory/api/v1/skills/<id>/download` | 下载指定 Skill 详情 |

## 依赖关系

### 内部依赖

| 依赖项 | 用途 |
|--------|------|
| `plugins._base.db` | 插件基础数据库连接模块 |
| `auth-center.models` | 主库读取（cms_posts） |
| `auth-center.services.ai_content_generator` | 公共 AI 内容生成服务（`_qwen_chat`、`generate_image`） |
| `auth-center.routes.cleaner_agent` | 知识库推送（`process_clean_content`） |
| `platform.staticgen` | 静态页面生成引擎 |
| `plugins.social_push` | 社交媒体发布（通过 PluginManager 获取实例） |

### 外部依赖

| 依赖项 | 用途 |
|--------|------|
| DashScope API (通义千问) | AI 内容加工与排版 |
| DashScope API (通义万相) | AI 封面图生成 |

### 提供的 Hook

| Hook 标识符 | 说明 |
|-------------|------|
| `content_factory/collect` | 触发内容采集 |
| `content_factory/process` | 触发 AI 内容加工 |
| `content_factory/publish` | 触发内容发布 |
| `content_factory/push_skill` | 触发 Skill 推送 |

### 监听的 Hook

| Hook 标识符 | 说明 |
|-------------|------|
| `cron.tick` | 定时任务触发，用于自动采集调度 |

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。