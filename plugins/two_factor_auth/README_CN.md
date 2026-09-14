# 双因素认证插件（two_factor_auth）

> 基于 TOTP 的 VeroRun 双因素认证，兼容 Google Authenticator / Microsoft Authenticator，提供一次性恢复码、设备绑定的短期令牌，以及完整的操作审计。

版本：**1.2.0**

## 概述

双因素认证插件为 VeroRun 登录增加第二重身份验证。它为账户签发 TOTP 密钥（AES-256-GCM 加密，绑定 `user_id`），提供 Authenticator 风格设置向导（打开即显示二维码、扫码/手动输入密钥切换、30 秒倒计时、恢复码），在用户已启用 2FA 时通过 `auth.before_issue_session` 钩子拦截登录，并经由设备绑定的挑战页完成最终登录。插件为**纯插件设计**：不改任何核心文件，禁用插件后系统行为与未安装时完全一致。

## 功能特性

- **Authenticator 风格设置流程** — 打开设置页即显示二维码（无需点按钮），输码时二维码保持可见，支持"扫码 / 手动输入密钥"切换与复制，1→2→3 步骤向导，30 秒验证码倒计时，恢复码一键复制/打印
- **标准 TOTP（RFC 6238 / Key Uri Format）** — 6 位动态码、30 秒周期、SHA-1、BASE32 密钥，Google Authenticator / Microsoft Authenticator / 1Password / 2FAS / Aegis 均可识别
- **登录挑战** — 已启用 2FA 的用户登录时，登录被拦截一次，引导至挑战页完成第二因子校验后才签发 SSO 会话
- **恢复码** — 启用时生成 10 个一次性 10 位恢复码（bcrypt 哈希存储），使用后即作废
- **暴力破解防护** — 失败次数原子自增，达到 `max_failed_attempts` 后锁定 `lockout_seconds` 秒（无 TOCTOU 竞态）
- **短期、设备绑定令牌** — 管理端 JWT 不进 URL：在内存中换取 3 分钟有效的 `setup_token`（绑定 IP 前缀 + UA）；登录挑战绑定生成设备并原子消费，防重放
- **静态加密** — TOTP 密钥用 AES-256-GCM 加密，密钥由稳定的 `TOTP_MASTER_KEY` 环境变量派生，以 `user_id` 作为 AAD
- **操作审计** — `setup_token_issued`、`setup_init`、`setup_enable`、`setup_disable`、`challenge_generated`、`login_success/failed`、设备绑定不一致等事件写入 `audit_log`
- **自动清理** — 启用时启动 APScheduler 定时任务，每小时清理过期的 challenge 与 setup_token
- **国际化** — 中英文界面，遵循项目 i18n 标准（英文键、英文默认语言）

## 架构

### 数据隔离

遵循插件标准，插件数据全部存放在独立 schema `two_factor_auth`，不写入共享 `public` 区域：

```sql
CREATE SCHEMA IF NOT EXISTS two_factor_auth;
SET search_path TO two_factor_auth, public;
```

所有插件访问经 `get_two_factor_db()`（借池连接 + 切换 search_path + UTC 时间语义）。迁移使用 `pg_try_advisory_xact_lock` 串行化 + `schema_migrations` run-once 记录，避免多 gunicorn worker 并发 DDL 死锁。

### 登录拦截

```
登录 → 核心 issue_auth_session
         │
         ▼
   apply_filters('auth.before_issue_session', …)   ← two_factor_auth 过滤器
         │
         ├─ scenario != 'login'          → 放行（None）
         ├─ 用户未启用 2FA               → 放行（None）
         ├─ 已启用 2FA → 返回 {blocked, challenge_token, redirect}
         │                 → 核心抛 TwoFactorRequired → 返回 needs_2fa 响应
         │                 → 浏览器打开 /plugin/two_factor_auth/challenge-page
         └─ 任何异常                    → fail-open（放行登录）并记录日志
```

预检为 **fail-open**：任何异常（含数据库故障）都直接放行登录并记录告警，绝不因 2FA 把用户挡在系统外。

