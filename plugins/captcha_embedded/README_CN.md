# Captcha Service (captcha_embedded)

## 概述

Captcha Service 是 VeroRun 的滑块验证码插件，提供拼图生成、行为分析与频率限制能力。插件采用自包含架构：核心逻辑（生成器、安全、行为分析、存储）内聚于插件自身 `captcha/` 包，REST 路由由插件自有 `routes.py` 暴露（url_prefix 保持 `/api/captcha`）。

版本：**1.0.0**

## 功能特性

- **滑块验证码生成**：动态生成拼图式滑块验证码，包含背景图和滑块图
- **行为分析**：分析用户拖动滑块的行为特征（轨迹、速度、时间），判断是否为真实用户操作
- **频率限制**：内置频率限制机制，防止验证码接口被恶意滥用
- **无 Cookie 验证**：验证过程不依赖客户端 Cookie，通过服务端令牌进行状态管理
- **轻量级集成**：作为嵌入式插件，通过 Hook 接口对外暴露验证能力

## 架构设计

### 数据库策略

插件生产环境使用 **Redis + 内存回退** 存储验证码临时状态（token、限流、封禁、统计）。
Redis 本身即持久化方案（RDB/AOF），对短生命周期（TTL≤300s）的验证码数据是合理选择，
热路径（每次 generate/verify/consume）延迟低。

按插件标准 v1.4 §9.1/§11.2，`models.py` 已声明此存储策略，并预留 PostgreSQL schema
`captcha_embedded`（表定义 + `init_captcha_db()`）；未来如需全量迁移 PG 可直接调用初始化
后切换 `store.py` 读写层。

### 模块结构

```
┌─────────────────────────────────────────────────┐
│                __init__.py                       │
│          (插件生命周期 + 路由注册 + Dashboard)     │
└─────────────────────┬───────────────────────────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
┌──────────────┐ ┌──────────┐ ┌──────────────┐
│ routes.py    │ │ captcha/ │ │ config.py    │
│ Blueprint    │ │ 生成器/   │ │ 配置         │
│ /api/captcha │ │ 安全/行为/│ │ (SECRET_KEY, │
│ (generate/   │ │ 存储      │ │ IMAGE_DIR…)  │
│ verify/      │ │           │ │              │
│ consume/     │ │           │ │              │
│ stats)       │ │           │ │              │
└──────────────┘ └──────────┘ └──────────────┘
```

### 验证流程

1. **生成阶段**：客户端请求 `captcha/generate` Hook，服务端生成拼图图片和验证令牌
2. **展示阶段**：前端渲染滑块验证码，用户拖动滑块完成拼图
3. **验证阶段**：前端提交拖动行为数据，服务端通过 `captcha/verify` Hook 进行行为分析
4. **消费阶段**：业务方调用 `captcha/consume` Hook 消费验证令牌，确保验证码一次性使用

## 目录结构

```
captcha_embedded/
├── __init__.py          # 插件入口：生命周期 + 路由注册 + Dashboard 统计
├── plugin.json          # 插件元数据配置（plugin-standard v1.4 合规）
├── routes.py            # Blueprint（url_prefix=/api/captcha，从 admin/captcha_bp.py 迁入）
├── config.py            # 配置（从 captcha-service/config.py 迁入，含延迟 SECRET_KEY 校验）
├── models.py            # 存储策略声明 + PG schema captcha_embedded 预留（init_captcha_db）
├── images/              # 拼图背景图（自包含，26 张，从 captcha-service/images 复制）
├── captcha/             # 核心逻辑（从 captcha-service/captcha/ 迁入）
│   ├── __init__.py
│   ├── generator.py     # 拼图生成
│   ├── security.py      # HMAC token 生成/校验（延迟获取密钥）
│   ├── behavior.py      # 行为轨迹分析与风险评分
│   └── store.py         # 存储：Redis + 内存 fallback
├── templates/
│   └── captcha_stats.html  # 统计页裸 JS partial（§12.11 无 <script>）
└── i18n/
    ├── en.yml           # 英文国际化（identity 映射）
    └── zh-CN.yml        # 中文国际化
```

## 安装与启用

### 安装

插件已包含在 VeroRun 的默认插件目录中，无需额外安装步骤。

### 启用

1. 在 VeroRun 管理后台 "插件管理" 页面中启用 Captcha Service 插件
2. 插件启用后，`captcha/generate`、`captcha/verify`、`captcha/consume` 三个 Hook 将自动注册
3. 前端页面可调用验证码 Hook 进行人机验证

## 配置说明

在 `plugin.json` 中配置以下参数：

```json
{
  "name": "captcha_embedded",
  "version": "0.1.0",
  "captcha": {
    "puzzle_size": {
      "width": 60,
      "height": 60
    },
    "background_size": {
      "width": 320,
      "height": 160
    },
    "tolerance": 5,
    "expire_seconds": 300,
    "rate_limit": {
      "max_requests_per_minute": 10,
      "max_requests_per_ip_per_minute": 5
    }
  }
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `captcha.puzzle_size.width` | 拼图块宽度（像素） | `60` |
| `captcha.puzzle_size.height` | 拼图块高度（像素） | `60` |
| `captcha.background_size.width` | 背景图宽度（像素） | `320` |
| `captcha.background_size.height` | 背景图高度（像素） | `160` |
| `captcha.tolerance` | 滑块位置容差（像素） | `5` |
| `captcha.expire_seconds` | 验证令牌过期时间（秒） | `300` |
| `captcha.rate_limit.max_requests_per_minute` | 每分钟最大请求数 | `10` |
| `captcha.rate_limit.max_requests_per_ip_per_minute` | 每 IP 每分钟最大请求数 | `5` |

## API 端点

### Hook 提供

| Hook 标识符 | 类型 | 说明 |
|-------------|------|------|
| `captcha/generate` | Hook | 生成验证码拼图，返回图片数据与验证令牌 |
| `captcha/verify` | Hook | 验证用户拖动行为，返回验证结果 |
| `captcha/consume` | Hook | 消费验证令牌，标记验证码已使用 |

### 管理后台

本插件无独立管理菜单。`templates/captcha_stats.html` 为统计页裸 JS partial
（§12.11 无 `<script>`），数据来自 `GET /api/captcha/admin/stats/`，主系统可按需加载。

## 依赖关系

### 内部依赖

- VeroRun 核心框架：Hook 系统、PluginManager 路由注册（register_routes）
- 拼图背景图：插件内 `images/` 目录（自包含，经 `config.IMAGE_DIR` 引用，环境变量可覆盖）

### 外部依赖

- 第三方 Python 包：Pillow（拼图生成）、numpy（像素处理）、redis（存储，可选）

### 被依赖

- 任何需要进行人机验证的业务模块均可通过 Hook 调用本插件的验证码能力
- 常见使用场景：登录表单、注册表单、敏感操作确认

## 许可证

本插件为 VeroRun 项目的一部分，遵循 VeroRun 项目的整体许可证协议。