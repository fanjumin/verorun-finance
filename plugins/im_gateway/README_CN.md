# IM Gateway (im_gateway)

> 版本：**v3.1.0**（职责收敛 + IM 底座增强版本：8 通道出站 · 入站验签 fail-closed · 统一 HTTP 客户端）

## 概述

IM Gateway 是 VeroRun 平台的**即时通讯通道网关**插件，基于 Adapter 模式统一管理 IM 频道的凭据配置、连接测试与出站消息/媒体投递，并内置一套完整的 **Web 第三方登录**（OAuth 联邦登录）能力。插件使用独立的 PostgreSQL schema `im_gateway`。

v3.0.0 完成职责收敛：**社媒内容发布 / 社媒 OAuth 账号 / 定时刷新**已迁回 `social_push` 插件，**小程序登录 / 开发者 API Key / 小程序开发账户**已迁回 `mini_app_builder` 插件。本插件此后只负责「IM 出站通道」与「Web 第三方登录」两件事。

v3.1.0 完成 IM 底座增强：新增统一 `http_client`（连接 5s / 读取 15s 超时、SSRF 内网拦截、限长下载、进程内 token 缓存）、新增 **Slack / Discord** 适配器、钉钉与 QQ 接入**官方真实 API**、入站 Webhook 改为**各平台原生验签且 fail-closed**。

## 能力现状（如实标注）

| 频道 | 标识 | 出站文本消息 | 连接测试 | 媒体推送 | 入站验签 | 说明 |
|------|------|:---:|:---:|:---:|:---:|------|
| 飞书 | `feishu` | ✅ | ✅ 真实调用 | ✅ | 共享密钥 | 默认启用 |
| 企业微信 | `wecom` | ✅ | ✅ 真实调用 | ✅（群机器人 / 应用消息） | 共享密钥 | 默认启用 |
| 钉钉 | `dingtalk` | ✅ | ✅ 真实调用 | — | 共享密钥 | 出站走 OAPI 工作通知 |
| QQ | `qq` | ✅ | ✅ 真实调用 | — | ✅ Ed25519 | 官方机器人 API（`api.sgroup.qq.com`） |
| Telegram | `telegram` | ✅ | ✅ 真实调用 | — | ✅ secret_token | 按需配置 |
| LINE | `line` | ✅ | ✅ 真实调用 | — | ✅ HMAC-SHA256 | 按需配置 |
| Slack | `slack` | ✅ | ✅ 真实调用 | — | ✅ v0 HMAC-SHA256 | v3.1.0 新增 |
| Discord | `discord` | ✅ | ✅ 真实调用 | — | ✅ Ed25519 | v3.1.0 新增 |

> Slack / Discord 通过 `adapters/__init__.py` 注册后**自动出现**在管理后台卡片（概览由适配器注册表驱动，字段由 `get_config_fields()` 动态渲染），无需改模板或建表。

### HTTP 出站与 SSRF 防护

所有出站请求统一走 `http_client.py`：

- **统一超时**：连接 5s / 读取 15s，杜绝裸 `urlopen` 无超时阻塞。
- **SSRF 拦截**：`safe_fetch()` 仅用于**外部输入 URL**（如媒体 `file_url`）——拒绝私网 / 回环 / 链路本地 / 云元数据（`169.254.169.254`）/ 保留段（含 IPv6 与 IPv4-mapped），仅允许 http/https 且端口限 80/443，禁带凭据，重定向逐跳复检，下载限长 10MB。
- **Token 缓存**：飞书 `tenant_access_token`、企业微信 / 钉钉 access_token 按 app 维度进程内 TTL 缓存，避免每次发送重复换取。
- 平台写死的 API 域名（`open.feishu.cn` 等）只做统一超时，不做私网拦截。

## 功能特性

- **多频道统一管理**：频道凭据集中配置，secret 类字段自动掩码，更新留空时保留旧值。
- **Adapter 模式**：抽象基类 `BaseIMAdapter` 定义统一契约，新增频道只需实现子类并注册。
- **统一出站门面**：`gateway.send_message()` / `gateway.test()` / `gateway.list_channels()`。
- **跨 worker 频控**：基于 PG `rate_limit_events` 表计数，多 gunicorn worker 下仍按「60 秒 / 渠道 / 20 次」限流。
- **媒体推送**：`push_media()` 供主系统媒体库调用（当前飞书 / 企业微信）。
- **统一 HTTP 出站**：`http_client.py` 统一超时 / 禁自动重定向 / token 缓存，媒体下载走 `safe_fetch()`（SSRF 拦截 + 限长）。
- **入站验签 fail-closed**：`/webhook/<channel>` 对 telegram / line / slack / discord / qq 做**平台原生验签**，密钥缺失或签名不符一律 401，绝不"未配置即放行"。
- **内核事件联动**：订阅 `stock.alert.triggered` 事件，按告警自身勾选的 `im` 渠道推送。
- **Web 第三方登录闭环**：授权 → 回调 → code 换 token → 联邦用户绑定 → JWT 签发，登录内核复用 auth-center。
- **独立 schema**：`im_gateway`，共 5 张表（见下）；卸载时只清 IM 运行表、保留登录数据。

