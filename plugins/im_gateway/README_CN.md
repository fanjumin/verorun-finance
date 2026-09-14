# IM Gateway (im_gateway)

## 概述

IM Gateway（即时通讯网关）是 VeroRun 平台的统一即时通讯频道管理插件，采用 Adapter 模式为飞书、企业微信、QQ、钉钉、Telegram、LINE 等主流 IM 平台提供统一的配置管理和消息推送接口。插件使用独立的 PostgreSQL schema `im_gateway`，存储频道配置数据。

插件通过抽象基类 `BaseIMAdapter` 定义统一的频道适配器接口契约，每个频道实现独立的 Adapter 子类，支持连接测试、配置字段声明、消息/媒体推送。各频道配置可通过管理后台界面集中管理，secret 类字段自动掩码保护。

## 功能特性

- **多平台统一管理**：飞书、企业微信、QQ、钉钉、Telegram、LINE 六大频道集中配置
- **Adapter 模式**：基于 `BaseIMAdapter` 抽象基类的可扩展适配器架构
- **连接测试**：支持各频道连接测试，验证配置有效性
- **Secret 掩码**：敏感字段（token、secret、key）自动掩码显示，更新时智能合并
- **消息推送**：提供 `send_message` Hook，支持文本消息推送
- **媒体推送**：提供 `push_media` Hook，支持媒体文件推送（子类按需覆写）
- **环境变量兜底**：适配器可声明环境变量兜底配置，供前端参考
- **种子数据**：首次运行自动创建飞书、企业微信等默认频道配置行
- **数据迁移**：支持从主库幂等迁移已有频道配置
- **独立数据库**：使用 PostgreSQL schema `im_gateway`，包含 `channel_configs` 表

## 架构设计

```
+--------------------------------------------------------------+
|                     管理后台界面                               |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      路由层 (routes.py)                        |
|  /admin/channels/*                                            |
|  +-- GET  /                   列出所有频道配置                 |
|  +-- GET  /<channel>          获取单个频道详情                 |
|  +-- PUT  /<channel>          保存/更新频道配置                 |
|  +-- POST /<channel>/test     测试频道连接                    |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                   适配器层 (adapters/)                         |
|  +-- base.py             BaseIMAdapter 抽象基类                |
|  +-- feishu.py           飞书适配器                            |
|  +-- wecom.py            企业微信适配器                         |
|  +-- qq.py               QQ 适配器                             |
|  +-- dingtalk.py         钉钉适配器                             |
|  +-- telegram.py         Telegram 适配器                       |
|  +-- line.py             LINE 适配器                           |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      数据层 (models.py)                        |
|  PG Schema: im_gateway                                        |
|  +-- channel_configs    频道配置表                             |
|      (channel PK, config_json JSON, is_enabled, timestamps)   |
+--------------------------------------------------------------+
```

**Adapter 模式设计**：

```
                    BaseIMAdapter (ABC)
                    +-- channel: str
                    +-- supports_test: bool
                    +-- get_config_fields() -> list
                    +-- test_connection(data) -> (ok, msg)
                    +-- get_env_fallback() -> dict
                    +-- push_media(url, name, mime)
                          |
          +-------+-------+-------+-------+-------+-------+
          |       |       |       |       |       |       |
       Feishu  WeCom    QQ   DingTalk Telegram  LINE
```

## 目录结构

```
im_gateway/
+-- README.md                    # 插件文档
+-- plugin.json                  # 插件元数据配置
+-- __init__.py                  # 插件入口，注册蓝图和 Hook
+-- models.py                    # 数据模型（独立库连接、表创建、种子数据、主库迁移）
+-- routes.py                    # 管理端 API 路由（频道 CRUD、连接测试）
+-- im_gateway.db                # 独立数据库文件（保留用于迁移）
+-- adapters/
|   +-- __init__.py              # 适配器注册与工厂函数
|   +-- base.py                  # BaseIMAdapter 抽象基类
|   +-- feishu.py                # 飞书适配器
|   +-- wecom.py                 # 企业微信适配器
|   +-- qq.py                    # QQ 适配器
|   +-- dingtalk.py              # 钉钉适配器
|   +-- telegram.py              # Telegram 适配器
|   +-- line.py                  # LINE 适配器
+-- i18n/
|   +-- en.yml                   # 英文国际化
|   +-- zh-CN.yml                # 中文国际化
+-- templates/
    +-- admin_imgateway.html     # 管理后台页面模板
```

## 安装与启用

### 前提条件

