# Social Push (social_push)

## 概述

Social Push（社媒发布）是 VeroRun 平台的多平台社交媒体内容发布插件，支持微信公众号、微博、今日头条、Twitter/X、LinkedIn、Reddit、Telegram Channel 七大平台的内容发布。插件使用独立的 PostgreSQL schema `social_push`，存储发布历史记录，核心发布逻辑委托给 auth-center 的对应服务层处理。

插件采用 Provider 模式管理国际平台（Twitter、LinkedIn、Reddit、Telegram），国内平台（微信、微博、头条）直接复用 auth-center 的推送服务。AI 文案生成和 AI 配图走全站公共 LLM 服务（`ai_content_generator`），与发布渠道分离。

## 功能特性

- **七大平台支持**：微信公众号、微博、今日头条、Twitter/X、LinkedIn、Reddit、Telegram Channel
- **Provider 模式**：国际平台通过 `providers/` 目录下的适配器扩展，统一接口
- **AI 文案生成**：基于通义千问的 AI 内容生成（走公共 LLM 服务，非插件私有）
- **AI 配图生成**：基于通义万相的 AI 封面图生成（走公共 LLM 服务）
- **发布历史管理**：完整的发布日志记录，支持按平台筛选和分页查询
- **CMS 文章导入**：支持从 CMS 已发布文章导入到社媒编辑器
- **发布状态查询**：支持微信发布状态回查
- **配置检测**：自动检测各平台配置状态，区分已配置和未配置平台
- **市场感知**：根据 `DEPLOY_MARKET` 环境变量区分国内/国际市场，动态显示可用平台
- **独立数据库**：使用 PostgreSQL schema `social_push`，包含 `social_push_logs` 表
- **数据迁移**：首次启动自动从主库幂等迁移历史发布记录

## 架构设计

```
+--------------------------------------------------------------+
|                     管理后台界面                               |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      路由层 (routes.py)                        |
|  /admin/social/*                                              |
|  +-- /check-config     平台配置检测                            |
|  +-- /content-types    内容类型查询                            |
|  +-- /generate         AI 文案生成                             |
|  +-- /generate-image   AI 配图生成                             |
|  +-- /publish          多平台发布                              |
|  +-- /publish-status   微信发布状态查询                         |
|  +-- /history          发布历史查询                            |
|  +-- /import-from-cms  CMS 文章导入                           |
|  +-- /history/<id>     删除发布记录                            |
+--------------------------------------------------------------+
                              |
              +---------------+---------------+
              |               |               |
              v               v               v
+------------------+ +------------------+ +------------------+
| _publish_wechat  | | _publish_weibo   | | _publish_toutiao |
| (微信草稿+发布)  | | (微博发布)       | | (头条发布)       |
+------------------+ +------------------+ +------------------+
                              |
                              v
              +-------------------------------+
              | _publish_via_provider()       |
              | (Twitter/LinkedIn/Reddit/     |
              |  Telegram 统一入口)           |
              +-------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                   Provider 层 (providers/)                    |
|  +-- base.py               Provider 抽象基类                  |
|  +-- twitter.py            Twitter/X Provider                |
|  +-- linkedin.py           LinkedIn Provider                 |
|  +-- reddit.py             Reddit Provider                   |
|  +-- telegram_channel.py   Telegram Channel Provider         |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      数据层 (models.py)                        |
|  PG Schema: social_push                                       |
|  +-- social_push_logs    发布历史记录表                        |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                  auth-center 服务层                            |
|  +-- services/wechat_push_service.py   微信推送服务            |
|  +-- services/weibo_service.py         微博推送服务            |
|  +-- services/toutiao_service.py       头条推送服务            |
|  +-- services/ai_content_generator.py  公共 AI 服务            |
+--------------------------------------------------------------+
```

**AI 能力与发布渠道分离设计**：

```
创作工具 (ai_capabilities)          发布渠道 (platforms)
+---------------------------+      +---------------------------+
| AI 文案生成 (通义千问)     |      | 微信公众号 (wechat)       |
| AI 配图生成 (通义万相)     |      | 微博 (weibo)              |
| 走公共 LLM 服务            |      | 今日头条 (toutiao)        |
| (ai_content_generator)    |      | Twitter/X (twitter)       |
+---------------------------+      | LinkedIn (linkedin)       |
                                   | Reddit (reddit)           |
                                   | Telegram (telegram)       |
                                   +---------------------------+
```