## 架构

```
管理后台 admin_imgateway.html（2 个 Tab：即时通讯 / 第三方登录）
        │
        ▼
路由层（5 个蓝图）
  routes.py            /admin/channels/*        IM 频道 CRUD + 连接测试
  routes_overview.py   /admin/channels/overview  聚合概览（卡片 UI 数据源）
  routes_login.py      /admin/channels/login/*   登录提供方凭据管理（方案 A）
  routes_third_login.py /api/v1/oauth/*          Web OAuth 登录闭环（方案 B）
  routes_webhook.py    /webhook/<channel>        入站 Webhook（原生验签 fail-closed → 归一化 → 分发）
        │
        ▼
入站验签 webhook_signing.py
  telegram(secret_token) / line(HMAC) / slack(v0 HMAC) / discord(Ed25519) / qq(Ed25519)
        │
        ▼
适配器层 adapters/
  base.py(BaseIMAdapter)
  feishu / wecom / telegram / line / slack / discord / dingtalk / qq（均可真实出站）
        │
        ▼
出站底座 http_client.py（统一超时 · SSRF 拦截 · TTL token 缓存）
        │
        ▼
数据层 models.py —— PG Schema: im_gateway
  channel_configs        IM 频道凭据（config_json）
  rate_limit_events      跨 worker 频控计数
  login_providers        第三方登录提供方配置
  login_user_bindings    联邦身份 ↔ 主库用户绑定
  oauth_login_states     OAuth state（CSRF，一次性 + 10 分钟过期）
```

## 目录结构

```
im_gateway/
├── plugin.json                 插件元数据（v3.1.0，capabilities: im.channel.list / im.message.send / im.message.receive / im.account.bind）
├── __init__.py                 插件入口：蓝图注册、事件订阅、dashboard、卸载清理
├── models.py                   schema/5 张表、默认频道种子、主库频道配置幂等迁移
├── gateway.py                  GatewayFacade：IM 出站门面 + PG 频控（社媒 publish 已移除）
├── http_client.py              统一出站 HTTP（超时 5s/15s · SSRF 拦截 · 限长下载 · TTL token 缓存）
├── webhook_signing.py          入站平台原生验签（telegram/line/slack/discord/qq，fail-closed）
├── routes.py                   IM 频道 CRUD + 连接测试
├── routes_overview.py          聚合概览端点
├── routes_webhook.py           入站 Webhook 入口（验签 → 归一化 → 分发）
├── routes_login.py             第三方登录提供方管理（方案 A）
├── routes_third_login.py       Web OAuth 登录闭环（方案 B）
├── events.py                   入站事件归一化 / 分发
├── adapters/                   IM 适配器注册表与各频道实现
│   ├── __init__.py / base.py
│   ├── feishu.py / wecom.py / telegram.py / line.py
│   └── slack.py / discord.py / dingtalk.py / qq.py
├── login/                      Web 登录提供方注册表 + code→token 交换（纯标准库 urllib）
│   ├── providers.py / exchange.py
├── i18n/                       en.yml / zh-CN.yml
└── templates/admin_imgateway.html
```

## 安装与启用

1. 插件随 `plugins/im_gateway` 目录分发，置于 `plugins/` 下。
2. 在管理后台「插件管理」启用；启用时幂等创建 schema 与 5 张表，并写入默认频道种子、尝试从主库迁移历史频道配置。
3. 在「System → IM Gateway」中配置飞书 / 企业微信等频道凭据并做连接测试。

默认频道种子：

| 频道 | 标识 | 默认启用 |
|------|------|:---:|
| 飞书 | `feishu` | 是 |
| 企业微信 | `wecom` | 是 |
| QQ | `qq` | 否 |
| 钉钉 | `dingtalk` | 否 |

Telegram / LINE 按需创建配置。

## API 端点

### IM 频道管理（需管理员）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/channels/` | 频道列表（secret 掩码） |
| GET | `/admin/channels/<channel>` | 频道详情（含环境变量兜底信息） |
| PUT | `/admin/channels/<channel>` | 保存 / 更新（掩码值不覆盖旧值） |
| POST | `/admin/channels/<channel>/test` | 连接测试 |
| GET | `/admin/channels/overview` | 聚合概览（`im` / `login` 两段） |

### 第三方登录提供方管理（方案 A，需管理员）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/channels/login/providers` | 提供方目录 + 已存配置（secret 掩码） |
| POST | `/admin/channels/login/providers/save` | 保存凭据（client_secret 留空保留原值） |
| POST | `/admin/channels/login/providers/enable` | 启用 / 停用提供方 |
| GET | `/admin/channels/login/authorize/<provider>` | 生成授权 URL（测试用） |

内置提供方目录：`wechat`（开放平台扫码）/ `qq` / `weibo` / `github` / `google`，见 `login/providers.py`。

