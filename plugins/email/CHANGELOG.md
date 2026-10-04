# Changelog

## v1.8.0 — 2026-10-01

### Added

- **企业级安全管控**：新增附件扩展名黑名单（`blocked_attachment_exts`）、每日发信配额（`daily_send_quota`，按 `email_sent` 记录计数）、收件人域名白名单（`recipient_domain_allowlist`），以及私网 SMTP/IMAP 探测开关（`allow_private_targets`，默认禁止探测环回/私网目标）。
- `email/send` / `email/send_contact` / `email/get_config` 三个 hook 除 MCP 工具面之外，同时作为插件间同步动作面（inter-plugin synchronous action surface）提供。

### Changed

- `plugin.json` `version` → `1.8.0`。

## v1.7.0 — 2026-09-30

### Added

- **CC / BCC 抄送密送**：`send_email()` 与 `POST /admin/email/send` 新增 `cc` / `bcc` 参数；BCC 仅加入发送目标、不写入 MIME 头（标准密送语义）。
- **草稿箱**：新增 `email_drafts` 表（独立 PG schema `email`）与 `save_draft()` / `list_drafts()` / `delete_draft()` 服务函数；`GET/POST /admin/email/drafts`、`DELETE /admin/email/drafts/<id>` 路由；撰写表单支持「存草稿」、草稿面板编辑/删除，发信成功后自动删除关联草稿。
- **收件箱搜索**：`fetch_inbox()` 新增 `keyword` 参数，优先服务端 IMAP SEARCH（FROM/SUBJECT/TEXT 并集，CHARSET UTF-8），失败自动降级为客户端最近 500 封内存过滤；`GET /admin/email/inbox?q=` 已接线。
- **信箱批量管理**：`list_folders()` / `delete_emails()` / `mark_read()` / `move_emails()` 服务函数；`GET /admin/email/folders`、`POST /admin/email/batch` 路由；收件箱表格支持多选（删除需二次确认、标记已读/未读、移动到 IMAP 文件夹）。
- **转发**：收件箱新增「转发」按钮（主题加 `Fwd:` 前缀，原文以 blockquote 形式带入正文）。
- **Agent hooks 接线为 MCP 工具**：新增 `plugins/email/email_mcp_server.py`（stdio JSON-RPC 2.0），暴露 `email_send` / `email_send_contact` / `email_get_config`；`agent_matrix` 内核启动时经 `register_system_mcp_server(plugin_id='email')` 注册，配置快照经 `_email_mcp_env()` 注入 `EMAIL_MCP_*` 环境变量（含域名推导结果），并清理 DB 中 `sync_plugin_mcp()` 时代残留记录。
- `fetch_inbox()` / `read_email()` 与全部批量操作统一改用 IMAP **UID 语义**（`imap.uid(...)`）。

### Changed

- `plugin.json` `version` → `1.7.0`；**清理虚假 capabilities**：移除 `email.schedule` / `email.track`（无对应实现），保留 `email.send` / `email.template`。
- `send_contact_email()` 新增 `cfg` 透传参数（MCP 子进程预构造配置）。
- 修复 IMAP 批量操作中 `_`（i18n 翻译函数）被 `imap.uid(...)` 返回值遮蔽导致的 `TypeError`（`status, _ = ...` → `status, _resp = ...`）。
- 收件箱 UID 搜索需传 UTF-8 bytes（`kw.encode("utf-8")`），规避 imaplib 对 str 的 ASCII encode 抛异常。

## v1.6.0 — 2026-09-30

### Added

- **快捷配置（邮件服务器自动预置）**：新增 `mail_providers.py` 单一数据源，预置 22 家国内外常用邮件服务商（含 SMTP/IMAP 主机、端口、SSL 标记与认证提示）。
- `GET /admin/email/settings` 新增返回 `providers`（服务商清单）与 `suggestion`（按已填账号自动识别到的服务商）。
- `POST /admin/email/settings` 新增保存端域名推导：账号匹配预置服务商时自动补全空白的服务器/端口字段。
- `services.py` 配置合并引擎新增域名自动推导层（仅填充空字段，不覆盖显式配置）。
- 设置页新增服务商下拉框与「自动识别账号」按钮。
- `GET /admin/email/providers` 改为从统一数据源输出（保持旧字段 `ssl` 兼容）。

### Changed

- 修正 USAGE.cn.md 中过时的「默认值（阿里企业邮箱）」描述为「默认值均为空，支持域名自动推导」。

## v1.5.2 — 2026-08-22

### Changes

- Version bump from v1.5.1

## v1.5.1 — 2026-08-20

### Changes

- Version bump from v1.4.1

## v1.4.0 — 2026-08-19

### Changes

- Version bump from v1.3.0

