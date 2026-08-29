# Site Domains (site_domains)

## 概述

Site Domains 是 VeroRun 的子域名管理与 Web 服务器配置生成插件。插件管理多站点域名绑定关系，并能自动生成 Nginx 和 Caddy 的服务器配置文件。同时注册 Caddy 的 On-Demand TLS 证书校验端点，实现自动化 HTTPS 证书管理。

版本：**1.1.0**

## 功能特性

- **子域名管理**：管理多站点与域名的绑定关系（通过 auth-center admin_bp 提供 CRUD）
- **Nginx 配置生成**：根据域名绑定关系自动生成 Nginx 虚拟主机配置
- **Caddy 配置生成**：根据域名绑定关系自动生成 Caddyfile 配置
- **Caddy On-Demand TLS 校验**：注册证书颁发校验端点，配合 Caddy 实现自动化 HTTPS
- **轻量级设计**：仅含一个路由模块，CRUD 由 auth-center 统一提供

## 架构设计

### 数据库策略

插件**不使用独立数据库**，直接读取 VeroRun 主库中的 `site_domains` 表。

### 数据表结构

#### site_domains（主库表）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer | 主键 |
| `site_id` | Integer | 关联站点 ID |
| `domain` | String(255) | 域名 |
| `is_primary` | Boolean | 是否为主域名 |
| `ssl_enabled` | Boolean | 是否启用 SSL |
| `status` | String(50) | 域名状态（active / pending / error） |
| `created_at` | DateTime | 创建时间 |
| `updated_at` | DateTime | 更新时间 |

### 模块结构

```
┌─────────────────────────────────────────────────────────────────┐
│                       routes.py                                  │
│          (Caddy On-Demand TLS 校验端点 / 配置生成)                  │
└─────────────────────────────┬───────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌──────────────────┐ ┌────────────────┐ ┌──────────────────┐
│ Caddy On-Demand  │ │ Nginx 配置     │ │ auth-center      │
│ TLS 校验端点      │ │ 自动生成       │ │ admin_bp (CRUD)  │
└──────────────────┘ └────────────────┘ └──────────────────┘
```

### 数据流

1. **域名管理**：管理员通过 auth-center 的 admin_bp 进行域名的 CRUD 操作，数据写入主库 `site_domains` 表
2. **配置生成**：本插件的 `routes.py` 提供配置生成接口，从主库读取域名绑定关系，生成 Nginx/Caddy 配置
3. **TLS 校验**：当 Caddy 处理 On-Demand TLS 证书请求时，回调本插件注册的校验端点，验证域名合法性

## 目录结构

```
site_domains/
├── __init__.py                  # 插件入口，注册路由与菜单
├── routes.py                    # Caddy 校验端点 + 配置生成接口
├── plugin.json                  # 插件元数据配置
└── templates/
    └── admin_sitedomains.html   # 管理后台域名管理页面模板
```

## 安装与启用

### 安装

插件已包含在 VeroRun 的默认插件目录中，无需额外安装步骤。

### 启用

1. 在 VeroRun 管理后台 "插件管理" 页面中启用 Site Domains 插件
2. 管理后台 "System" 菜单组将出现域名管理入口
3. 如需使用 Caddy On-Demand TLS，需在 Caddyfile 中配置校验端点：

```caddyfile
*.your-domain.com {
    tls {
        on_demand
    }
    reverse_proxy localhost:5000
}
```

## 配置说明

在 `plugin.json` 中配置以下参数：

```json
{
  "name": "site_domains",
  "version": "0.1.0",
  "database": {
    "use_main_db": true,
    "table": "site_domains"
  },
  "caddy": {
    "on_demand_tls_endpoint": "/api/site_domains/caddy/verify",
    "allowed_domains_pattern": "*.your-domain.com"
  },
  "nginx": {
    "config_output_path": "/etc/nginx/sites-enabled/",
    "template": "default"
  }
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `database.use_main_db` | 是否使用主库 | `true` |
| `database.table` | 主库中的域名表名 | `site_domains` |
| `caddy.on_demand_tls_endpoint` | Caddy TLS 校验端点路径 | `/api/site_domains/caddy/verify` |
| `caddy.allowed_domains_pattern` | 允许的域名模式 | 无 |
| `nginx.config_output_path` | Nginx 配置输出目录 | `/etc/nginx/sites-enabled/` |

## API 端点

### Caddy On-Demand TLS 校验

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/site_domains/caddy/verify?domain=<domain>` | Caddy 证书颁发前域名校验 |

### 配置生成

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/site_domains/generate/nginx` | 生成 Nginx 配置 |
| `POST` | `/api/site_domains/generate/caddy` | 生成 Caddy 配置 |

### 管理后台

| 菜单项 | 分组 | 说明 |
|--------|------|------|
| `Site Domains` | `System` | 域名管理页面 |

## 依赖关系

### 内部依赖

- VeroRun 核心框架：路由注册、主库 ORM
- 管理后台（auth-center）：提供 admin_bp 的 CRUD 能力

### 外部依赖

- **Caddy**：Web 服务器（On-Demand TLS 功能需要 Caddy）
- **Nginx**：Web 服务器（配置生成功能需要 Nginx）

### 被依赖

- 站点系统：多站点域名绑定能力
- Caddy 服务器：On-Demand TLS 证书校验

## 许可证

本插件为 VeroRun 项目的一部分，遵循 VeroRun 项目的整体许可证协议。