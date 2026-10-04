# Email Service (email)

## 概述

Email Service 是 VeroRun 平台的统一邮件服务插件，提供完整的 SMTP 发信和 IMAP 收信能力，支持收件箱管理、邮件撰写、附件处理和联系人管理。插件使用独立的 PostgreSQL schema `email`，不依赖主库的邮件相关表，实现完全的数据隔离。

插件支持 SMTP/IMAP 协议。v1.6.0 起预置 22 家国内外常用邮件服务商清单（`mail_providers.py`），在设置页选择服务商或填写邮箱账号后点击「自动识别账号」，即可自动填充 SMTP/IMAP 服务器与端口（仅填充空字段，不覆盖手动配置）；同时仍支持任意自定义服务商手填。配置来源遵循环境变量 > PluginManager > system_config > 默认值（均为空）> 域名自动推导 的优先级顺序。

v1.7.0 起邮件能力全面增强：支持 CC/BCC 抄送密送、草稿箱、收件箱全文搜索、信箱批量管理（删除/标记已读未读/移动）与转发；同时将 `email/send`、`email/send_contact`、`email/get_config` 三个 Agent hooks 接线为系统级 MCP 工具（`email_send` / `email_send_contact` / `email_get_config`），Agent 与人类用户均可收发与撰写邮件。

v1.8.0 起强化安全管控：新增附件扩展名阻断、日发送配额、收件域名白名单、私网 SMTP/IMAP 探测开关等企业级安全配置；同时将 `email/send`、`email/send_contact`、`email/get_config` 三个钩子从仅 MCP 面补齐为插件间同步调用面（声明即承诺）。

## 功能特性

- **SMTP 发信**：支持纯文本和 HTML 邮件发送，支持 SSL/TLS 加密；CC/BCC 抄送与密送
- **IMAP 收信**：支持收件箱列表、邮件详情读取、自动标记已读
- **收件箱搜索**：服务端 IMAP SEARCH（FROM/SUBJECT/TEXT 并集），失败自动降级客户端内存过滤
- **草稿箱**：撰写中途保存草稿、草稿列表编辑与删除，发信成功后自动清理关联草稿
- **信箱批量管理**：多选删除（二次确认）、标记已读/未读、移动到 IMAP 文件夹
- **转发**：一键转发，主题加 `Fwd:` 前缀并引用原文
- **附件处理**：支持附件上传与下载，单附件限制 10MB，总附件限制 50MB
- **联系人管理**：Python 级合并已发送邮件联系人与联系表单联系人
- **多编码支持**：自动处理 UTF-8、GBK、GB2312、Latin-1 等多种编码
- **MIME 支持**：完整支持 multipart/alternative、multipart/mixed 邮件结构
- **发送记录**：单独记录所有已发送邮件到 `email_sent` 表
- **联系表单集成**：`send_contact_email` 方法支持品牌化联系表单邮件
- **Agent 工具**：`email_send` / `email_send_contact` / `email_get_config` 三个系统级 MCP 工具（经 `agent_matrix` 注册）
- **独立数据库**：使用 PostgreSQL schema `email`，包含 `email_sent`、`email_drafts` 表
- **灵活配置**：支持环境变量、PluginManager、system_config 三级配置来源
- **快捷配置**：预置 22 家国内外邮件服务商（阿里/网易/QQ/腾讯企业/新浪/搜狐/139/263/21CN/Foxmail/Gmail/Outlook/Yahoo/iCloud/AOL/Zoho/Yandex/Fastmail/Proton 等），按邮箱域名自动填充服务器参数

## 架构设计

