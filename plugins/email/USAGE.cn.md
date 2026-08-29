# 邮件服务 — 使用说明

## 快速上手

1. **配置邮件服务器** — 后台「Users & Support > Email Management > 设置」，或通过环境变量（`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASS`、`IMAP_HOST`、`IMAP_PORT`）配置。
2. **发送邮件** — 撰写新邮件（支持文本 / HTML 正文，附件单封上限 10MB、总上限 50MB）。
3. **收件箱** — 通过 IMAP 读取来信，自动标记已读。
4. **联系人** — 自动合并已发送收件人与联系表单联系人。

## 配置优先级

环境变量 > PluginManager 配置 > system_config > 默认值（阿里企业邮箱）。

## 管理端 API

- `GET /admin/email/inbox` — 收件箱列表
- `POST /admin/email/send` — 发送邮件
- `GET /admin/email/sent` — 已发送列表
- `GET /admin/email/settings`、`POST /admin/email/settings` — 读取 / 保存配置
- `GET /admin/email/attachment/<uid>/<filename>` — 附件下载

## 提示

- 密码等敏感字段在界面中掩码显示。
- 自动识别多编码（UTF-8 / GBK / GB2312 / Latin-1）。
- 提供 `email/send`、`email/send_contact`、`email/get_config` 钩子供其他模块调用。