## 目录结构

```
social_push/
+-- README.md                    # 插件文档（英文）
+-- README_CN.md                 # 插件文档（中文）
+-- plugin.json                  # 插件元数据配置
+-- __init__.py                  # 插件入口，注册蓝图和 Hook
+-- models.py                    # 数据模型（独立库连接、表创建、主库迁移）
+-- routes.py                    # 管理端 API 路由（配置检测、AI 生成、发布、历史等）
+-- providers/
|   +-- __init__.py              # Provider 注册与工厂函数
|   +-- base.py                  # Provider 抽象基类
|   +-- twitter.py               # Twitter/X Provider
|   +-- linkedin.py              # LinkedIn Provider
|   +-- reddit.py                # Reddit Provider
|   +-- telegram_channel.py      # Telegram Channel Provider
+-- i18n/
|   +-- en.yml                   # 英文国际化
|   +-- zh-CN.yml                # 中文国际化
+-- templates/
    +-- admin_socialpush.html    # 管理后台页面模板
```

## 安装与启用

### 前提条件

- VeroRun 平台版本 >= 0.10.0
- 目标平台的 API 凭证（如微信公众号 AppID/AppSecret、微博 App Key 等）
- DashScope API Key（用于 AI 文案和配图生成）
- PostgreSQL 数据库

### 安装步骤

1. 将 `social_push` 目录放置于 `plugins/` 下
2. 重启应用，插件将自动注册并在启动时：
   - 创建 PostgreSQL schema `social_push`
   - 初始化 `social_push_logs` 表
   - 从主库幂等迁移历史发布记录
3. 在管理后台 "Publishing" > "Social Push" 中配置各平台参数
4. 使用 `check-config` 端点验证各平台配置状态

### 环境变量

| 环境变量 | 说明 |
|----------|------|
| `DEPLOY_MARKET` | 部署市场：`cn`（国内，仅显示国内平台）/ `intl`（国际，显示全部平台） |

## 配置说明

本插件通过各平台独立的配置进行管理，配置项存储在 auth-center 的 `system_config` 表中。

### 国内平台配置

| 平台 | 配置项 |
|------|--------|
| 微信公众号 | `wechat_app_id`, `wechat_app_secret` |
| 微博 | `weibo_app_key`, `weibo_access_token` |
| 今日头条 | `toutiao_app_id`, `toutiao_access_token` |

### 国际平台配置

| 平台 | 配置项 |
|------|--------|
| Twitter/X | `twitter_api_key`, `twitter_api_secret`, `twitter_access_token`, `twitter_access_secret`, `twitter_bearer_token` |
| LinkedIn | `linkedin_client_id`, `linkedin_client_secret`, `linkedin_access_token` |
| Reddit | `reddit_client_id`, `reddit_client_secret`, `reddit_username`, `reddit_password` |
| Telegram | `telegram_bot_token`, `telegram_channel` |

### AI 能力配置

| 能力 | 配置项 |
|------|--------|
| AI 文案 (通义千问) | `dashscope_text_key` |
| AI 配图 (通义万相) | `dashscope_api_key` |

## API 端点

### 配置检测

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/social/check-config` | 检测各平台和 AI 能力的配置状态 |

### AI 内容生成

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/social/generate` | AI 文案生成（支持 topic、content_type、temperature） |
| POST | `/admin/social/generate-image` | AI 配图生成（支持 prompt、title、cover 模式） |

### 内容类型

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/social/content-types` | 查询各平台支持的内容类型 |

### 发布管理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/social/publish` | 发布内容到指定平台（支持多平台同时发布） |
| GET | `/admin/social/publish-status/<publish_id>` | 查询微信发布状态 |

### 发布历史

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/social/history` | 分页查询发布历史（支持按平台筛选） |
| DELETE | `/admin/social/history/<id>` | 删除发布历史记录 |

### CMS 导入

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/social/import-from-cms` | 列出已发布 CMS 文章供导入 |

### 发布请求体示例

```json
{
  "title": "文章标题",
  "body": "正文内容",
  "body_html": "<h1>HTML 正文</h1>",
  "summary": "摘要",
  "author": "admin",
  "cover_image_url": "/static/uploads/cover.jpg",
  "platforms": ["wechat", "weibo", "twitter"],
  "auto_publish": false
}
```

## 平台内容类型