### 模块说明

| 模块 | 职责 |
|------|------|
| `__init__.py` | 插件生命周期（on_install/on_enable/on_disable/on_uninstall）、过滤器注册、清理调度 |
| `routes.py` | Flask Blueprint：门户换牌、设置页、init/verify/disable、状态、挑战校验、会话签发 |
| `services.py` | TOTPService（生成/加解密/校验、provisioning URI、二维码）、恢复码、pre_login_check |
| `models.py` | 数据库访问层、schema 切换、advisory lock 迁移 |
| `plugin.json` | 清单：元数据、配置默认值、菜单、依赖 |

## 数据库表

所有表位于 `two_factor_auth` schema（迁移 `0001_init.sql`、`0002_pending_timestamptz.sql`、`0003_security_hardening.sql`）：

| 表 | 用途 |
|-----|------|
| `setup_tokens` | 短期门户令牌（3 分钟、IP/UA 绑定、暂存待确认 TOTP 密钥，支撑幂等 init） |
| `user_totp` | 每用户加密 TOTP 密钥、启用状态、恢复码哈希、失败/锁定计数 |
| `two_factor_challenges` | 登录挑战（5 分钟、设备绑定、原子消费） |
| `audit_log` | 操作审计事件 |
| `schema_migrations` | 已应用迁移文件（run-once） |

## 目录结构

```
plugins/two_factor_auth/
├── __init__.py              # 插件生命周期 + 钩子注册 + 清理调度
├── routes.py                # Blueprint（门户/设置页/init/verify/disable/status/challenge）
├── services.py              # TOTP / AES-GCM / 恢复码 / pre_login_check
├── models.py                # 数据库访问 + advisory lock 迁移
├── plugin.json              # 清单与默认配置
├── CHANGELOG.md
├── README.md / README_CN.md # 英文 / 中文文档（本文档含使用说明书）
├── migrations/
│   ├── 0001_init.sql
│   ├── 0002_pending_timestamptz.sql
│   └── 0003_security_hardening.sql
├── templates/
│   ├── setup.html           # Authenticator 风格设置向导
│   └── challenge.html       # 登录第二因子校验页
└── i18n/
    ├── en.yml
    └── zh-CN.yml
```

## 安装与启用

1. 插件随 VeroRun 插件集内置，无需单独下载。
2. 安装依赖：

```bash
pip install pyotp qrcode bcrypt cryptography
```

3. **先配置加密密钥**（必需，见"配置"）：在承载插件的每个服务的 `.env` 中设置稳定的 `TOTP_MASTER_KEY` 并重启。

4. 在 **后台 → 插件管理** 中启用。启用时插件自动：
   - 执行幂等 schema 迁移；
   - 注册 `auth.before_issue_session` 过滤器；
   - 启动每小时清理调度。

5. 在后台 **安全与合规 → 两步验证设置** 菜单中为账户绑定。

> 禁用插件会注销过滤器并停止调度；卸载会删除 `two_factor_auth` schema。

## 配置

默认值来自 `plugin.json` 的 `config` 字段，可通过 `TOTP_*` 环境变量按环境覆盖（环境变量优先）：

| 键 | 环境变量 | 说明 | 默认值 |
|----|----------|------|--------|
| `max_failed_attempts` | `TOTP_MAX_FAILED_ATTEMPTS` | 锁定前的失败次数 | `5` |
| `lockout_seconds` | `TOTP_LOCKOUT_SECONDS` | 锁定时长（秒） | `900` |
| `recovery_code_count` | `TOTP_RECOVERY_CODE_COUNT` | 启用时生成的恢复码数量 | `10` |
| `issuer_name` | `TOTP_ISSUER_NAME` | Authenticator 中显示的名称 | `VeroRun` |
| `totp_valid_window` | `TOTP_VALID_WINDOW` | TOTP 容错窗口（±30 秒/级） | `1` |
| — | `TOTP_MASTER_KEY` | **加密密钥（≥32 字符）** — 见下方警告 | （无） |