```
+--------------------------------------------------------------+
|                        前端管理界面                             |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      路由层 (routes.py)                        |
|  /admin/email/*                                               |
|  +-- /inbox          收件箱列表（IMAP，支持 ?q= 搜索）          |
|  +-- /read/<uid>     读取邮件详情                              |
|  +-- /send           发送邮件（支持 cc/bcc）                   |
|  +-- /drafts         草稿箱（GET 列表 / POST 保存）            |
|  +-- /drafts/<id>    删除草稿 (DELETE)                        |
|  +-- /folders        邮箱文件夹列表（批量移动用）               |
|  +-- /batch          批量操作（delete/read/unread/move）       |
|  +-- /sent           已发送邮件列表                            |
|  +-- /contacts       联系人管理（合并多源）                     |
|  +-- /settings       配置读写（含服务商清单与自动识别）          |
|  +-- /attachment/<uid>/<filename>  附件下载                    |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      服务层 (services.py)                      |
|  +-- _connect_imap()             IMAP 连接                     |
|  +-- fetch_inbox(keyword=)       收件箱查询（含搜索）           |
|  +-- read_email()                邮件详情读取                  |
|  +-- get_attachment()            附件提取                      |
|  +-- send_email(cc=,bcc=)        SMTP 发信（CC/BCC）           |
|  +-- save_draft/list_drafts/delete_draft   草稿箱              |
|  +-- list_folders/delete_emails/mark_read/move_emails  信箱管理 |
|  +-- get_sent_emails()           已发送查询                    |
|  +-- send_contact_email(cfg=)    联系表单邮件                  |
|  +-- _get_mail_config()          配置合并引擎（含域名推导）     |
|  +-- _decode_mime_header()       MIME 头解码                   |
|  +-- _decode_body()              正文多编码解码                 |
|  +-- _get_email_body()           正文提取（plain/html）         |
|  +-- _get_attachments_from_msg() 附件提取                      |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      数据层 (models.py)                        |
|  PG Schema: email                                             |
|  +-- email_sent    已发送邮件记录表                            |
|  +-- email_drafts  草稿表（attachments 存 JSON 引用）          |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      系统级 MCP（email_mcp_server.py）          |
|  经 agent_matrix register_system_mcp_server 注册：             |
|  +-- email_send          发邮件（含 cc/bcc/附件）              |
|  +-- email_send_contact  联系表单邮件                          |
|  +-- email_get_config    脱敏配置查询                          |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      外部服务                                  |
|  +-- SMTP Server (SSL/TLS)                                    |
|  +-- IMAP Server                                              |
+--------------------------------------------------------------+
```

**配置优先级**：

```
环境变量 (SMTP_HOST, SMTP_PORT, ...)
    |
    v
PluginManager 配置 (email plugin config)
    |
    v
主库 system_config 表 (兼容旧配置)
    |
    v
默认值 (均为空)
    |
    v
域名自动推导 (mail_providers.py 预置服务商清单，仅填充空字段)
```

## 目录结构

```
email/
+-- README.md                    # 插件文档
+-- plugin.json                  # 插件元数据配置
+-- __init__.py                  # 插件入口，注册蓝图和 Hook
+-- mail_providers.py             # 邮件服务商预置清单（快捷配置的数据源）
+-- email_mcp_server.py           # 系统级 MCP 服务器（email_send / send_contact / get_config）
+-- models.py                    # 数据模型（PG schema: email 连接、email_sent/email_drafts 表创建）
+-- routes.py                    # 管理端 API 路由（收件箱、发送、草稿、批量管理、联系人等）
+-- services.py                  # 邮件服务核心逻辑（SMTP/IMAP、MIME 编解码、附件、草稿、搜索）
+-- i18n/
|   +-- en.yml                   # 英文国际化
|   +-- zh-CN.yml                # 中文国际化
+-- templates/
    +-- admin_email.html         # 管理后台页面模板
```

## 安装与启用

### 前提条件

- VeroRun 平台版本 >= 0.10.0
- 可用的 SMTP 和 IMAP 邮件服务器
- PostgreSQL 数据库

### 安装步骤

1. 将 `email` 目录放置于 `plugins/` 下
2. 确保 `plugin.json` 中 `enabled` 为 `true`
3. 配置邮件服务器参数（通过环境变量或后台设置页面）
4. 重启应用，插件将自动创建 PostgreSQL schema `email` 并初始化 `email_sent` 表
5. 在管理后台 "Users & Support" > "Email Management" 中配置和管理邮件

