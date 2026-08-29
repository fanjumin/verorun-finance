# Enterprise Verification (enterprise_verify)

## 概述

Enterprise Verification（企业认证）是 VeroRun 平台的企业资质审核插件，提供营业执照 OCR 识别和 AI 自动审核能力。插件使用独立的 PostgreSQL schema `enterprise_verify`，存储认证申请记录，同时跨读主库获取用户信息，审核通过后回写主库 `users` 表更新认证状态。

插件集成了 SiliconFlow 平台的 DeepSeek-OCR 模型进行营业执照智能识别，支持自动批准高置信度结果和人工审核两种模式。审核流程完整覆盖待审核、已批准、已驳回三种状态。

## 功能特性

- **OCR 营业执照识别**：基于 SiliconFlow/DeepSeek-OCR 模型，自动识别营业执照上的企业名称、税号等关键信息
- **AI 自动审核**：支持配置自动批准高置信度 OCR 结果，提高审核效率
- **人工审核**：提供完整的审核界面，支持批准/驳回操作，驳回需填写原因
- **用户信息回写**：审核通过后自动更新主库 `users` 表的 `enterprise_name`、`enterprise_tax_id`、`enterprise_verified` 字段
- **独立数据库**：使用 PostgreSQL schema `enterprise_verify`，包含 `enterprise_verifications` 表
- **批量用户信息补充**：Python 级合并插件库认证记录与主库用户信息（display_name、phone、email）
- **重试机制**：支持配置最大 OCR 重试次数

## 架构设计

```
+--------------------------------------------------------------+
|               前端（用户提交 + 管理审核）                       |
+--------------------------------------------------------------+
         |                              |
         v                              v
+------------------------+    +---------------------------+
|  routes_user.py        |    |  routes_admin.py          |
|  /api/enterprise/*     |    |  /admin/enterprise-       |
|                        |    |  verifications/*          |
|  +-- /submit           |    |  +-- /                    |
|     企业认证提交         |    |      认证列表（待审核）    |
|                        |    |  +-- /<id>/approve        |
|                        |    |      批准认证              |
|                        |    |  +-- /<id>/reject         |
|                        |    |      驳回认证              |
|                        |    |  +-- /settings            |
|                        |    |      配置读写              |
+------------------------+    +---------------------------+
         |                              |
         v                              v
+--------------------------------------------------------------+
|                      服务层 (services.py)                      |
|  +-- OCR 识别 (SiliconFlow/DeepSeek-OCR)                      |
|  +-- AI 自动审核逻辑                                           |
|  +-- 认证提交处理                                              |
+--------------------------------------------------------------+
         |                              |
         v                              v
+------------------------+    +---------------------------+
|  插件独立库              |    |  主库（读写）              |
|  PG Schema:             |    |  +-- users                |
|  enterprise_verify      |    |      (enterprise_name,    |
|  +-- enterprise_        |    |       enterprise_tax_id,  |
|      verifications      |    |       enterprise_verified)|
+------------------------+    +---------------------------+
```

**审核状态流转**：

```
pending --> approved  (批准，回写主库 users 表)
pending --> rejected  (驳回，需填写驳回原因)
```

## 目录结构

```
enterprise_verify/
+-- README.md                    # 插件文档
+-- plugin.json                  # 插件元数据配置
+-- __init__.py                  # 插件入口，注册蓝图和 Hook
+-- models.py                    # 数据模型（独立库连接、表创建、索引）
+-- routes_admin.py              # 管理端 API 路由（列表、批准、驳回、配置）
+-- routes_user.py               # 用户端 API 路由（认证提交）
+-- services.py                  # 核心服务（OCR 识别、AI 审核）
+-- enterprise_verify.db         # 独立数据库文件（保留用于迁移）
+-- i18n/
|   +-- en.yml                   # 英文国际化
|   +-- zh-CN.yml                # 中文国际化
+-- templates/
    +-- admin_verify.html        # 管理后台页面模板
```

## 安装与启用

### 前提条件

- VeroRun 平台版本 >= 0.10.0
- SiliconFlow API Key（用于 DeepSeek-OCR 识别 + AI 审核）
- PostgreSQL 数据库

### 安装步骤

1. 将 `enterprise_verify` 目录放置于 `plugins/` 下
2. 确保 `plugin.json` 中 `enabled` 为 `true`
3. 重启应用，插件将自动创建 PostgreSQL schema `enterprise_verify` 并初始化表
4. 在管理后台 "Users & Support" > "Enterprise Verification" 中配置 API Key
5. 设置 SiliconFlow API Key 以启用 OCR 识别功能

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `siliconflow_api_key` | string | "" | SiliconFlow API Key，用于 DeepSeek-OCR 识别和 AI 审核 |
| `auto_approve` | boolean | false | 是否自动批准高置信度 OCR 结果 |
| `max_retry` | integer | 3 | OCR 识别最大重试次数 |

## API 端点

### 管理端 API（需要管理员权限）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/enterprise-verifications/` | 分页查询认证列表（按状态筛选，默认 pending） |
| POST | `/admin/enterprise-verifications/<id>/approve` | 批准企业认证（回写主库 users 表） |
| POST | `/admin/enterprise-verifications/<id>/reject` | 驳回企业认证（需填写驳回原因） |
| GET | `/admin/enterprise-verifications/settings` | 获取插件配置 |
| POST | `/admin/enterprise-verifications/settings` | 保存插件配置 |

### 用户端 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/enterprise/submit` | 提交企业认证申请（上传营业执照等） |

## 数据库表结构

### enterprise_verifications

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | BIGINT (PK) | 自增主键 |
| `user_id` | BIGINT | 申请人用户 ID |
| `enterprise_name` | TEXT | 企业名称 |
| `tax_id` | TEXT | 统一社会信用代码/税号 |
| `license_url` | TEXT | 营业执照图片 URL |
| `ocr_raw` | TEXT | OCR 原始识别结果（JSON） |
| `status` | TEXT | 审核状态：pending / approved / rejected |
| `review_notes` | TEXT | 审核备注 |
| `reviewed_by` | BIGINT | 审核人 ID |
| `reviewed_at` | TEXT | 审核时间 |
| `created_at` | TEXT | 提交时间 |
| `updated_at` | TEXT | 更新时间 |

## 依赖关系

### 内部依赖

| 依赖项 | 用途 |
|--------|------|
| `plugins._base.db` | 插件基础数据库连接模块 |
| `auth-center.models` | 主库读写（users 表认证状态更新） |
| `auth-center.routes.admin` | 管理员鉴权（`_require_admin`）和操作日志（`_log`） |

### 外部依赖

| 依赖项 | 用途 |
|--------|------|
| SiliconFlow API (DeepSeek-OCR) | 营业执照 OCR 识别 |
| SiliconFlow API (DeepSeek) | AI 自动审核 |

### 提供的 Hook

| Hook 标识符 | 说明 |
|-------------|------|
| `enterprise_verify/submit` | 提交企业认证 |
| `enterprise_verify/audit` | 执行认证审核 |
| `enterprise_verify/ocr_recognize` | 触发 OCR 识别 |

## 菜单组

- **Users & Support** - Enterprise Verification

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一的许可证协议。