### `TOTP_MASTER_KEY` — 必读

```env
TOTP_MASTER_KEY=<至少 32 个随机字符>
```

- 该值经 SHA-256 派生为 32 字节 AES-256-GCM 密钥，用于加密所有 TOTP 密钥。
- **必须稳定**。用户在绑定后再修改，将导致其密钥无法解密、2FA 登录被锁死。
- **必须在启用用户前配置**。缺失或过短时，`/setup/init` 与 `/challenge/verify` 会抛 `RuntimeError`（HTTP 500）。
- 生成方式：`openssl rand -hex 32`；请备份到安全位置，并在所有承载该插件的环境使用同一密钥。

## API 端点

> 所有端点位于自动注册前缀 `/plugin/two_factor_auth` 下。设置类端点接受短期 `setup_token`（`?setup_token=` 或 `Authorization: Bearer`）；管理类状态端点需登录 JWT。

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 存活检查 |
| `POST` | `/setup-token` | 用登录 JWT（`Bearer`）换取 3 分钟 `setup_token`（在内存中完成，JWT 不进 URL） |
| `GET` | `/setup-page` | 设置向导页（`?setup_token=…` 或 `sso_token` cookie） |
| `POST` | `/setup/init` | 生成 TOTP 密钥 + 二维码；**幂等**——重复调用返回已暂存密钥（刷新页面二维码不变） |
| `POST` | `/setup/verify` | 校验 6 位动态码（重绑定还需当前既有 2FA 的旧码），启用 2FA，返回恢复码 |
| `POST` | `/setup/disable` | 关闭 2FA（需当前有效动态码） |
| `GET` | `/status` | 启用状态（需登录 JWT） |
| `GET` | `/challenge-page` | 登录第二因子校验页（`?challenge_token=…`） |
| `POST` | `/challenge/verify` | 校验第二因子，原子消费挑战，签发最终 SSO 会话 |

## 安全设计

- **长期 JWT 不进 URL** — 管理端前端在内存中将 JWT 换为 3 分钟 `setup_token`，页面只携带短期令牌
- **设备绑定** — setup_token 与 challenge 记录 IP 前缀 + UA；不一致视为泄露并作废（setup token）/拒绝（challenge）
- **防重放** — 挑战原子消费（`WHERE consumed=false RETURNING`）；setup_token 在 verify/disable 成功后删除
- **加密** — AES-256-GCM，`user_id` 作 AAD（v2 格式；遗留 `v1:` 密文仍可解密）
- **无 TOCTOU 锁定** — 失败计数与锁定时间在单条原子 `UPDATE … RETURNING` 内完成
- **恢复码** — bcrypt 哈希存储；验证时每次失败最多抽样 2 个哈希，限制 CPU DoS 面
- **开放重定向防护** — `done`/`redirect` 参数经服务端同源校验
- **fail-open 登录预检** — 2FA 过滤器异常绝不阻断登录（记录日志）
- **迁移安全** — advisory lock 串行化 + run-once 记录，防止并发 DDL 死锁与重复时区转换

## 国际化（i18n）

插件遵循项目 i18n 标准（[docs/i18n-standard.md](../../docs/i18n-standard.md)）：词条键为英文，英文包为默认语言。新增界面文案须同时在 `i18n/en.yml` 与 `i18n/zh-CN.yml` 中以相同英文键添加；缺失词条回退显示键文本。

## 依赖

| 依赖 | 用途 | 必需 |
|------|------|------|
| `pyotp` | TOTP 生成/校验、provisioning URI | ✅ |
| `qrcode` | 二维码渲染 | ✅ |
| `bcrypt` | 恢复码哈希 | ✅ |
| `cryptography` | AES-256-GCM 加密 | ✅ |

---

# 使用说明书

## 一、管理员指南

### 1. 部署前准备

1. 在 `.env` 中配置 `TOTP_MASTER_KEY`（≥32 字符随机串），**先配置再启用**：

```bash
openssl rand -hex 32
```

2. 将输出写入所有承载该插件服务的 `.env`：