### 环境变量配置

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `SMTP_HOST` | SMTP 服务器地址 | （空，用户配置） |
| `SMTP_PORT` | SMTP 端口 | （空，用户配置） |
| `SMTP_USER` | SMTP 登录账号 | - |
| `SMTP_PASS` | SMTP 登录密码 | - |
| `SMTP_FROM` | 发件人地址 | 同 SMTP_USER |
| `IMAP_HOST` | IMAP 服务器地址 | （空，用户配置） |
| `IMAP_PORT` | IMAP 端口 | （空，用户配置） |
| `CONTACT_TO` | 联系表单收件人邮箱 | - |

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `smtp_host` | string | "" | SMTP 服务器主机名（用户配置） |
| `smtp_port` | integer | 0 | SMTP 端口（465=SSL，587=STARTTLS，用户配置） |
| `smtp_user` | string | "" | SMTP 登录用户名 |
| `smtp_pass` | string | "" | SMTP 登录密码（敏感字段，显示时掩码） |
| `smtp_from` | string | "" | 发件人地址 |
| `imap_host` | string | "" | IMAP 服务器主机名（用户配置） |
| `imap_port` | integer | 0 | IMAP 端口（用户配置） |
| `alert_recipient` | string | "" | 告警/晨报收件人地址（留空则邮件通道跳过；环境变量 `ALERT_RECIPIENT` 覆盖） |
| `blocked_attachment_exts` | string | `.exe,.bat,.cmd,.scr,.js,.vbs,.ps1,.jar,.msi` | 阻断的附件扩展名，逗号分隔（留空 = 允许全部） |
| `allow_private_targets` | boolean | false | 允许 test-config 探测回环/私网 SMTP/IMAP 目标（默认阻断） |
| `daily_send_quota` | integer | 200 | 每日最大发信量（0 = 不限）；从 `email_sent` 记录计数 |
| `recipient_domain_allowlist` | string | "" | 允许的收件人域名，逗号分隔（留空 = 允许全部） |

## API 端点

### 管理端 API（需要管理员权限）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/email/inbox` | 获取收件箱列表（分页，每页 50 条；`?q=` 关键词搜索） |
| GET | `/admin/email/read/<uid>` | 读取指定邮件详情（含正文、附件信息） |
| POST | `/admin/email/send` | 发送邮件（支持纯文本、HTML、附件、回复、cc/bcc） |
| GET | `/admin/email/drafts` | 草稿列表（更新序倒排） |
| POST | `/admin/email/drafts` | 保存/更新草稿（body 带 `draft_id` 为更新） |
| DELETE | `/admin/email/drafts/<id>` | 删除草稿 |
| GET | `/admin/email/folders` | IMAP 邮箱文件夹列表（批量移动用） |
| POST | `/admin/email/batch` | 批量操作：`action=delete\|read\|unread\|move` + `uids`（move 需 `folder`） |
| GET | `/admin/email/sent` | 获取已发送邮件列表 |
| GET | `/admin/email/contacts` | 获取联系人列表（合并已发送 + 联系表单） |
| GET | `/admin/email/settings` | 获取邮件服务配置（敏感字段掩码） |
| POST | `/admin/email/settings` | 保存邮件服务配置（支持域名自动补全） |
| GET | `/admin/email/providers` | 预置邮件服务商清单（22 家） |
| POST | `/admin/email/test-config` | 测试 SMTP/IMAP 连通性与认证 |
| GET | `/admin/email/attachment/<uid>/<filename>` | 下载邮件附件 |

### 发送邮件请求体示例

```json
{
  "to": "recipient@example.com",
  "subject": "邮件主题",
  "body": "纯文本正文",
  "body_html": "<h1>HTML 正文</h1>",
  "cc": "cc@example.com",
  "bcc": "bcc@example.com",
  "attachments": [
    {
      "filename": "report.pdf",
      "data": "<base64 编码数据>",
      "content_type": "application/pdf"
    }
  ],
  "reply_to_uid": 123
}
```

### 批量操作示例

```json
{
  "action": "move",
  "uids": [101, 102, 103],
  "folder": "Work"
}
```

### 系统级 MCP 工具（Agent 调用）

| 工具名 | 说明 | 关键参数 |
|--------|------|----------|
| `email_send` | 发送邮件（含 cc/bcc/附件） | `to`, `subject`, `body`, `body_html?`, `cc?`, `bcc?`, `attachments?` |
| `email_send_contact` | 联系表单邮件（发至 `CONTACT_TO`） | `name`, `email`, `subject`, `message` |
| `email_get_config` | 当前邮件配置摘要（密码脱敏） | 无 |

