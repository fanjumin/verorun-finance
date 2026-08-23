# AI Advisor (chatbot)

## 概述

AI Advisor 是 VeroRun 平台的全站智能客服插件，提供基于大语言模型（LLM）的 AI 对话能力。插件使用独立的 PostgreSQL 数据库 schema（`chatbot`），自包含配置管理、会话记录和 Agent 注册表，同时跨读主库以获取知识库数据和用户工单信息。

插件内置 Advisor Agent（`chat_assistant`），基于 DashScope 的 qwen-turbo 模型，具备 FAQ 问答、工单查询和自动转人工三大核心能力。支持多渠道接入（Telegram、LINE），可自动识别用户意图并完成工单创建与转接。

## 功能特性

- **AI 智能对话**：基于 DashScope qwen-turbo 大模型，提供自然语言理解与回复
- **多渠道接入**：支持 Web 端浮动按钮、Telegram Bot Webhook、LINE Messaging Webhook
- **FAQ 问答**：Agent 内置 `chatbot.faq` 能力，可从知识库检索匹配答案
- **工单查询**：Agent 内置 `chatbot.ticket` 能力，可跨读主库查询用户工单状态
- **自动转人工**：支持关键词匹配和失败次数阈值两种转人工策略，自动创建工单（`[TICKET_CREATE]` 标记解析）
- **对话统计**：提供今日统计概览、热门问题排行、座席绩效分析
- **对话质检**：支持对单轮对话进行质量分析（QA Check）
- **Agent Copilot**：为人工坐席提供 AI 回复建议
- **CSAT 满意度**：支持用户提交 1-5 星满意度评分
- **独立数据库**：使用 PostgreSQL schema `chatbot`，包含 `plugin_configs`、`agent_registry`、`chatbot_sessions` 三张核心表
- **数据迁移**：首次启动自动从主库幂等迁移配置、Agent 注册信息和最近 30 天会话记录

## 架构设计

```
+---------------------------------------------------+
|                    前端层                            |
|  +----------------+  +----------------+  +--------+ |
|  | Web 浮动按钮    |  | Telegram Bot  |  | LINE Bot| |
|  +-------+--------+  +-------+--------+  +----+---+ |
+----------+-------------------+----------------+------+
           |                   |                |
           v                   v                v
+---------------------------------------------------+
|                 路由层 (routes.py)                   |
|  +---------------------------------------------+   |
|  |  chatbot_bp (/admin/chatbot)                 |   |
|  |  +-- /chat               AI 对话              |   |
|  |  +-- /settings           配置读写             |   |
|  |  +-- /stats              统计概览             |   |
|  |  +-- /hot_topics         热门问题             |   |
|  |  +-- /agent_performance  座席绩效             |   |
|  |  +-- /qa_check           对话质检             |   |
|  |  +-- /copilot_suggest    Agent Copilot       |   |
|  |  +-- /csat               满意度评分           |   |
|  |  +-- /handoff_rules      转人工规则           |   |
|  |  +-- /escalate           创建工单             |   |
|  +---------------------------------------------+   |
|  +---------------------------------------------+   |
|  |  webhook_bp (/api/v1/channels)              |   |
|  |  +-- /telegram/webhook   Telegram Webhook   |   |
|  |  +-- /line/webhook       LINE Webhook       |   |
|  +---------------------------------------------+   |
+----------------------+----------------------------+
                       |
                       v
+---------------------------------------------------+
|              渠道路由层 (channels/router.py)         |
|  +---------------------------------------------+   |
|  |  telegram_handle_webhook()                  |   |
|  |  line_handle_webhook()                      |   |
|  |  _call_ai()              统一 AI 调用入口     |   |
|  |  _get_channel_config()   读取 IM Gateway 配置 |   |
|  +---------------------------------------------+   |
+----------------------+----------------------------+
                       |
                       v
+---------------------------------------------------+
|                   数据层                             |
|  +------------------------+  +-------------------+ |
|  |  chatbot 独立库         |  |  主库（只读）       | |
|  |  +-- plugin_configs     |  |  +-- user_tickets | |
|  |  +-- agent_registry     |  |  +-- knowledge_base| |
|  |  +-- chatbot_sessions   |  |  +-- users        | |
|  +------------------------+  +-------------------+ |
+---------------------------------------------------+
```

**核心设计原则**：

- **独立数据库 + 主库只读**：插件拥有独立的 PG schema `chatbot`，不污染主库；需要用户工单等数据时跨读主库
- **多渠道统一路由**：`channels/router.py` 统一处理 Telegram 和 LINE 的消息，复用同一套 AI 对话引擎
- **Agent 本地注册**：在独立库中维护 `agent_registry` 表，避免对主库 `agent_matrix` 的写依赖

## 目录结构

```
chatbot/
+-- README.md                    # 插件文档
+-- plugin.json                  # 插件元数据配置
+-- __init__.py                  # 插件入口，注册蓝图和初始化
+-- models.py                    # 数据模型（独立库连接、表创建、配置读写、Agent 注册、数据迁移）
+-- routes.py                    # 管理端 API 路由（对话、配置、统计、质检、转人工等）
+-- stats.py                     # 统计模块（会话日志、今日统计、CSAT、热门问题、座席绩效）
+-- channels/
|   +-- __init__.py
|   +-- router.py                # 多渠道路由核心（Telegram/LINE Webhook 处理）
+-- prompts/
|   +-- sub_chatbot_prompt.md    # Advisor Agent 系统提示词
+-- templates/
    +-- admin_chatbot.html       # 管理后台页面模板
```

## 安装与启用