### Web 第三方登录闭环（方案 B，公开端点）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/oauth/<provider>/login` | 发起授权（state 落库防 CSRF）→ 跳平台授权页 |
| GET | `/api/v1/oauth/<provider>/callback` | 回调 → code 换 token → 用户绑定 → JWT → sso_token cookie → 跳主站 |

要点：

- 交换实现 `login/exchange.py` 为**纯标准库 urllib**，零新增 pip 依赖，返回 `{openid, nickname, avatar, email}`。
- 联邦身份存 `login_user_bindings`，不扩展主库 `users` 结构；主库用户按唯一 `username`（`<provider>_<md5(openid)前12位>`）get-or-create。
- state 存 `oauth_login_states`，一次性消费 + 10 分钟过期。
- 登录内核复用 auth-center `session_service.issue_auth_session`（统一签发，含 2FA / 账号禁用检查）；2FA 时跳主站 `?needs_2fa=1&challenge_token=...`。
- 回调地址为 `<请求根>/api/v1/oauth/<provider>/callback`，需在各平台应用后台登记。

### 入站 Webhook（原生验签 · fail-closed）

`POST /webhook/<channel>`：先**验签**（`webhook_signing.verify`），再归一化（`events.normalize_event`）并按订阅分发（`events.dispatch_event`）。

支持渠道：`telegram` / `line` / `slack` / `discord` / `qq`（其他渠道 404）。各平台机制与密钥来源：

| 渠道 | 请求头 | 密钥来源（`channel_configs`） |
|------|--------|------|
| telegram | `X-Telegram-Bot-Api-Secret-Token` 常量比较 | `secret_token`（缺失时回退 env `IM_GATEWAY_WEBHOOK_SECRET`） |
| line | `X-Line-Signature` = base64(HMAC-SHA256(body)) | `channel_secret` |
| slack | `X-Slack-Signature` = `v0=…`，时间戳 ±5 分钟 | `signing_secret` |
| discord | `X-Signature-Ed25519` 对 (timestamp+body) 验签 | `application_public_key` |
| qq | `X-Signature-Ed25519` 对 (timestamp+body) 验签 | `bot_secret`（回退 `client_secret`） |

**fail-closed**：密钥缺失或签名不匹配一律 **401**，绝不"未配置即放行"。协议握手在验签后进行：Discord `PING(type=1) → PONG`、QQ `op=13` 回调地址验证返回 Ed25519 签名应答、Telegram 返回 200 空响应。

## 对外 Python 接口

```python
from plugins.im_gateway.gateway import gateway

gateway.send_message(channel='telegram', to='<chat_id>', content='Hello')  # IM 出站
gateway.test(channel='feishu', data={...})                                 # 连接测试
gateway.list_channels()                                                    # 渠道枚举
```

> 社媒「一发多平台」的 `gateway.publish()` / OAuth `connect()` 已在 v3.0.0 移除，相关能力请使用 `social_push` 插件。

## 内核事件集成

插件通过 `get_event_handlers()` 订阅 `stock.alert.triggered`：仅当该条告警的 `channels` 含 `im` 时，才向所有「已启用」IM 频道投递格式化文本；频道未配置或发送失败只记日志，不中断事件链。

## 扩展指南：新增 IM 适配器

1. 在 `adapters/` 新建文件（如 `slack.py`），继承 `BaseIMAdapter`，实现 `get_config_fields()` / `test_connection()` / `send()`（媒体按需覆写 `push_media()`）。
2. 在 `adapters/__init__.py` 的 `_ADAPTERS` 注册。
3. 如需默认行，在 `models.py` 的 `_SEED_CHANNELS` 增加。
4. 补齐 `i18n` 中适配器抛错词条（en / zh-CN 键集保持一致）。

## 卸载行为

`on_uninstall` 只 `DROP TABLE` 两张 IM 运行表（`rate_limit_events`、`channel_configs`），**显式保留**三张登录表（`login_providers` / `login_user_bindings` / `oauth_login_states`）——联邦登录绑定属于用户资产，卸载 IM 频道不应连带清除。

## 已知限制与在途项

- **凭据仍为明文存储**：`channel_configs.config_json` 与 `login_providers.client_secret`（历史设计）明文落库，凭据加密改造仍在规划中。
- **QQ Ed25519 派生机理**：按官方约定 `私钥 = sha256(bot_secret) → Ed25519` 实现（离线向量自测通过），上线前建议以真实回调复验。
- **QQ 主动消息**（无 `msg_id`）需平台授权，未授权时平台报错原样透出，不做降级伪装。
- **企业微信媒体**：群机器人本身不支持 file/video/audio 消息，`make_media` 不支持的类型如实降级为 markdown 下载链接，**不再伪造 media_id**。
- **`events.subscribe` 目前无业务消费者**：入站事件已能验签、归一化并分发，但尚无内置订阅方（供 chatbot 等按需订阅）。
- **真机连通未验证**：本批次仅完成静态与离线向量验证，实际发送 / webhook 注册需配置真实凭据后自测。

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一许可证协议。
