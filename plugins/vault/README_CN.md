# Vault (vault)

> 数据保险库 — 全量/增量备份、AES-256-GCM 加密、定时调度、审计日志、多目标存储与一键恢复。

版本：**2.1.1**

## 概述

Vault 是 VeroRun 的数据备份与恢复插件，为系统提供企业级数据保护能力：基于 `pg_dump` 的数据库全量备份、tar.gz 归档、gzip 压缩、AES-256-GCM 加密、HMAC-SHA256 签名校验、多目标存储（本地 / S3 / OSS / Azure / GCS / SFTP / WebDAV）、cron 定时调度、审计日志、合规报告，以及带沙箱验证的恢复演练（Restore Drill）与时间点恢复（PITR）。

## 功能特性

- **全量备份**：一键创建数据库完整备份快照（归档格式 `vault_*.tar.gz`）
- **增量/差异备份**：表结构预留 `backup_type`（full / incremental / differential），支持按类型编排
- **压缩**：gzip 压缩归档，降低存储与传输成本
- **AES-256-GCM 加密**：可选加密备份流，保障落盘数据机密性
- **HMAC-SHA256 签名校验**：通过 `VAULT_SIGNING_KEY` 对备份签名，支持完整性校验，防篡改
- **定时调度**：cron 表达式驱动（`vault_schedules`），支持启停/单次跳过；并与编排器 `cron_jobs` 集成（默认注册每日 03:00 UTC 备份任务）
- **多目标存储**：本地 + 六种远程后端（S3 / OSS / Azure / GCS / SFTP / WebDAV），支持默认目标与连接测试
- **3-2-1 旋转上传**：将最新备份按 3-2-1 原则推送至远程目标
- **存储分层报告**：查看各存储目标的分布与用量
- **一键恢复**：从备份归档恢复数据库，支持内容预览、自定义范围与目标库
- **时间点恢复（PITR）**：恢复到指定时间戳
- **恢复演练（Drill）**：沙箱还原 → 校验 → 清理，验证备份可恢复性
- **审计日志**：完整记录备份/恢复/调度/存储等操作，满足合规要求
- **合规报告**：保留策略、恢复演练、加密状态三方面自动检查
- **健康检查**：备份时效、存储用量、健康评分
- **趋势分析**：近 90 次成功备份的大小趋势
- **通知**：邮件 / Webhook / 飞书 / 钉钉，备份成功/失败推送
- **管理后台**：完整嵌入式管理界面（仪表盘 / 备份 / 恢复 / 调度 / 存储 / 设置 / 审计）

## 架构设计

### 数据隔离策略

按照 [插件标准 v1.3 §9.1](docs/plugin-standard-v1.3.md)，Vault 使用**独立数据库 schema `vault`** 存储全部插件数据，不再占用公共 `public` schema：

```sql
CREATE SCHEMA IF NOT EXISTS vault;
SET search_path TO vault, public;
```

- 所有业务访问通过 `get_vault_conn()` 固定 `search_path = vault, public`：插件表落到 `vault`，系统表经 `public` 回退，互不干扰。
- 全量备份归档文件仍写入 `data/vault/` 目录（受 `backup_dir` 配置控制）。

### 备份流水线

```
backup_engine.py  (编排：create_full_backup)
        │
        ├─ dumper.py        pg_dump 数据库导出 → tar.gz 归档
        ├─ compressor.py    gzip 压缩
        ├─ encryptor.py     AES-256-GCM 流加密（可选）
        ├─ uploader.py      多目标存储上传（StorageRouter）
        └─ notifier.py      成功/失败通知（邮件/Webhook/飞书/钉钉）
```

### 恢复流水线

```
restore_engine.py  (编排：restore / restore_pitr / drill_restore)
        │
        ├─ preview         预览备份内容
        ├─ restore         解压 → 解密 → psql 还原（可指定范围/目标库）
        ├─ pitr            时间点恢复
        └─ drill_restore   沙箱还原 → 校验 → 清理
```

### 模块结构

