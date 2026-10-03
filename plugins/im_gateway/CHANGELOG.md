# Changelog

## v3.1.0 — 2026-10-02

IM 底座增强（minor）：8 通道真实出站 + 入站验签闭环。

### Added

- 新增统一出站 HTTP 客户端 `http_client.py`：连接 5s / 读取 15s 超时、禁自动重定向（逐跳复检）、
  `safe_fetch()` SSRF 内网 / 云元数据拦截 + 10MB 限长流式下载、TTL token 缓存。
- 新增 `webhook_signing.py`：入站平台原生验签（telegram secret_token 常量比较 / line HMAC-SHA256 /
  slack v0 HMAC-SHA256 / discord 与 qq Ed25519），统一 **fail-closed**。
- 新增适配器 `slack.py`（`chat.postMessage` + `auth.test`）、`discord.py`
  （`POST /channels/{id}/messages` + `GET /users/@me`），并在 `adapters/__init__.py` 注册。
- `events.normalize_event` 新增 slack / discord / qq 入站事件归一化。
- 恢复 capability `im.message.receive`；`plugin.json` 3.0.0 → **3.1.0**。

### Changed

- 钉钉补齐真实 `send()`：OAPI 工作通知 `topapi/message/corpconversation/asyncsend_v2`
  （`agent_id` + `userid_list`），新增可选 `default_userid` 配置字段。
- QQ 重写为**官方机器人 API**（`api.sgroup.qq.com`）：`app_id` + `client_secret` 换取 access_token 的
  真实连接测试与真实 `send()`；删除「凭据非空即通过」占位测试；\(\*\) 主动消息需平台授权，错误原样透出。
- 企业微信修复**假媒体**：图片写入真实 `md5` 并限 2MB；video/audio/file 不再伪造 `media_id`，
  如实降级为 markdown 下载链接；出站改走 http_client。
- 飞书 `tenant_access_token`、企业微信 / 钉钉 `access_token` 改为进程内 TTL 缓存，避免每次发送重复换取。
- 入站 `/webhook/<channel>` 渠道扩至 telegram / line / slack / discord / qq；**先验签后握手**
  （Discord `PING→PONG`、QQ `op=13` 返回 Ed25519 签名应答、Telegram 返回 200 空响应）；
  移除原有「未配置即放行」，密钥缺失或签名不符一律 401。
- i18n 134 → **159**：补齐 25 个适配器消息键（en / zh-CN 键集保持一致）。
- README 中文 / 英文更新为 8 通道出站 + 入站验签 fail-closed + `http_client` / SSRF 章节。
- telegram 新增 `secret_token`、qq 新增 `bot_secret` 配置字段，验签密钥现可直接在管理界面填写。

### Removed

- 删除 `crypto.py`（Fernet 工具）：社媒职责迁出后，全仓（含跨插件）已无任何引用，随本批清理。

### Known limitations

- **真机连通未验证**：本批仅完成静态与离线向量自测（验签 16/16），实际发送与 webhook 注册需配置真实凭据后自测。
- QQ Ed25519 派生机理按官方约定（`私钥 = sha256(bot_secret)`）实现，上线前建议以真实回调复验。
- 凭据仍为明文存储；`events.subscribe` 暂无内置业务消费者。

## v3.0.0 — 2026-10-02

职责收敛主版本：IM Gateway 此后只负责 **IM 出站通道** 与 **Web 第三方登录**。

### Breaking（职责迁出）

- 社媒内容发布 / 社媒 OAuth 账号 / 凭据账号 / token 定时刷新全部迁回 `social_push`：
  移除 `gateway.publish()` / `connect()`、`/admin/channels/oauth/*`、`channels/social/`、
  `oauth/`、`scheduler.py`、`models_accounts.py`、`routes_oauth.py` 等（批次 B）。
- 小程序开发账户 CRUD / 连接测试、开发者 API Key 管理迁回 `mini_app_builder`：
  移除 `routes_developer.py` 及小程序账户接口（批次 A）。
- `plugin.json` 升至 3.0.0；删除名不副实的 capability `im.message.receive`（入站验签闭环在途）；
  删除失真的 optional 依赖 `tweepy` / `praw`（引用的 `_ensure_deps` 已随社媒迁出）。

### Changed

- 管理 UI 由多 Tab 收敛为 **2 Tab**（即时通讯 / 第三方登录）；社媒 OAuth 控制台整体迁入
  `social_push` 页面（新增独立「账号」「OAuth 连接」Tab），端点 `/admin/channels/oauth/*` 不变。
- `on_uninstall` 不再 `DROP SCHEMA ... CASCADE`，改为仅删除 `channel_configs` /
  `rate_limit_events` 两张 IM 运行表，**显式保留** `login_providers` /
  `login_user_bindings` / `oauth_login_states` 三张登录表。
- `gateway.py` 收敛为纯 IM 门面：`list_channels()` / `test()` / `send_message()` + 跨 worker
  PG 频控；`register_jobs()` 不再含 token 刷新任务。
- i18n 词条 233 → 134：移除随功能迁出的社媒 / developer / 小程序词条，并补齐 14 个此前缺失的
  IM 适配器消息键；en 与 zh-CN 键集保持一致。
- README 中文 / 英文重写为纯 IM + Web 第三方登录，能力口径如实区分
  「完整支持（飞书 / 企业微信 / Telegram / LINE）」与「适配中（钉钉 / QQ）」。