- VeroRun 平台版本 >= 0.10.0
- 需要接入的 IM 平台的有效凭证（如飞书 App ID/Secret、企业微信 Corp ID/Secret 等）
- PostgreSQL 数据库

### 安装步骤

1. 将 `im_gateway` 目录放置于 `plugins/` 下
2. 确保 `plugin.json` 中 `enabled` 为 `true`
3. 重启应用，插件将自动：
   - 创建 PostgreSQL schema `im_gateway`
   - 初始化 `channel_configs` 表
   - 插入飞书、企业微信等默认频道种子数据
   - 从主库幂等迁移已有频道配置
4. 在管理后台 "System" > "IM Gateway" 中配置各频道参数

## 配置说明

IM Gateway 的配置通过频道级别管理，每个频道独立配置，存储在 `channel_configs` 表的 `config_json` JSON 字段中。各频道支持的配置字段由其对应的 Adapter 子类通过 `get_config_fields()` 方法声明。

**默认频道种子**：

| 频道 | 标识符 | 默认启用 |
|------|--------|----------|
| 飞书 | feishu | 是 |
| 企业微信 | wecom | 是 |
| QQ | qq | 否 |
| 钉钉 | dingtalk | 否 |

Telegram 和 LINE 频道按需创建配置。

## API 端点

### 管理端 API（需要管理员权限）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/channels/` | 列出所有频道配置（secret 值掩码显示） |
| GET | `/admin/channels/<channel>` | 获取单个频道配置详情（含环境变量兜底信息） |
| PUT | `/admin/channels/<channel>` | 保存/更新频道配置（掩码值不覆盖旧值） |
| POST | `/admin/channels/<channel>/test` | 测试频道连接 |

### 频道配置更新示例

```json
{
  "config": {
    "app_id": "cli_xxxxx",
    "app_secret": "new_secret_value"
  },
  "is_enabled": true
}
```

掩码值（含 `●` 字符）不会被覆盖，保留旧值。

## 依赖关系

### 内部依赖

| 依赖项 | 用途 |
|--------|------|
| `plugins._base.db` | 插件基础数据库连接模块 |
| `auth-center.models` | 主库读取（channel_configs 迁移源） |
| `auth-center.routes.admin` | 管理员鉴权（`_require_admin`）和操作日志（`_log`） |

### 外部依赖

| 依赖项 | 用途 |
|--------|------|
| 飞书开放平台 API | 飞书消息推送 |
| 企业微信 API | 企业微信消息推送 |
| QQ 开放平台 API | QQ 消息推送 |
| 钉钉开放平台 API | 钉钉消息推送 |
| Telegram Bot API | Telegram 消息推送 |
| LINE Messaging API | LINE 消息推送 |

### 提供的 Hook

| Hook 标识符 | 说明 |
|-------------|------|
| `im_gateway/send_message` | 通过指定频道发送消息 |
| `im_gateway/push_media` | 通过指定频道推送媒体文件 |

## 菜单组

- **System** - IM Gateway

## 扩展指南

### 添加新的 IM 频道适配器

1. 在 `adapters/` 下创建新的适配器文件（如 `slack.py`）
2. 继承 `adapters.base.BaseIMAdapter` 并实现所有抽象方法
3. 在 `adapters/__init__.py` 的 `get_adapter()` 工厂函数中注册新频道
4. 在 `models.py` 的 `_SEED_CHANNELS` 中添加种子数据

```python
# adapters/slack.py 示例
from .base import BaseIMAdapter

class SlackAdapter(BaseIMAdapter):
    channel = 'slack'
    supports_test = True

    def get_config_fields(self):
        return [
            {'key': 'bot_token', 'label': 'Bot Token', 'type': 'password'},
            {'key': 'channel_id', 'label': 'Channel ID', 'type': 'text'},
        ]

    def test_connection(self, data):
        # 实现 Slack API 连接测试
        ...
```

## 统一社媒网关（v2.0.0 起）

IM Gateway 从 v2.0.0 起在原有 IM 频道网关基础上增量扩展为**统一社媒网关**：统一 IM 消息、社媒内容发布与第三方账号 OAuth 授权。全部新增代码位于插件内新目录，现有 IM 链路零改动。

### 新增能力

