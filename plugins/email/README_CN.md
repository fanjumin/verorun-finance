# Email Service (email)

## 概述

Email Service 是 VeroRun 平台的统一邮件服务插件，提供完整的 SMTP 发信和 IMAP 收信能力，支持收件箱管理、邮件撰写、附件处理和联系人管理。插件使用独立的 PostgreSQL schema `email`，不依赖主库的邮件相关表，实现完全的数据隔离。

插件支持 SMTP/IMAP 协议，默认配置兼容阿里企业邮箱（smtp.qiye.aliyun.com），可通过环境变量或 PluginManager 配置灵活切换邮件服务商。配置来源遵循环境变量 > PluginManager > system_config > 默认值的优先级顺序。

## 功能特性

- **SMTP 发信**：支持纯文本和 HTML 邮件发送，支持 SSL/TLS 加密
- **IMAP 收信**：支持收件箱列表、邮件详情读取、自动标记已读
- **附件处理**：支持附件上传与下载，单附件限制 10MB，总附件限制 50MB
- **联系人管理**：Python 级合并已发送邮件联系人与联系表单联系人
- **多编码支持**：自动处理 UTF-8、GBK、GB2312、Latin-1 等多种编码
- **MIME 支持**：完整支持 multipart/alternative、multipart/mixed 邮件结构
- **发送记录**：单独记录所有已发送邮件到 `email_sent` 表
- **联系表单集成**：`send_contact_email` 方法支持品牌化联系表单邮件
- **独立数据库**：使用 PostgreSQL schema `email`，包含 `email_sent` 表
- **灵活配置**：支持环境变量、PluginManager、system_config 三级配置来源

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
|  +-- /inbox          收件箱列表（IMAP）                        |
|  +-- /read/<uid>     读取邮件详情                              |
|  +-- /send           发送邮件                                  |
|  +-- /sent           已发送邮件列表                            |
|  +-- /contacts       联系人管理（合并多源）                     |
|  +-- /settings       配置读写                                  |
|  +-- /attachment/<uid>/<filename>  附件下载                    |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      服务层 (services.py)                      |
|  +-- _connect_imap()             IMAP 连接                     |
|  +-- fetch_inbox()               收件箱查询                    |
|  +-- read_email()                邮件详情读取                  |
|  +-- get_attachment()            附件提取                      |
|  +-- send_email()                SMTP 发信                     |
|  +-- get_sent_emails()           已发送查询                    |
|  +-- send_contact_email()        联系表单邮件                  |
|  +-- _get_mail_config()          配置合并引擎                  |
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
默认值 (smtp.qiye.aliyun.com:465 / imap.qiye.aliyun.com:993)
```

## 目录结构

```
email/
+-- README.md                    # 插件文档
+-- plugin.json                  # 插件元数据配置
+-- __init__.py                  # 插件入口，注册蓝图和 Hook
+-- models.py                    # 数据模型（PG schema: email 连接、email_sent 表创建）
+-- routes.py                    # 管理端 API 路由（收件箱、发送、联系人、附件等）
+-- services.py                  # 邮件服务核心逻辑（SMTP/IMAP、MIME 编解码、附件处理）
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
| `SMTP_HOST` | SMTP 服务器地址 | smtp.qiye.aliyun.com |
| `SMTP_PORT` | SMTP 端口 | 465 |
| `SMTP_USER` | SMTP 登录账号 | - |
| `SMTP_PASS` | SMTP 登录密码 | - |
| `SMTP_FROM` | 发件人地址 | 同 SMTP_USER |
| `IMAP_HOST` | IMAP 服务器地址 | imap.qiye.aliyun.com |
| `IMAP_PORT` | IMAP 端口 | 993 |
| `CONTACT_TO` | 联系表单收件人邮箱 | - |

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `smtp_host` | string | smtp.qiye.aliyun.com | SMTP 服务器主机名 |
| `smtp_port` | integer | 465 | SMTP 端口（465=SSL，587=STARTTLS） |
| `smtp_user` | string | "" | SMTP 登录用户名 |
| `smtp_pass` | string | "" | SMTP 登录密码（敏感字段，显示时掩码） |
| `smtp_from` | string | "" | 发件人地址 |
| `imap_host` | string | imap.qiye.aliyun.com | IMAP 服务器主机名 |
| `imap_port` | integer | 993 | IMAP 端口 |

## API 端点

### 管理端 API（需要管理员权限）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/email/inbox` | 获取收件箱列表（分页，每页 50 条） |
| GET | `/admin/email/read/<uid>` | 读取指定邮件详情（含正文、附件信息） |
| POST | `/admin/email/send` | 发送邮件（支持纯文本、HTML、附件、回复） |
| GET | `/admin/email/sent` | 获取已发送邮件列表 |
| GET | `/admin/email/contacts` | 获取联系人列表（合并已发送 + 联系表单） |
| GET | `/admin/email/settings` | 获取邮件服务配置（敏感字段掩码） |
| POST | `/admin/email/settings` | 保存邮件服务配置 |
| GET | `/admin/email/attachment/<uid>/<filename>` | 下载邮件附件 |

### 发送邮件请求体示例

```json
{
  "to": "recipient@example.com",
  "subject": "邮件主题",
  "body": "纯文本正文",
  "body_html": "<h1>HTML 正文</h1>",
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

## 菜单组

- **Users & Support** - Email Management

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。