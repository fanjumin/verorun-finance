# 飞书频道管理 — 完整设计分析

> 生成日期: 2026-05-14
> 分析范围: 管理后台模板、数据库、飞书集成代码

---

## 一、整体架构

```
┌─ 管理后台 admin.html ──────────────────────────┐
│  「系统与安全」→「频道管理」 (l_channels)         │
│    ├─ Tab: 飞书(active) / 微信/QQ/钉钉(即将推出) │
│    ├─ 表单: App ID, App Secret, Open ID, Token   │
│    ├─ 保存配置 → PUT /admin/channels/feishu      │
│    └─ 测试连接 → POST /admin/channels/feishu/test│
└──────────────────────────────────────────────────┘
                         │
                         ▼
┌─ 数据库 channel_configs 表 ──────────────────────┐
│  channel='feishu' → config_json (JSON)            │
│    {app_id, app_secret, admin_open_id,            │
│     verification_token, encrypt_key}              │
└──────────────────────────────────────────────────┘
                         │
                         ▼
┌─ community/feishu.py (运行时读取) ────────────────┐
│  _get_db_feishu_config() → 5s 缓存                │
│    ├─ DB 优先 (channel_configs WHERE is_enabled=1)│
│    └─ 环境变量 FEISHU_* fallback                   │
└──────────────────────────────────────────────────┘
```

## 二、数据库设计 — `channel_configs` 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| channel | TEXT UNIQUE | 频道标识: `feishu`, `wechat`, `qq`, `dingtalk` |
| config_json | TEXT | JSON 键值对 |
| is_enabled | INTEGER | 0=禁用 / 1=启用 |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 更新时间 |

创建于 `auth-center/models/database.py` 的 `init_db()` (行 767-788)，自动 seed 一条 `channel='feishu'` 的空记录。

飞书 `config_json` 包含 5 个字段：

| 键 | 前端输入框类型 | 说明 |
|----|-------------|------|
| `app_id` | text | 自建应用 App ID |
| `app_secret` | password (可切换) | 自建应用 Secret |
| `admin_open_id` | text | 管理员接收通知的 open_id |
| `verification_token` | password (可切换) | 回调验证 Token |
| `encrypt_key` | password (可切换) | 加密 Key（通常留空） |

## 三、安全机制：Secret 掩码 + 不覆盖

### 3.1 后端掩码（GET 返回时自动遮蔽）

`get_channel()` / `list_channels()` 中，所有 key 含 `secret`/`token`/`key` 的字段值自动遮蔽：
```
原始: "BOpWwPWVb4YgOCyPgzlbYcMYgn1E60gO"
返回: "BOpW●●●●●●●●●●●●●●●●●●●●●●●●●●●●●"
```
前端看到的永远是掩码值，无法从页面源码中获取真实密钥。

### 3.2 保存时智能合并（掩码值不覆盖）

`update_channel()` (admin.py 行 1664-1697) 收到 PUT 请求后：
1. 读取数据库中**当前的** `config_json`
2. 遍历前端传来的每个字段
3. 如果值中包含 `●`（掩码字符）→ **跳过，保留旧值**
4. 否则 → 更新为新值

这保证了：用户不改密码字段时，旧密码不会丢失。

### 3.3 前端双保险

前端 `_chSave()` (admin.html 行 808-843) 也有自己的 `_chSecrets` 内存缓存：
- 页面加载时，如果是掩码值 → 从 `_chSecrets.feishu` 取真实值
- 用户点击「显示」后手动修改 → 新值直接发送

## 四、前端 UI — 关键函数位置

| 函数 | 行号 | 用途 |
|------|------|------|
| `l_channels()` | 873 | 页面入口，渲染 Tab + 加载飞书 |
| `_chToggleSecret(ch, key)` | 717 | password ↔ text 切换 |
| `_chLoadTab(ch)` | 726 | 加载指定频道配置，渲染表单 |
| `_chToggleEnable()` | 799 | 开关切换 |
| `_chGetValue(key)` | 803 | 读取输入框值 |
| `_chSave()` | 808 | 收集表单 → PUT 保存，掩码智能处理 |
| `_chTest()` | 846 | POST 测试连接（调飞书 token API） |

## 五、后端路由 — `admin.py` 4 个端点

| 端点 | 方法 | 行号 | 用途 |
|------|------|------|------|
| `/admin/channels` | GET | 1601 | 所有频道列表（secret 掩码） |
| `/admin/channels/<channel>` | GET | 1630 | 单个频道配置 + `from_env` fallback |
| `/admin/channels/<channel>` | PUT | 1664 | 保存（UPSERT，掩码值保留旧值） |
| `/admin/channels/<channel>/test` | POST | 1700 | 测试飞书连接（调 `tenant_access_token` API） |

## 六、运行时读取优先级 — `feishu.py`

`community/feishu.py` 中所有配置函数遵循统一优先级：

```
1. DB channel_configs (channel='feishu', is_enabled=1)
   └─ 5 秒缓存 (_DB_CONFIG_CACHE, 行 17)
2. 环境变量 FEISHU_* (fallback)
   └─ 硬编码默认值（仅 app_id/app_secret 有）
```

涉及的函数：

| 函数 | 行号 | 逻辑 |
|------|------|------|
| `_get_db_feishu_config()` | 19 | 返回 config_json dict，5s 缓存 |
| `_get_app_id()` | 45 | DB 优先 → 环境变量 → 默认值 |
| `_get_app_secret()` | 52 | DB 优先 → 环境变量 → 默认值 |
| `get_tenant_token()` | 59 | 用上述两个函数获取凭证，token 也有自己的缓存 |

## 七、部署注意事项

| 改动范围 | 需要重启的服务 |
|----------|-------------|
| admin.html (前端) | 无需重启（`TEMPLATES_AUTO_RELOAD=True`） |
| admin.py (后端路由) | 重启 admin (tmux admin-8084) |
| database.py (表结构) | 重启 admin |
| feishu.py (读取逻辑) | 重启 community (`systemctl restart community.service`) |
| community/app.py | 重启 community |

**环境变量保留不删** — 它们作为 DB 配置的 fallback 继续生效，systemd service 文件无需修改。

## 八、核心设计模式总结

1. **DB 优先、环境变量兜底** — 不改代码就能换飞书应用，同一个配置后台可以无缝切换
2. **Secret 掩码 + 智能合并** — 管理员在后台看不到完整密钥（掩码），但保存时不会因为没改而丢失旧值
3. **5s 缓存** — 避免每次飞书 API 调用都查库，生产环境性能友好
4. **可扩展** — `channel_configs` 用 `channel` 字段区分，微信/QQ/钉钉直接加新行即可，不需要改表结构
5. **双层掩码保护** — 前端 `_chSecrets` 内存缓存 + 后端 `●` 检测，双重保险不丢密钥