| 平台 | 支持的内容类型 |
|------|----------------|
| 微信公众号 | article（文章）、announcement（通知）、promotion（推广） |
| 微博 | weibo（微博帖子） |
| 今日头条 | article（文章） |
| Twitter/X | tweet（推文） |
| LinkedIn | article（文章）、post（动态） |
| Reddit | link（链接帖）、text（文本帖） |
| Telegram | message（频道消息） |

## 依赖关系

### 内部依赖

| 依赖项 | 用途 |
|--------|------|
| `plugins._base.db` | 插件基础数据库连接模块 |
| `auth-center.models` | 主库读取（system_config 配置、cms_posts 文章） |
| `auth-center.services.wechat_push_service` | 微信公众号推送（`create_draft`、`submit_publish`、`upload_article_image`） |
| `auth-center.services.weibo_service` | 微博发布（`publish_weibo`） |
| `auth-center.services.toutiao_service` | 头条发布（`publish_article`） |
| `auth-center.services.ai_content_generator` | 公共 AI 服务（`generate_article`、`generate_image`、`generate_cover_image`） |

### 外部依赖

| 依赖项 | 用途 |
|--------|------|
| 微信公众号 API | 草稿创建与发布 |
| 微博开放平台 API | 微博发布 |
| 今日头条 API | 头条文章发布 |
| Twitter/X API | 推文发布 |
| LinkedIn API | 文章/动态发布 |
| Reddit API | 帖子发布 |
| Telegram Bot API | 频道消息发布 |
| DashScope (通义千问) | AI 文案生成 |
| DashScope (通义万相) | AI 配图生成 |

### 提供的 Hook

| Hook 标识符 | 说明 |
|-------------|------|
| `social_push/publish` | 发布内容到社交媒体平台 |

## 菜单组

- **Publishing** - Social Push

## 扩展指南

### 添加新的社交媒体平台

1. 在 `providers/` 下创建新的 Provider 文件（如 `facebook.py`）
2. 继承 `providers.base.BaseSocialProvider` 并实现 `publish()` 和 `get_config_fields()` 方法
3. 在 `providers/__init__.py` 的 `get_provider()` 工厂函数中注册新平台
4. 在 `routes.py` 的 `_publish_to_platform()` 中添加路由分支

```python
# providers/facebook.py 示例
from .base import BaseSocialProvider

class FacebookProvider(BaseSocialProvider):
    platform_id = 'facebook'
    platform_name = 'Facebook'

    def get_config_fields(self):
        return [
            {'key': 'facebook_app_id', 'label': 'App ID', 'type': 'text'},
            {'key': 'facebook_app_secret', 'label': 'App Secret', 'type': 'password'},
            {'key': 'facebook_page_token', 'label': 'Page Access Token', 'type': 'password'},
        ]

    def publish(self, title, body, summary, image_url, link_url, config):
        # 实现 Facebook Graph API 发布逻辑
        ...
```

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。

## 变更日志

### v1.2.0 (2026-08-06)

审计修复完成（19 项问题全部清零）：

**阻塞级**
- Reddit 发布链路打通：发布表单的 subreddit 板块透传至 Reddit Provider
- `_get_international_providers()` 签名与调用处对齐，Provider 配置正确传入，`is_configured()` 判定准确
- 版本号统一为 1.2.0（`plugin.json` / `__init__.py`）

**高危**
- LinkedIn `shareMediaCategory` 根据是否提供链接自动切换 `ARTICLE` / `NONE`
- tweepy 响应改为 `resp.data.id` 访问（替代 `resp.data['id']`）
- 移除外部 `get_market` 依赖，市场信息直接读取 `DEPLOY_MARKET` 环境变量
- `category` 规范化为 v1.4 标准枚举（`social`）

**中危**
- `settings_schema` 完整声明（20 个配置项，合法 JSON Schema）
- 数据库连接改为线程安全（`threading.local`）
- `created_at` 迁移为 `TIMESTAMPTZ`，历史 TEXT 列幂等 ALTER
- 管理页增加 `content_factory` 可用性检测（未启用时 AI 按钮禁用）
- 补充 "Import from CMS" 中英翻译键
- v1.4 商店规范：声明 `dashboard.stats` 并实现 `get_dashboard_stats()`

**低危**
- 今日头条成功提示引号错误修复
- 移除插件入口的 `sys.path` 污染
- 删除 `plugin.json` 中非标准 `enabled` 字段
- Telegram 有图片链接时改用 `sendPhoto` 发送
- 修复少量 i18n 字符串空格问题