| 模块 | 职责 |
|------|------|
| `backup_engine.py` | 备份流程编排引擎 |
| `dumper.py` | 数据库导出（pg_dump）与归档 |
| `compressor.py` | gzip 压缩 |
| `encryptor.py` | AES-256-GCM 加密 |
| `validator.py` | HMAC-SHA256 签名 / 完整性校验 |
| `uploader.py` | 多目标存储上传 |
| `restore_engine.py` | 恢复 / PITR / 恢复演练编排 |
| `scheduler.py` | cron 定时调度（`vault_schedules`） |
| `compliance.py` | 合规检查报告（保留 / 演练 / 加密） |
| `audit.py` | 审计日志记录与查询 |
| `notifier.py` | 通知分发（邮件 / Webhook / 飞书 / 钉钉） |
| `storage/base.py` | 存储路由与抽象基类（local/s3/oss/azure/gcs/sftp/webdav） |
| `utils.py` | 连接助手（`get_vault_conn`）、幂等建表（`ensure_schema`）与配置读取 |

## 数据库结构

全部表位于 `vault` schema（由 `migrations/001_initial.sql` 幂等创建，`IF NOT EXISTS`）：

| 表 | 用途 |
|----|------|
| `vault_backups` | 备份任务记录（类型/状态/大小/校验和/内容摘要/时间） |
| `vault_schedules` | 备份调度计划（cron/保留策略/窗口/前后钩子/启停） |
| `vault_audit_log` | 审计日志（动作/资源/操作者/IP/详情） |
| `vault_storage_targets` | 存储目标配置（类型/配置/默认标记/连接测试） |

## 目录结构

```
vault/
├── __init__.py              # 插件入口（on_install / on_enable / 路由注册 / 调度播种）
├── routes.py                # 管理后台页面与 API 路由
├── run_scheduler.py         # 独立调度器启动入口
├── plugin.json              # 插件元数据与默认配置
├── README.md / README_CN.md # 英文 / 中文文档
├── migrations/
│   └── 001_initial.sql      # 幂等迁移：建 vault schema + 4 张表 + 索引
├── services/
│   ├── __init__.py
│   ├── backup_engine.py     # 备份引擎（编排）
│   ├── dumper.py            # 数据库导出
│   ├── compressor.py        # 压缩
│   ├── encryptor.py         # AES-256-GCM 加密
│   ├── validator.py         # HMAC 签名 / 校验
│   ├── uploader.py          # 上传
│   ├── restore_engine.py    # 恢复 / PITR / Drill
│   ├── scheduler.py         # 定时调度
│   ├── compliance.py        # 合规报告
│   ├── audit.py             # 审计日志
│   ├── notifier.py          # 通知
│   ├── utils.py             # 连接助手 / 幂等建表
│   └── storage/
│       ├── base.py          # 存储路由抽象基类
│       ├── local.py / s3.py / oss.py / azure.py / gcs.py / sftp.py / webdav.py
├── static/
│   ├── vault.css            # 管理后台样式
│   └── vault.js             # 管理后台脚本
├── templates/               # 页面模板（vault / audit / restore / schedules / settings / storage）
└── i18n/
    ├── en.yml               # 英文国际化
    └── zh-CN.yml            # 中文国际化
```

## 安装与启用

1. 插件已随 VeroRun 默认插件目录分发，无需额外安装。
2. 安装依赖：

```bash
pip install croniter cryptography paramiko requests
# 按需：pip install boto3 oss2 azure-storage-blob google-cloud-storage
```

3. 启用插件：管理后台「插件管理」中启用 Vault。启用时自动执行：
   - 创建备份目录 `data/vault/`
   - 幂等执行迁移（建 `vault` schema 与数据表，属主为应用数据库用户）
   - 在编排器注册每日备份任务（默认 `0 3 * * *` UTC）
4. 管理后台 **System → Vault** 进入备份管理。

> 运行时每次 Vault 请求前也会触发 `ensure_schema()` 幂等检查，确保表结构始终就绪（全新环境由应用用户建表，属主正确）。

## 配置说明

默认配置位于 `plugin.json` 的 `config` 字段，运行时可通过管理后台「设置」页覆盖：

