# SMS Service (sms)

## 概述

SMS Service 是 VeroRun 平台的短信验证码发送服务插件，支持阿里云和 Twilio 双短信服务商，可根据手机号区号自动路由选择提供商。插件使用独立的 PostgreSQL schema `sms`，存储短信模板和发送日志，提供验证码生成、发送、手机号验证、发送频率限制等完整功能。

插件还通过 `get_login_methods` 和 `get_register_methods` Hook 为平台提供动态的短信登录/注册方式，支持国家代码选择和国际手机号验证。

## 功能特性

- **双服务商支持**：阿里云（国内）和 Twilio（国际），按手机号区号自动路由
- **智能路由**：+86 号段走阿里云，其他国际区号走 Twilio，无区号按 `DEPLOY_MARKET` 环境变量回退
- **验证码生成**：使用 `secrets` 模块生成安全随机 6 位数字验证码
- **模板管理**：支持 captcha（验证码）、notice（通知）、promo（营销）三类短信模板的 CRUD 管理
- **发送日志**：完整记录每次发送的手机号、验证码、用途、提供商、状态
- **频率限制**：基于插件 schema `sms` 中 `sms_rate_limits` 表的每小时发送次数限制（默认 5 次/小时）
- **手机号验证**：支持 33 个国家/地区的手机号格式验证，自动检测国家代码
- **国家代码选择**：提供完整的国家列表（含国旗、区号、中英文名称）
- **动态登录方式**：通过 `get_login_methods` / `get_register_methods` Hook 提供短信认证方式
- **独立数据库**：使用 PostgreSQL schema `sms`，包含 `sms_templates`、`sms_logs` 和 `sms_rate_limits` 三张表
- **数据迁移**：首次启动自动从主库幂等迁移短信模板数据

## 架构设计

```
+--------------------------------------------------------------+
|                    前端（管理后台 + 用户端）                     |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      路由层 (routes.py)                        |
|  /admin/sms/*                                                 |
|  +-- /templates     短信模板 CRUD（按分类分组）                |
|  +-- /logs          短信发送日志查询                           |
|  +-- /test-send     测试短信发送                              |
|  +-- /settings      阿里云配置读写                            |
|  +-- /countries     国家代码列表                              |
+--------------------------------------------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      服务层 (services.py)                      |
|  +-- send_sms()              短信发送入口                      |
|  +-- generate_code()         验证码生成                        |
|  +-- validate_phone()        手机号验证                        |
|  +-- check_rate_limit()      频率限制检查                      |
|  +-- get_sms_provider()      提供商自动选择                    |
|  +-- _select_provider_by_phone()  号段路由                     |
|  +-- _send_aliyun_via_provider()  阿里云发送                   |
|  +-- _log_send()             发送日志记录                      |
+--------------------------------------------------------------+
              |                           |
              v                           v
+------------------------+    +---------------------------+
|  countries.py          |    |  auth-center providers    |
|  +-- COUNTRIES (33 国)|    |  +-- providers/sms/      |
|  +-- find_country()    |    |      aliyun.py           |
|  +-- detect_country()  |    |      twilio.py           |
|  +-- validate_phone()  |    |      (复用现有 Provider)  |
+------------------------+    +---------------------------+
                              |
                              v
+--------------------------------------------------------------+
|                      数据层 (models.py)                        |
|  PG Schema: sms                                               |
|  +-- sms_templates      短信模板表                            |
|  +-- sms_logs           短信发送日志表                        |
|  +-- sms_rate_limits    发送频率限制表                        |
+--------------------------------------------------------------+
```

**提供商路由逻辑**：

```
手机号以 +86 开头 --> 阿里云 (AliyunSMSProvider)
手机号以 + 开头且非 +86 --> Twilio (TwilioSMSProvider)
无区号 --> DEPLOY_MARKET=cn ? 阿里云 : Twilio
```

## 目录结构

```
sms/
+-- README.md                    # 插件文档
+-- plugin.json                  # 插件元数据配置
+-- __init__.py                  # 插件入口，注册蓝图和 Hook
+-- models.py                    # 数据模型（独立库连接、表创建、主库迁移）
+-- routes.py                    # 管理端 API 路由（模板、日志、测试发送、配置、国家列表）
+-- services.py                  # 核心服务（发送、验证码、手机号验证、频率限制、提供商路由）
+-- countries.py                 # 国家/地区列表与手机号验证规则
+-- migrations/
|   +-- v1.0.0_to_v1.1.0.sql     # 版本迁移 SQL（§10.6）
+-- i18n/
|   +-- en.yml                   # 英文国际化
|   +-- zh-CN.yml                # 中文国际化
+-- templates/
    +-- admin_sms.html           # 管理后台页面模板
```