> MCP 工具命名空间为 `mcp__email__*`；配置由主进程经 `EMAIL_MCP_*` 环境变量快照注入
> （含域名推导结果），子进程无 DB 依赖。

## 依赖关系

### 内部依赖

| 依赖项 | 用途 |
|--------|------|
| `plugins._base.db` | 插件基础数据库连接模块 |
| `auth-center.models` | 主库读取（system_config 配置、contact_messages 联系人） |
| `auth-center.services.brand_service` | 品牌设置（联系表单邮件中的站点名称） |

### 外部依赖

| 依赖项 | 用途 |
|--------|------|
| Python `smtplib` | SMTP 协议支持 |
| Python `imaplib` | IMAP 协议支持 |
| Python `email` | MIME 邮件构建与解析 |

### 提供的 Hook

| Hook 标识符 | 说明 |
|-------------|------|
| `email/send` | 发送邮件 |
| `email/send_contact` | 发送联系表单邮件 |
| `email/get_config` | 获取邮件配置 |

## 事件驱动的投研推送（Event-Driven Research Delivery）

Email 插件除被动收发邮件外，还作为 **VeroRun 投研事件的总出口**——通过平台 Hook Registry（`get_event_handlers` 订阅、`do_action` 发射，与 `get_event_bus` 事件总线是两条互相隔离的通道，不可混用）消费来自 `stock_analysis` 的投研事件并落地为邮件。这是 O6 晨报推送闭环与告警邮件闭环的消费方。

### 订阅的事件

| 事件标识符 | 消费函数 | 说明 |
|-----------|----------|------|
| `stock.alert.triggered` | `push_alert` | 行情/信号预警邮件。尊重 `channels` 配置，仅当告警声明的渠道含 `email` 且插件已配置收件人时发送；未配置收件人则安全跳过（不发、不报错）。 |
| `stock.morning_brief.ready` | `push_morning_brief` | 定时晨报邮件。订阅 `stock_analysis` 调度器在交易日 08:30（周一至周五）生成的晨报载荷，经 `_format_morning_mail` 渲染为纯文本/HTML 邮件后推送。 |

> 发射侧在 `stock_analysis` 插件：`alert_engine.py` 经 `do_action("stock.alert.triggered")` 发射；`morning_brief.py` 的 `dispatch_morning_brief()` 经 `do_action("stock.morning_brief.ready", payload=...)` 发射。两侧均走 Hook Registry，确保订阅/发射同通道、不静默失效。

### 晨报推送链路（O6 闭环）

```
stock_analysis 调度器 (交易日 08:30, mon-fri)
    |
    |  build_morning_brief()  —— 纯函数，汇总上一交易日 sa_signal_log
    |  dispatch_morning_brief() —— pg_try_advisory_lock 单跑 + do_action
    v
do_action("stock.morning_brief.ready", payload={summary, signals, date})
    |
    v
email.push_morning_brief(payload)        # get_event_handlers 订阅
    |
    |  _format_morning_mail(payload)      # 渲染主题/正文（含信号摘要与日期）
    v
send_email(to=alert_recipient, subject, body_html)
```

- 晨报内容由 `stock_analysis` 生产、email 仅负责投递，职责单一；`plugin.json` 的 `alert_recipient` 配置项指定收件人。
- 调度注册位于 `stock_analysis/__init__.py` 的 `register_jobs`（5 → 6 项），与既有 15:05 批量分析、16:00 信号兑现回算等任务并列。
- 单跑保护：`dispatch_morning_brief()` 使用会话级 `pg_try_advisory_lock` + `finally` 释放，避免多实例重复发射（与 `alert_engine.py` / `batch.py` 同机制）。

### 配置要求

- 邮件服务器参数：见上方「配置说明 / 环境变量配置」（SMTP_HOST/PORT/USER/PASS/FROM）。
- 收件人：`plugin.json` 的 `alert_recipient`（告警/晨报共用的目标邮箱）；未配置则该事件消费方安全跳过。
- 权限：插件需在 `plugin.json` 声明 `events` 权限，才能通过 `get_event_handlers` 注册订阅（受平台权限门约束）。

## 菜单组

- **Users & Support** - Email Management

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。