```json
{
  "backup_dir": "data/vault",
  "keep_days": 30,
  "include_files": true,
  "include_config": true,
  "encryption": { "enabled": false, "algorithm": "aes256-gcm", "key_source": "env" },
  "compression": { "algorithm": "gzip", "level": 6 },
  "storage": { "type": "local", "s3_bucket": "", "s3_region": "", "s3_access_key": "", "s3_secret_key": "", "oss_endpoint": "", "oss_bucket": "", "oss_access_key": "", "oss_secret_key": "" },
  "schedule": { "enabled": false, "interval_hours": 24 },
  "notifications": {
    "email": { "enabled": false, "smtp_host": "", "smtp_port": 465, "smtp_user": "", "smtp_password": "", "recipients": [] },
    "webhook": { "enabled": false, "url": "", "headers": {} },
    "feishu": { "enabled": false, "webhook_url": "" },
    "dingtalk": { "enabled": false, "webhook_url": "" }
  }
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `backup_dir` | 备份归档目录（相对项目根） | `data/vault` |
| `keep_days` | 自动清理保留天数（`/api/cleanup`） | `30` |
| `include_files` | 备份是否包含文件 | `true` |
| `include_config` | 备份是否包含配置 | `true` |
| `encryption.enabled` | 是否启用 AES-256-GCM 加密 | `false` |
| `encryption.algorithm` | 加密算法 | `aes256-gcm` |
| `encryption.key_source` | 密钥来源 | `env` |
| `compression.algorithm` | 压缩算法 | `gzip` |
| `compression.level` | 压缩级别 | `6` |
| `storage.type` | 默认存储类型 | `local` |
| `schedule.enabled` | 是否启用默认定时备份 | `false` |
| `schedule.interval_hours` | 定时间隔（小时） | `24` |
| `notifications.*` | 各通知渠道配置 | 全部关闭 |

## API 端点

> 所有接口均需管理员 JWT（`sso_token` Cookie 或 `?token=`），未登录非 AJAX 请求跳转 `/admin/login`。

### 页面路由

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/admin/vault/` | 备份仪表盘 |
| `GET` | `/admin/vault/backups` | 备份列表 |
| `GET` | `/admin/vault/restore` | 恢复向导 |
| `GET` | `/admin/vault/schedules` | 调度管理 |
| `GET` | `/admin/vault/storage` | 存储配置 |
| `GET` | `/admin/vault/settings` | 插件设置 |
| `GET` | `/admin/vault/audit` | 审计日志 |

### 备份 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/admin/vault/api/backup/create` | 触发备份（全量，兼容增量/差异） |
| `POST` | `/admin/vault/api/create` | 触发备份（旧版，兼容保留） |
| `GET` | `/admin/vault/api/backup/list` | 备份列表（搜索/筛选/分页） |
| `GET` | `/admin/vault/api/list` | 备份列表（旧版） |
| `GET` | `/admin/vault/api/backup/detail/<label>` | 备份详情 + 内容预览 |
| `GET` | `/admin/vault/api/backup/download/<label>` | 下载备份 |
| `DELETE` | `/admin/vault/api/backup/delete/<label>` | 删除备份（需 confirm 确认） |
| `DELETE` | `/admin/vault/api/delete/<label>` | 删除备份（旧版） |
| `DELETE` | `/admin/vault/api/cleanup` | 按 `keep_days` 清理过期备份 |
| `POST` | `/admin/vault/api/backup/sign/<label>` | HMAC-SHA256 签名（需 `VAULT_SIGNING_KEY`） |
| `GET` | `/admin/vault/api/backup/verify/<label>` | 签名与完整性校验 |

### 恢复 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/admin/vault/api/restore/preview` | 预览备份内容 |
| `POST` | `/admin/vault/api/restore` | 执行恢复（可选 scope / target_db / target_host） |
| `POST` | `/admin/vault/api/restore/pitr` | 时间点恢复（`target_time` ISO 格式） |
| `POST` | `/admin/vault/api/restore/drill` | 恢复演练（沙箱还原→校验→清理） |