## 安装与启用

### 前提条件

- VeroRun 平台版本 >= 0.10.0
- 阿里云短信服务 AccessKey 或 Twilio Account SID/Token
- PostgreSQL 数据库

### 安装步骤

1. 将 `sms` 目录放置于 `plugins/` 下
2. 重启应用，插件将自动：
   - 创建 PostgreSQL schema `sms`
   - 初始化 `sms_templates`、`sms_logs` 和 `sms_rate_limits` 表
   - 从主库幂等迁移短信模板数据
4. 在管理后台 "Security & Compliance" > "SMS Management" 中配置阿里云参数
5. 确保 `auth-center` 的 `providers/sms/aliyun.py` 和 `providers/sms/twilio.py` 已正确配置

### 环境变量

| 环境变量 | 说明 |
|----------|------|
| `DEPLOY_MARKET` | 部署市场：`cn`（国内）/ `intl`（国际），影响无区号手机号的提供商选择 |

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `aliyun_sms_sign_name` | string | "" | 阿里云短信签名 |
| `aliyun_sms_access_key` | string | "" | 阿里云 AccessKey ID |
| `aliyun_sms_secret` | string | "" | 阿里云 AccessKey Secret（敏感字段） |

## API 端点

### 管理端 API（需要管理员权限）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/sms/templates` | 获取所有短信模板（按分类分组：captcha/notice/promo） |
| POST | `/admin/sms/templates` | 创建新短信模板 |
| PUT | `/admin/sms/templates/<id>` | 更新短信模板 |
| DELETE | `/admin/sms/templates/<id>` | 删除短信模板 |
| GET | `/admin/sms/logs` | 分页查询短信发送日志 |
| POST | `/admin/sms/test-send` | 测试短信发送（支持国家代码选择） |
| GET | `/admin/sms/settings` | 获取阿里云短信配置 |
| POST | `/admin/sms/settings` | 保存阿里云短信配置 |
| GET | `/admin/sms/countries` | 获取支持的国家/地区列表 |

### 测试发送请求体示例

```json
{
  "phone": "13800138000",
  "code": "123456",
  "country_code": "+86",
  "purpose": "test"
}
```

## 短信模板分类

| 分类 | 标识符 | 说明 |
|------|--------|------|
| 验证码 | captcha | 用于登录、注册、修改密码等场景的验证码发送 |
| 通知 | notice | 用于订单通知、系统通知等场景 |
| 营销 | promo | 用于营销推广类短信 |

**预置模板映射**（阿里云模板代码）：

| 用途 | 模板代码 |
|------|----------|
| register | SMS_506135003 |
| change_phone | SMS_506380001 |
| reset_password | SMS_506285002 |
| modify_password | SMS_506190002 |
| login | SMS_506330002 |

## 依赖关系

### 内部依赖

| 依赖项 | 用途 |
|--------|------|
| `plugins._base.db` | 插件基础数据库连接模块 |
| `auth-center.models` | 主库读取（sms_templates 迁移源） |
| `auth-center.providers.sms.aliyun` | 阿里云短信 Provider |
| `auth-center.providers.sms.twilio` | Twilio 短信 Provider |

### 外部依赖

| 依赖项 | 用途 |
|--------|------|
| 阿里云短信服务 (Dysmsapi) | 国内短信发送 |
| Twilio API | 国际短信发送 |

### 提供的 Hook

| Hook 标识符 | 说明 |
|-------------|------|
| `sms/send` | 发送短信验证码 |
| `sms/generate_code` | 生成随机验证码 |
| `sms/validate_phone` | 验证手机号格式 |
| `sms/check_rate_limit` | 检查发送频率限制 |

### 动态认证方法

| 方法 | 说明 |
|------|------|
| `get_login_methods` | 提供短信验证码登录方式 |
| `get_register_methods` | 提供短信验证码注册方式 |

## 菜单组

- **Security & Compliance** - SMS Management

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。