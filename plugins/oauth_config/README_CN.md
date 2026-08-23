# OAuth Login Config (oauth_config)

## 概述

OAuth Login Config 是 VeroRun 的第三方登录完整插件，提供 OAuth 2.0 协议的登录、回调与配置管理能力。支持抖音、微信、支付宝、Google、GitHub、Facebook、Telegram 七大主流平台，通过统一的 Provider 抽象层实现多平台快速接入，并提供动态登录方式注册能力。

## 功能特性

- **多平台 OAuth 登录**：支持抖音、微信、支付宝、Google、GitHub、Facebook、Telegram 七个平台
- **统一 Provider 抽象**：基于 `base.py` 的 Provider 基类，标准化 OAuth 流程，新增平台只需实现接口
- **完整 OAuth 流程**：授权请求、回调处理、Token 获取、用户信息拉取
- **动态登录方式**：通过 `get_login_methods` 接口动态注册可用的登录方式，前端自动渲染
- **配置管理后台**：提供可视化的 OAuth 配置管理页面
- **平台特化服务**：为支付宝、抖音、微信提供平台专属服务模块，处理平台特有的 API 差异

## 架构设计

### 数据库策略

插件**无独立数据库**，使用 VeroRun 主库存储 OAuth 配置数据。

### 模块结构

```
┌─────────────────────────────────────────────────────────────────┐
│                        routes/auth.py                            │
│              (OAuth 登录入口 / 回调处理 / 动态登录方式)              │
└─────────────────────────────┬───────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌──────────────────┐ ┌────────────────┐ ┌──────────────────┐
│  routes/admin.py │ │oauth_service.py│ │ get_login_methods│
│  (OAuth 配置管理) │ │ (统一 OAuth 服务)│ │ (动态登录方式注册) │
└──────────────────┘ └──────┬─────────┘ └──────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐
│  providers/       │ │  services/   │ │  models.py       │
│  base.py (基类)   │ │  alipay_     │ │  (OAuth 配置模型)  │
│  facebook.py      │ │  service.py  │ │                  │
│  github.py        │ │  douyin_     │ │                  │
│  google.py        │ │  service.py  │ │                  │
│  telegram.py      │ │  wechat_     │ │                  │
│                   │ │  service.py  │ │                  │
└──────────────────┘ └──────────────┘ └──────────────────┘
```

### OAuth 登录流程

1. **用户点击登录**：前端调用 `get_login_methods` 获取可用登录方式列表
2. **发起授权**：前端重定向到 `oauth/login` Hook，服务端生成授权 URL 并重定向到平台
3. **平台授权**：用户在第三方平台完成授权
4. **回调处理**：平台回调到 `oauth/callback` Hook，服务端交换 Token 并获取用户信息
5. **登录完成**：服务端创建或关联本地用户账号，返回登录凭证

## 目录结构

```
oauth_config/
├── __init__.py              # 插件入口，注册 Hook 与路由
├── models.py                # OAuth 配置数据模型
├── plugin.json              # 插件元数据配置
├── routes/
│   ├── __init__.py
│   ├── admin.py             # 管理后台 OAuth 配置管理路由
│   └── auth.py              # OAuth 登录/回调路由
├── services/
│   ├── __init__.py
│   ├── oauth_service.py     # 统一 OAuth 服务（Provider 路由分发）
│   ├── alipay_service.py    # 支付宝 OAuth 服务
│   ├── douyin_service.py    # 抖音 OAuth 服务
│   └── wechat_service.py    # 微信 OAuth 服务
├── providers/
│   ├── __init__.py
│   ├── base.py              # OAuth Provider 抽象基类
│   ├── facebook.py          # Facebook OAuth Provider
│   ├── github.py            # GitHub OAuth Provider
│   ├── google.py            # Google OAuth Provider
│   └── telegram.py          # Telegram OAuth Provider
├── i18n/
│   ├── en.yml               # 英文国际化
│   └── zh-CN.yml            # 中文国际化
└── templates/
    ├── admin_oauth.html     # 管理后台 OAuth 配置页面
    └── douyin_login.html    # 抖音登录页面模板
```

## 安装与启用

### 安装

插件已包含在 VeroRun 的默认插件目录中，无需额外安装步骤。

### 启用

1. 在 VeroRun 管理后台 "插件管理" 页面中启用 OAuth Login Config 插件
2. 进入 "Security & Compliance" 菜单组，配置各平台的 OAuth 参数（App ID、App Secret 等）
3. OAuth 登录入口将自动在前端登录页面渲染

### 平台配置示例

以 GitHub 为例：

1. 在 GitHub Developer Settings 中创建 OAuth App
2. 设置回调 URL 为 `https://your-domain.com/oauth/callback/github`
3. 将获得的 Client ID 和 Client Secret 填入管理后台配置页面

## 配置说明

在 `plugin.json` 中配置以下参数：

```json
{
  "name": "oauth_config",
  "database": {
    "use_main_db": true
  },
  "providers": {
    "douyin": { "enabled": true },
    "wechat": { "enabled": true },
    "alipay": { "enabled": true },
    "google": { "enabled": true },
    "github": { "enabled": true },
    "facebook": { "enabled": true },
    "telegram": { "enabled": true }
  },
  "callback_base_url": "https://your-domain.com",
  "default_redirect_after_login": "/"
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `database.use_main_db` | 是否使用主库 | `true` |
| `providers.<platform>.enabled` | 是否启用指定平台 | 均为 `true` |
| `callback_base_url` | OAuth 回调基础 URL | 应用域名 |
| `default_redirect_after_login` | 登录后默认跳转路径 | `/` |

## API 端点

### Hook 提供

| Hook 标识符 | 类型 | 说明 |
|-------------|------|------|
| `oauth/provider_list` | Hook | 获取已启用的 OAuth Provider 列表 |
| `oauth/login` | Hook | 发起 OAuth 登录请求，返回授权 URL |
| `oauth/callback` | Hook | 处理 OAuth 回调，完成登录 |

### 动态登录方式

| 接口 | 说明 |
|------|------|
| `get_login_methods()` | 动态注册所有可用登录方式，前端自动渲染登录按钮 |

### 管理后台 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/admin/oauth/` | 列出所有 OAuth Provider 配置 |
| `POST` | `/admin/oauth/` | 创建或更新 Provider 配置 |
| `GET` | `/admin/oauth/<id>` | 查看 Provider 配置详情 |
| `PUT` | `/admin/oauth/<id>` | 更新 Provider 配置 |
| `DELETE` | `/admin/oauth/<id>` | 删除 Provider 配置 |

### 前端回调端点

| 路径 | 说明 |
|------|------|
| `/oauth/login/<provider>` | 发起 OAuth 登录 |
| `/oauth/callback/<provider>` | OAuth 回调处理 |

### 管理后台

| 菜单项 | 分组 | 说明 |
|--------|------|------|
| `OAuth Config` | `Security & Compliance` | OAuth 登录配置管理 |

## 依赖关系

### 内部依赖

- VeroRun 核心框架：Hook 系统、路由注册、用户系统
- 管理后台（auth-center）：菜单渲染
- **dev_accounts** 插件：读取各平台的开发者账号凭证

### 外部依赖

- 各 OAuth 平台的 API 端点（无需额外 SDK）

### 被依赖

- 前端登录页面：通过 `get_login_methods` 获取可用登录方式
- 用户系统：OAuth 登录创建或关联本地用户

## 许可证

本插件为 VeroRun 项目的一部分，遵循 VeroRun 项目的整体许可证协议。