### Retained（保持不变）

- IM 出站：飞书 / 企业微信 / Telegram / LINE 真实发送与连接测试。
- Web 第三方登录：方案 A（提供方凭据管理）+ 方案 B（`/api/v1/oauth/<provider>/login|callback`
  完整闭环），提供方 wechat / qq / weibo / github / google；联邦绑定三表结构不变。

### Known limitations（后续 IM 底座批次处理）

- 钉钉尚无真实 `send`；QQ 连接测试为占位、未接官方机器人 API 与 Ed25519 验签。
- 入站 Webhook 未全面强验签（fail-open），`events.subscribe` 暂无业务消费者。
- `channel_configs` / `login_providers.client_secret` 仍为明文存储；出站 HTTP 待统一超时 / SSRF 拦截。

## v2.1.0 — 2026-08-30

### Changes

- Version bump from v2.0.0

## v2.1.0 — 2026-08-30

### New features

- 第三方登录完整闭环（Phase 5 方案 B）：IM Gateway 提供 Web OAuth 登录
  （wechat / qq / weibo / github / google），新增公开端点
  `GET /api/v1/oauth/<provider>/login` 与 `GET /api/v1/oauth/<provider>/callback`
- 登录内核复用 auth-center `session_service.issue_auth_session`（统一签发 JWT，
  含 2FA / 账号禁用检查）；`oauth_config` 插件保留现状、不再扩展
- 新增 im_gateway 表：`login_user_bindings`（联邦身份绑定，不扩展主库 users 结构）、
  `oauth_login_states`（CSRF state，一次性消费 + 10 分钟过期）
- 新增 `login/exchange.py`：五平台 code→token→userinfo 交换（纯标准库 urllib，
  零新增 pip 依赖）
- 管理端 UI 对齐：开发者登录 tab 由表格改为卡片网格（API Key + 小程序开发账户
  两块），与 im / social / login 各 tab 视觉统一（纯前端，后端 API 不变）

## v2.0.0 — 2026-08-25

### Security / Stability fixes (第三方安全审计后修复)

- P0 修复 `gateway_token_refresh` 定时任务无调度消费方：插件内自建 daemon
  APScheduler（04:00 cron），并用 PG advisory lock 防止多 worker 重复执行
- P0 修复微信公众号渠道 import 路径错误（`auth_center.services` → `services`）
- P1 频控改为 PG 共享计数表，gunicorn 多 worker 下 60s 窗口仍限 20 次/渠道
- P1 tweepy/praw 弱依赖改为插件启用时按需安装（不随系统预装，默认开启、
  失败不阻断；IM_GATEWAY_AUTO_INSTALL_DEPS=0 可关闭）
- P1 新增 POST /admin/channels/oauth/accounts 手动存号接口 + UI 表单
  （client_credential 渠道：telegram_channel / wechat_oa）
- P2 撤销不存在账号返回 404；publish-test 顶层 success 聚合真实结果
- P2 刷新接口区分「平台不支持刷新 / 账号无 refresh token」，失败返回 502 友好提示
- P2 instagram 支持 instagram_app_id/secret，缺省回退 facebook 凭据
- P3 未知渠道 webhook 返回 404；twitter 缺依赖时 connect 返回 400 明确提示

### UI 重构 & i18n 全覆盖

- 社媒网关账号表格新增「剩余天数」列（过期红色 / 7 天内橙色），并新增单账号
  「刷新」按钮（调用 POST /admin/channels/oauth/refresh/<id>，行内显示刷新中/已刷新）
- 连接按钮按「OAuth / 凭据渠道」分组展示，平台名与分组标题全部接入 i18n
- 渠道配置页字段标签全覆盖 i18n：App ID/App Secret/AppKey/AgentId/CorpId/
  Encrypt Key/Verification Token/EncodingAESKey 等；合并历史拆分的
  `{{ _('Enterprise') }} ID`、`{{ _('Callback') }} Token` 等占位字符串
- 新增 43 个 i18n key（en.yml + zh-CN.yml 同步补齐），模板 98 个 `_()` key 覆盖率为 100%
- 修正 `{channel}` 占位 key 的 YAML 引号（`'Enable {channel} Integration'`），保证 yaml.safe_load 可解析
- 适配器层遗留 i18n 统一修复：feishu/wecom/dingtalk/qq/telegram/line 的
  14 处硬编码中文 `_()` key 全部转换为英文 key，并新增 28 个适配器消息词条
  （en/zh 同步），`Connection failed: {}` 等占位 key 已正确加 YAML 引号
- 适配器/网关层硬编码英文 f-string 一并接入 i18n：`Connection failed: {}`、
  `Unsupported channel: {}`、`LINE Connected! Bot: {}`、`Telegram Connected! Bot: {}`、
  `WeCom returned: {} (errcode={})`、`DingTalk returned: {} (errcode={})`、
  `Channel {channel} does not support media push` 等 12 处，新增 7 个词条；
  至此 im_gateway 全插件 `_()`/用户可见字符串零硬编码

## v1.5.2 — 2026-08-22

### Changes

- Version bump from v1.5.1

## v1.5.1 — 2026-08-20

### Changes

- Version bump from v1.4.1

## v1.4.0 — 2026-08-19

### Changes

- Version bump from v1.3.0