### 前提条件

- VeroRun 平台版本 >= 0.10.0
- DashScope API Key（用于 qwen-turbo 模型调用）
- PostgreSQL 数据库（插件使用独立 schema `chatbot`）

### 安装步骤

1. 将 `chatbot` 目录放置于 `plugins/` 下
2. 确保 `plugin.json` 中 `enabled` 为 `true`
3. 重启应用，插件将自动完成以下初始化：
   - 创建 PostgreSQL schema `chatbot`
   - 初始化 `plugin_configs`、`agent_registry`、`chatbot_sessions` 三张核心表
   - 从主库幂等迁移已有配置数据、Agent 注册信息和最近 30 天会话记录
4. 在管理后台 "AI & Content" > "AI Advisor" 中配置插件参数

### 启用多渠道（可选）

**Telegram**：

1. 在 IM Gateway 插件中配置 Telegram 频道（bot_token 等）
2. 设置环境变量 `TELEGRAM_SECRET_TOKEN`（用于 Webhook 认证）
3. 配置 Telegram Bot Webhook 指向 `https://<your-domain>/api/v1/channels/telegram/webhook`

**LINE**：

1. 在 IM Gateway 插件中配置 LINE 频道（access_token 等）
2. 设置环境变量 `LINE_CHANNEL_SECRET`（用于签名验证）
3. 配置 LINE Messaging Webhook 指向 `https://<your-domain>/api/v1/channels/line/webhook`

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | boolean | true | 是否启用 AI Advisor |
| `auto_escalate` | boolean | true | 转人工时是否自动创建工单 |
| `title` | string | "AI Advisor" | 聊天窗口标题 |
| `subtitle` | string | "Powered by AI Engine" | 聊天窗口副标题 |
| `welcome_message` | string | - | 欢迎消息 |
| `help_hint` | string | - | 帮助提示文本 |
| `avatar_url` | string | "" | 机器人头像图片 URL |
| `agent_id` | string | "chat_assistant" | 绑定的 Agent 标识符 |
| `max_history` | integer | 20 | 最大对话历史轮数（范围 1-50） |
| `float_button_text` | string | "AI Advisor" | 页面浮动按钮显示文本 |
| `handoff_keywords` | JSON array | ["human","agent",...] | 触发转人工的关键词列表 |
| `handoff_max_fails` | integer | 3 | 连续失败次数阈值，达到后自动转人工 |

## API 端点

### 管理端 API（需要管理员权限）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/chatbot/chat` | 发送对话消息，返回 AI 回复 |
| GET | `/admin/chatbot/settings` | 获取插件全部配置 |
| POST | `/admin/chatbot/settings` | 保存插件配置（批量更新） |
| POST | `/admin/chatbot/log_session` | 记录一次对话回合（需登录） |
| GET | `/admin/chatbot/stats` | 获取今日统计概览 |
| GET | `/admin/chatbot/hot_topics` | 获取今日热门问题 Top 10 |
| GET | `/admin/chatbot/agent_performance` | 获取座席绩效数据 |
| POST | `/admin/chatbot/qa_check` | 对话质检分析（需登录） |
| POST | `/admin/chatbot/copilot_suggest` | Agent Copilot 生成回复建议（需登录） |
| POST | `/admin/chatbot/csat` | 提交 CSAT 满意度评分（1-5 分，需登录） |
| GET | `/admin/chatbot/handoff_rules` | 获取转人工规则配置 |
| POST | `/admin/chatbot/handoff_rules` | 保存转人工规则配置 |
| POST | `/admin/chatbot/escalate` | AI 转人工，创建工单（需登录） |

### 公开 Webhook 端点

| 方法 | 路径 | 认证方式 | 说明 |
|------|------|----------|------|
| POST | `/api/v1/channels/telegram/webhook` | X-Telegram-Bot-Api-Secret-Token 请求头 | Telegram Bot Webhook 接收端点 |
| POST | `/api/v1/channels/line/webhook` | x-line-signature 请求头 (HMAC-SHA256) | LINE Messaging Webhook 接收端点 |

## 依赖关系

### 内部依赖

| 依赖项 | 用途 |
|--------|------|
| `plugins._base.db` | 插件基础数据库连接模块，提供 `get_raw_connection()` |
| `auth-center.models` | 主库读取（user_tickets、knowledge_base、users 等表） |
| `auth-center.services.jwt_service` | JWT Token 验证（`validate_token`） |
| `agent_matrix.engine` | 统一 LLM 引擎（`UnifiedLLM`） |
| `agent_matrix.intent` | 意图分类模块（`classify_intent`） |
| `plugins.im_gateway` | IM Gateway 插件，读取 Telegram/LINE 频道配置凭证 |

### 外部依赖

| 依赖项 | 用途 |
|--------|------|
| DashScope API (qwen-turbo) | AI 对话模型，提供自然语言理解与生成 |
| Telegram Bot API | 多渠道消息发送（`sendMessage`） |
| LINE Messaging API | 多渠道消息回复（`replyMessage`） |

### 提供的 Hook

| Hook 标识符 | 说明 |
|-------------|------|
| `chatbot/config` | 获取聊天机器人配置 |
| `chatbot/chat` | 执行 AI 对话 |

### Agent 注册

| 属性 | 值 |
|------|-----|
| 名称 | Advisor Agent |
| 标识符 | `chat_assistant` |
| 角色类型 | sub |
| 领域 | chatbot |
| 模型策略 | tier: standard, 允许用户覆盖 |
| 能力 | `chatbot.faq`, `chatbot.ticket`, `chatbot.human_handoff` |

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。