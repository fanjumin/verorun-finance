# 邮件服务 — 使用说明

## 快速上手

1. **配置邮件服务器** — 后台「Users & Support > Email Management > 设置」。
   - **快捷方式（推荐）**：选择邮件服务商下拉框，或填写邮箱账号后点击「自动识别账号」，SMTP/IMAP 服务器与端口自动填充；再填写密码（或授权码）即可。
   - 也支持通过环境变量（`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASS`、`IMAP_HOST`、`IMAP_PORT`）配置。
2. **发送邮件** — 撰写新邮件（支持文本 / HTML 正文、CC/BCC 抄送密送、附件单封上限 10MB、总上限 50MB）；撰写中途可「存草稿」，稍后在草稿箱继续编辑。
3. **收件箱** — 通过 IMAP 读取来信，自动标记已读；顶部搜索框可按发件人 / 主题 / 正文关键词过滤。
4. **批量管理** — 勾选多封邮件后可批量标记已读 / 未读、移动到指定文件夹或删除（删除需二次确认）。
5. **转发** — 邮件行的「转发」按钮一键带入原文并加 `Fwd:` 前缀。
6. **联系人** — 自动合并已发送收件人与联系表单联系人。

## 配置优先级

环境变量 > PluginManager 配置 > system_config > 默认值（均为空） > 域名自动推导（预置服务商清单）。

## 管理端 API

- `GET /admin/email/inbox` — 收件箱列表（`?q=` 搜索）
- `POST /admin/email/send` — 发送邮件（`cc` / `bcc` 可选）
- `GET /admin/email/drafts`、`POST /admin/email/drafts`、`DELETE /admin/email/drafts/<id>` — 草稿箱
- `GET /admin/email/folders` — 邮箱文件夹列表
- `POST /admin/email/batch` — 批量操作（`delete` / `read` / `unread` / `move`）
- `GET /admin/email/sent` — 已发送列表
- `GET /admin/email/settings`、`POST /admin/email/settings` — 读取 / 保存配置
- `GET /admin/email/providers` — 预置邮件服务商清单（22 家，含 SMTP/IMAP 参数）
- `GET /admin/email/test-config` — 连通性与认证测试
- `GET /admin/email/attachment/<uid>/<filename>` — 附件下载

## Agent / MCP 工具（v1.7.0）

插件将三个 Agent hooks 接线为系统级 MCP 工具（命令空间 `mcp__email__*`），Agent 可直接调用：

| 工具 | 用途 | 必填参数 |
|------|------|----------|
| `email_send` | 发邮件（支持 cc/bcc/附件） | `to`、`subject`、`body` |
| `email_send_contact` | 联系表单邮件（发至站点联系邮箱） | `name`、`email`、`subject`、`message` |
| `email_get_config` | 查看当前邮件配置（密码脱敏） | — |

配置由主进程在启动系统 MCP 时以 `EMAIL_MCP_*` 环境变量快照注入（含域名推导结果），子进程无数据库依赖。

## 提示

- 密码等敏感字段在界面中掩码显示。
- 自动识别多编码（UTF-8 / GBK / GB2312 / Latin-1）。
- 批量删除与移动基于 IMAP UID 语义，删除后立即 EXPUNGE 生效。
- 提供 `email/send`、`email/send_contact`、`email/get_config` 钩子供其他模块调用。