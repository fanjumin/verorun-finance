# Net Proxy（统一出站网络代理）

## 概述

Net Proxy 是 VeroRun 平台的统一出站网络代理与出口治理插件，集中管理出口通道（直连 / HTTP 代理 / SOCKS5 代理）、按域路由规则、请求日志、连通性探测与熔断，为所有插件的出站流量提供统一的控制平面。

## 功能特性

- **出口通道管理**：创建 / 修改 / 删除代理通道（HTTP / HTTPS / SOCKS5），凭据加密存储（fail-closed，绝不明文落库）
- **按域路由规则**：按目标域名或插件来源标签将流量路由到指定通道
- **熔断器**：连续失败达到阈值后自动熔断通道，冷却时间后自动恢复
- **连通性探测**：定时探测通道端点可用性，记录探测结果
- **请求日志**：每笔代理请求记录来源、目标、延迟与结果
- **区域感知**：通道按区域打标签（`cn` / `os` / `any`），用于路由决策
- **用途标签**：通道按用途打标签（`llm` / `search` / `crawl` / `social` / `push` / `market` / `mail` / `generic`）
- **仪表盘统计**：通道总数、今日请求数、探测健康率、熔断通道数
- **健康检查**：schema 连通性、熔断通道数、24h 出站成功率

## 架构

```
管理后台
    │
    ▼
路由层（/plugin/net_proxy/admin/*）
  总览 / 通道 CRUD / 规则 CRUD / 请求日志 / 探测日志
    │
    ├── channels.py     通道校验 + 代理 URL 构造（纯函数）
    ├── crypto.py       凭据加密（fail-closed，绝不明文落库）
    ├── rules.py        出口规则解析（域名 → 通道查找）
    ├── egress.py       出站请求执行 + 私网拦截
    ├── fuse.py         熔断器逻辑
    ├── region_profile.py  当前区域档案解析
    ├── health.py       结构 / 熔断通道 / 近期成功率健康检查
    └── scheduler.py    定时探测 + 日志保留清理
    │
    ▼
数据层 —— PG schema: net_proxy
  proxy_channels / proxy_rules / proxy_probe_log / proxy_request_log
```

## 配置说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `default_timeout_s` | 10 | 出站请求默认超时（秒） |
| `probe_interval_minutes` | 5 | 通道连通性探测间隔（分钟） |
| `probe_target` | （空） | 探测目标 URL；留空 = 探测通道端点本身 |
| `fuse_threshold` | 3 | 连续失败多少次后熔断 |
| `fuse_cooldown_minutes` | 10 | 熔断冷却时间（分钟），过后自动恢复 |
| `log_retention_days` | 30 | 请求日志保留天数 |
| `default_policy` | `DIRECT` | 无规则匹配时的默认出口策略（`DIRECT` / `PROXY` / `BLOCK`） |

## API 端点

> 所有端点需管理员 JWT。响应契约：`{success: bool, data: ..., error: ...}`。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/plugin/net_proxy/admin/status` | 出口治理总览（通道、健康、今日统计、熔断数） |
| GET | `/plugin/net_proxy/admin/channels` | 通道列表（凭据脱敏） |
| POST | `/plugin/net_proxy/admin/channels` | 新建通道（密码落库前加密） |
| PUT | `/plugin/net_proxy/admin/channels/<id>` | 更新通道（密码留空 = 保留原值） |
| DELETE | `/plugin/net_proxy/admin/channels/<id>` | 删除通道 |
| GET | `/plugin/net_proxy/admin/rules` | 路由规则列表 |
| POST | `/plugin/net_proxy/admin/rules` | 新建路由规则 |
| PUT | `/plugin/net_proxy/admin/rules/<id>` | 更新路由规则 |
| DELETE | `/plugin/net_proxy/admin/rules/<id>` | 删除路由规则 |
| GET | `/plugin/net_proxy/admin/log/requests` | 请求日志（分页，最多 200 行） |
| GET | `/plugin/net_proxy/admin/log/probes` | 探测历史日志 |

## Python 依赖

必需：无（纯标准库）
可选：`PySocks>=1.7.0`（SOCKS5 代理支持）

## 权限

- `api:read`、`api:write`、`network:request`、`routes`、`scheduler`、`health`

## Hook

- **提供**：`net_proxy/get_status`、`net_proxy/get_channels`、`net_proxy/get_request_log`
- **订阅**：无

## 安全

- **凭据加密**：通道密码经 `crypto.py` 加密落库；加密密钥不可用时 fail-closed（绝不存明文），返回 HTTP 503
- **私网拦截**：出站请求强制拦截云元数据、回环、链路本地等保留网段
- **仅管理员**：所有管理端点需管理员 JWT 校验
- **Fail-closed**：加密不可用 → 拒绝凭据写入；熔断开启 → 失败通道不放行流量

## 卸载

卸载时 `DROP SCHEMA net_proxy` 及全部表，零残留。

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一许可证协议。
