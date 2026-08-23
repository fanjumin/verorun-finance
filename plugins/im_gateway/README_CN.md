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

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。