### 调度 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/admin/vault/api/schedule/list` | 调度列表 |
| `POST` | `/admin/vault/api/schedule/create` | 创建调度（name + cron_expression） |
| `PUT` | `/admin/vault/api/schedule/<id>` | 更新调度 |
| `DELETE` | `/admin/vault/api/schedule/<id>` | 删除调度 |
| `POST` | `/admin/vault/api/schedule/<id>/toggle` | 启用/停用调度 |

### 存储 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/admin/vault/api/storage/list` | 存储目标列表 |
| `POST` | `/admin/vault/api/storage/create` | 创建存储目标 |
| `PUT` | `/admin/vault/api/storage/<id>` | 更新存储目标 |
| `DELETE` | `/admin/vault/api/storage/<id>` | 删除存储目标 |
| `POST` | `/admin/vault/api/storage/<id>/test` | 连接测试 |
| `POST` | `/admin/vault/api/storage/rotate` | 3-2-1 旋转上传最新备份 |
| `GET` | `/admin/vault/api/storage/tier/report` | 存储分层报告 |

### 其他 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/admin/vault/api/health` | 健康检查（评分/上次备份/下次调度/磁盘用量） |
| `GET` | `/admin/vault/api/audit` | 审计日志查询（action/resource_type/operator/limit/offset） |
| `GET` | `/admin/vault/api/compliance/report` | 合规报告 |
| `GET` | `/admin/vault/api/trend` | 备份大小趋势（近 90 次成功备份） |

### Hook 提供

| Hook 标识符 | 说明 |
|-------------|------|
| `vault/create_backup` | 触发备份 |
| `vault/list_backups` | 备份列表 |
| `vault/delete_backup` | 删除备份 |
| `vault/health_check` | 备份系统健康检查 |
| `vault/audit_log` | 审计日志查询 |

## 安全设计

- **身份认证**：所有 Vault API 强制管理员 JWT 校验（`validate_token` + `is_admin`）。
- **数据加密**：可选 AES-256-GCM 流加密备份（`encrypt_stream`）。
- **防篡改**：`VAULT_SIGNING_KEY` 环境变量启用 HMAC-SHA256 签名与校验。
- **审计留痕**：备份/恢复/调度/存储关键操作均写入 `vault_audit_log`。
- **删除确认**：删除备份接口要求 `confirm` 字段，防止误删。
- **连接隔离**：插件数据固定在 `vault` schema，不写入公共区。

## 通知

支持邮件（SMTP）、Webhook、飞书、钉钉四种渠道，备份成功/失败事件自动推送。渠道配置见「配置说明」`notifications` 段。

## 依赖关系

| 依赖 | 用途 | 必需 |
|------|------|------|
| `croniter` | cron 表达式解析 | ✅ |
| `cryptography` | AES-256-GCM 加密 | ✅ |
| `paramiko` | SFTP 存储后端 | ✅ |
| `requests` | HTTP 通知发送 | ✅ |
| `boto3` | S3 存储后端 | ⭕ 可选 |
| `oss2` | 阿里云 OSS 后端 | ⭕ 可选 |
| `azure-storage-blob` | Azure Blob 后端 | ⭕ 可选 |
| `google-cloud-storage` | GCS 后端 | ⭕ 可选 |

## 常见问题

- **storage/list 返回 500 / relation does not exist**：确认已部署 2.1.1 及以上代码并重启服务，`ensure_schema()` 会自动在 `vault` schema 建表；若为旧版残留的 `public.vault_*` 表，不影响新代码，可按运维流程在确认后清理。
- **备份加密报 ValueError**：`encryption.enabled` 为 false 或密钥未配置时，加密步骤自动跳过，属预期行为。
- **签名接口 400**：需先设置环境变量 `VAULT_SIGNING_KEY`。
- **调度不生效**：确认调度计划 `enabled = true` 且 cron 表达式合法；编排器任务入口为 `run_scheduler.py`。

## 许可证

本插件为 VeroRun 项目的一部分，遵循 VeroRun 项目整体许可证协议。