| 能力 | 说明 |
|------|------|
| 统一接入层 `gateway` | `gateway.publish(channels, payload)` 一发多平台；`send_message` / `connect` / `callback` / `test` / `get_status` |
| OAuth 第三方登录 | Twitter（OAuth 1.0a）、LinkedIn / Reddit / Weibo / Toutiao / Facebook / Instagram（OAuth 2.0 授权码），`/admin/channels/oauth/connect` |
| 社媒渠道适配器 | `channels/social/` 下 twitter / linkedin / reddit / weibo / toutiao / facebook / instagram / wechat_oa / telegram_channel |
| 加密账号表 | `channel_accounts`（im_gateway schema）整体 Fernet 加密，与旧 `channel_configs` 明文表并存 |
| token 自动刷新 | APScheduler 定时任务每日扫描剩余有效期 <7 天的 token 自动刷新 |
| 统一入站 Webhook | `/webhook/<channel>` 归一化后按订阅者分发（`events.subscribe`） |
| 发布测试入口 | 管理界面「社媒渠道」区块连接按钮 + 账号列表 + 发布测试 |

### 目录结构（新增部分）

```
im_gateway/
+-- gateway.py              # GatewayFacade 统一接入层（频控）
+-- crypto.py               # 凭据 Fernet 加密（复用 ENCRYPTION_KEY）
+-- models_accounts.py      # channel_accounts 加密账号表
+-- events.py               # 入站事件归一化与订阅分发
+-- routes_oauth.py         # OAuth 授权路由（connect/callback/accounts/revoke/refresh/publish-test）
+-- routes_webhook.py       # 统一 Webhook 入口
+-- scheduler.py            # GATEWAY_JOBS（token 自动刷新）
+-- routes_overview.py      # 聚合概览端点（卡片式管理 UI 数据源）
+-- routes_developer.py     # 开发者登录 API Key 管理（Phase 4）
+-- routes_login.py         # 第三方登录提供方管理（Phase 5，方案 A）
+-- routes_third_login.py   # 第三方登录 Web OAuth 闭环（Phase 5，方案 B：login/callback）
+-- channels/
|   +-- base.py             # BaseChannelAdapter 统一渠道抽象基类
|   +-- social/             # 社媒渠道适配器集合（注册到统一注册表）
+-- oauth/                  # OAuth Provider 注册表 + 各平台实现
+-- login/                  # 登录 OAuth 提供方注册表 + code→token 交换实现（Phase 5）
```

### 认证模式

| 模式 | 平台 |
|------|------|
| `oauth` | Twitter / LinkedIn / Reddit / Weibo / Toutiao / Facebook / Instagram |
| `client_credential` | 微信公众号（AppID/AppSecret）/ Telegram bot |
| `manual` | 兜底 |

### 使用示例

```python
from plugins.im_gateway.gateway import gateway

# 一发多平台（payload 支持 title/body/summary/image_url/link_url）
gateway.publish(['twitter', 'linkedin'], {
    'title': 'Hello',
    'body': 'Hello world',
    'image_url': 'https://example.com/cover.jpg',
})

# IM 消息
gateway.send_message(channel='telegram', to='@channel', content='Hello')

# 第三方登录（返回授权 URL）
gateway.connect(platform='twitter')
```

### 平台 App 凭据配置

OAuth 平台的应用凭据（client_id / client_secret / api_key 等）沿用现有频道配置接口写入 `channel_configs`（channel 即平台标识，如 `twitter` / `linkedin`），用户 token 经 OAuth 授权后加密存入 `channel_accounts`。

## 卡片式管理 UI（Phase 1-7）

管理界面 `admin_imgateway.html` 升级为 **5-Tab 卡片式框架**：即时通讯 / 社媒渠道 / 内容发布 / 第三方登录 / 开发者登录。各 tab 统一从聚合端点 `GET /admin/channels/overview` 读取结构化数据渲染（`im` / `social` / `publish` / `login` / `developer` / `miniapp` 六段）。

### 开发者登录（Phase 4）