```env
TOTP_MASTER_KEY=<上一步的输出>
```

3. 重启服务。**密钥一旦有用户绑定后即不可更改**，请备份。

### 2. 启用插件

后台 → 插件管理 → 找到 **Two-Factor Authentication** → 启用。启用后：
- schema 自动创建（幂等）
- 登录预检过滤器注册
- 每小时清理调度启动

### 3. 入口与菜单

启用后后台左侧 **安全与合规（Security & Compliance）** 分组出现 **两步验证设置（2FA Settings）** 菜单，点击后在右侧打开设置向导（iframe）。

### 4. 审计与运维

- 操作事件记录于 `two_factor_auth.audit_log`（token 签发、初始化、启用、关闭、挑战生成、登录成功/失败、设备绑定不一致）
- 过期数据每小时自动清理
- 无需手工维护表结构；升级插件后迁移自动应用

## 二、终端用户指南

### 1. 绑定（首次开启）

1. 登录后台 → 安全与合规 → **两步验证设置**
2. 页面**自动显示二维码**（无需点按钮）
3. 打开手机 **Google Authenticator / Microsoft Authenticator** → 扫描二维码（或点"无法扫码？手动输入密钥"，输入页面展示的密钥）
4. App 中出现 **VeroRun: <你的账户>**，动态码每 30 秒刷新
5. 在页面输入当前 6 位动态码 → 点 **开启两步验证**
6. 页面展示 **10 个恢复码** → 点 **复制全部** 或 **打印**，离线妥善保存

### 2. 日常登录

1. 输入用户名密码 → 若已启用 2FA，登录会跳转到第二因子校验页
2. 打开 App 查看当前动态码 → 输入 → 完成登录
3. **注意**：校验绑定发起登录的设备（IP/UA），更换网络/浏览器可能导致校验失败，请重新登录

### 3. 使用恢复码

- 手机丢失时，在第二因子校验页输入任一未使用的恢复码（格式 `XXXXX-XXXXX`）
- 每个恢复码**仅可用一次**，用后即作废；10 个用完后需重新绑定
- 恢复码仅以哈希存储，后台无法查看明文，请自行备份

### 4. 更换手机 / 重新绑定

1. 进入 两步验证设置（已启用时页面进入重绑定流程）
2. 先输入**当前既有 2FA 的动态码**确认身份
3. 再扫描新二维码完成替换——旧密钥立即失效

### 5. 关闭双因素

- 在两步验证设置页输入当前动态码执行关闭
- 关闭需通过 2FA 校验，防止会话持有者静默降级

## 三、常见问题（FAQ）

**Q1：`/setup/init` 返回 500**
`TOTP_MASTER_KEY` 未配置或不足 32 字符。在 `.env` 配置后重启服务。

**Q2：`/setup/init` 返回 401**
`setup_token` 缺失、过期（3 分钟）或设备上下文不匹配。重新打开设置菜单获取新令牌。

**Q3：二维码一闪而过 / 无法扫码**
旧版本缺陷（二维码生成后立即隐藏）。升级到 v1.2.0+，打开页面即显示且保持可见。

**Q4：设置页里嵌套了整个后台（递归）**
旧版本缺陷（完成后在 iframe 内跳转 admin）。升级到 v1.2.0+，完成后跳转顶层窗口。

**Q5：绑定后无法登录，提示设备变更**
挑战绑定发起登录的设备。请在相同浏览器/网络重新登录；多次失败可能触发临时锁定，等待锁定时间后重试。

**Q6：`TOTP_MASTER_KEY` 被修改后老用户全部校验失败**
密钥变更后旧密文无法解密，**设计上不可恢复**。密钥必须保持稳定并备份。

**Q7：Authenticator 扫不出二维码**
确认使用的是 Google Authenticator / Microsoft Authenticator / 1Password / 2FAS / Aegis 等标准 TOTP 应用；或使用"手动输入密钥"方式录入。

## License

本插件属于 VeroRun 项目，遵循 VeroRun 项目许可证。
