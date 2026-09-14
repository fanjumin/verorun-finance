# Changelog

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