复用 auth-center `UnifiedAuthService` 统一 API Key 体系（**不建表、不直写库**），管理员可签发 / 吊销 `vr_user_` / `vr_agent_` / `vr_prov_` 三类 Key，前端「开发者登录」tab 提供新建表单（名称 / 类型 / 过期时间）与吊销操作。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/channels/developer/keys/issue` | 签发新 API Key（raw_key 仅返回一次，调用方立即展示） |
| POST | `/admin/channels/developer/keys/revoke/<id>` | 吊销本人 API Key（软删除） |

### 第三方登录（Phase 5，方案 A：插件自包含）

插件内置 WeChat / QQ / Weibo / GitHub / Google 提供方目录（`login/providers.py` 注册表），凭据存入 `im_gateway.login_providers` 表，`client_secret` 掩码显示；前端「第三方登录」tab 提供卡片列表（启用状态 / 是否配置）、凭据配置表单、启用 / 停用切换与授权 URL 测试。

> 说明：方案 A 覆盖「提供方目录 + 凭据管理 + 授权 URL 生成（测试）」。完整登录闭环（回调 → 用户绑定 → JWT）见下方「方案 B」小节，已由 IM Gateway 实现。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/channels/login/providers` | 提供方目录 + 已保存配置（secret 掩码） |
| POST | `/admin/channels/login/providers/save` | 保存提供方凭据（client_secret 留空时保留原值） |
| POST | `/admin/channels/login/providers/enable` | 启用 / 停用提供方 |
| GET | `/admin/channels/login/authorize/<provider>` | 生成授权 URL（测试用） |

提供方注册表接口：`login/providers.py` 中 `list_login_providers()` 返回目录、`get_login_provider_class()` 按 id 查找提供方类。

### 第三方登录闭环（Phase 5，方案 B：插件自包含）

方案 B 在 IM Gateway 内实现 Web 第三方登录的**完整闭环**：授权 → 回调 → 用户绑定 → JWT。登录入口在插件（公开端点），登录内核仍为 auth-center（`session_service.issue_auth_session` 统一签发，含 2FA / 账号禁用检查），与小程序登录同架构；`oauth_config` 插件保留现状、不再扩展，二者提供方目录不同、互不影响。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/oauth/<provider>/login` | 发起授权（state 落库防 CSRF）→ 重定向平台授权页 |
| GET | `/api/v1/oauth/<provider>/callback` | 平台回调 → code 换 token → 用户绑定 → JWT → sso_token cookie → 跳主站 |

要点：
- 平台：WeChat（开放平台扫码）/ QQ / Weibo / GitHub / Google，与方案 A 目录一致（`login/providers.py`）
- 交换实现：`login/exchange.py`（纯标准库 urllib，零新增依赖），返回 `{openid, nickname, avatar, email}`
- 用户绑定：统一走 `im_gateway.login_user_bindings` 表（联邦身份，同 mini_app_builder 的 platform_user_mappings 先例），不扩展主库 `users` 结构；主库用户按唯一 `username`（`<provider>_<md5(openid)前12位>`）get-or-create
- CSRF：state 存 `im_gateway.oauth_login_states`，一次性消费 + 10 分钟过期
- 2FA 拦截：`issue_auth_session` 抛 `TwoFactorRequired` 时跳主站 `?needs_2fa=1&challenge_token=...`
- 回调地址：`<当前请求根>/api/v1/oauth/<provider>/callback`（login 与 callback 自动一致，需在各平台应用后台配置该地址）

### 小程序开发账户（Phase 7，集中 mini_app_builder 登录）

「开发者登录」tab 分上下两块（均采用**卡片网格**，与 im / social / login 各 tab 视觉对齐）：上块为 API Key 管理（Phase 4），下块为**小程序开发账户**（Phase 7）。小程序账户复用 mini_app_builder 的 `dev_accounts` 数据层（**不重复建表、不改其代码**），在 IM Gateway 内完成 5 平台（douyin / toutiao / wechat / telegram / line）凭据的完整 CRUD 与连接测试；mini_app_builder 未启用时接口优雅降级（返回明确错误，不影响其余功能）。

各平台小程序登录方式（供前端展示）：

| 平台 | 登录方式 |
|------|---------|
| douyin / toutiao | `tt.login()` code → code2session → openid → JWT（抖音与头条共用一套凭据） |
| wechat | `wx.login()` code → get_openid_by_code → unionid → JWT |
| telegram | WebApp `initData` → HMAC 验签（bot_token）→ JWT（全局通用） |
| line | LIFF `accessToken` → LINE profile API 校验 → JWT |

> 说明：whatsapp 生成器存在但 dev_accounts 暂无凭据条目，暂不纳入 CRUD；待 mini_app_builder 白名单扩展后再跟进。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/channels/miniapp-accounts` | 账户列表（可选 `?platform=` 过滤，敏感字段掩码） |
| POST | `/admin/channels/miniapp-accounts` | 新增账户 |
| PUT | `/admin/channels/miniapp-accounts/<id>` | 编辑账户（敏感字段留空 = 保持原值） |
| DELETE | `/admin/channels/miniapp-accounts/<id>` | 删除账户 |
| POST | `/admin/channels/miniapp-accounts/<id>/test` | 测试账户连接 |

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。