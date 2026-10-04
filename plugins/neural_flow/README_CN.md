# NeuralFlow（神经中枢数据流基座）

## 概述

NeuralFlow 是 VeroRun 平台级 AI 数据流可观测性基座——NeuralHub 仪表盘的后端。它提供 Span、Domain Profile 与跨域回放能力，用于可视化平台 AI Agent 的数据流转全过程。

渲染引擎本身行业无关：所有行业差异通过 **Domain Profile**（`domain.yaml` 或内置档案）声明，新增行业域无需改动渲染引擎代码。股票域（`stock_analysis`）是第一个数据源。

## 功能特性

- **Span**：每个 Agent 任务、LLM 调用、插件生命周期事件都生成一条 Span，归档于 `nf_flow_spans` 表（保留 30 天）
- **Domain Profile**：按行业域声明流水线阶段、决策路由、拱结构与配色——渲染引擎保持通用
- **增量采集器**：基于游标的增量轮询平台表（`agent_token_logs` → LLM 域 Span），多 worker 间 advisory lock 互斥
- **实时 SSE**：复用现有 `stock_analysis` SSE 单连接（`topic=flow`），不新增并发闸占用
- **事件驱动 Span**：订阅 `agent.task.completed` 与插件生命周期事件，生成 agent/system 域 Span
- **档案注册中心**：内置档案（`platform`、`stock`）+ 外部档案（其他插件的 `domain.yaml`）；schema 校验失败拒载并留痕，不阻塞其他域

## 架构

```
NeuralHub 仪表盘（前端）
    │
    ├── 实时：  SSE 经 stock_analysis /api/events（topic=flow）
    │          SDK 双写 → sa_sse_events（表归 stock_analysis 所有）
    │
    └── 归档：  GET /admin/neural-flow/api/spans
                nf_flow_spans（自有 schema，保留 30 天）
    │
    ▼
采集器（interval 任务，默认 5s）
  增量游标轮询 → agent_token_logs → llm 域 span
  advisory lock（0x6E464C57）——多 worker 仅一方执行
    │
    ▼
EventBus 订阅
  agent.task.completed → agent 域 span
  plugin.installed/enabled/disabled → system 域 span + 档案刷新
    │
    ▼
Domain Profile 注册中心（profile_registry.py）
  内置：platform / stock
  外部：其他插件 domain.yaml（schema 校验，失败拒载留痕）
```

## 数据模型

独立 PostgreSQL schema `neural_flow`：

| 表 | 用途 |
|-------|---------|
| `nf_flow_spans` | 归档 Span（domain、trace_id、entity JSONB、payload JSONB、source、source_id） |

索引：`(domain, created_at)`、`(trace_id)`、唯一索引 `(source, source_id)` 保证采集幂等。

## 配置说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `collector_interval_seconds` | 5 | 平台表增量游标轮询间隔（秒） |
| `span_retention_days` | 30 | `nf_flow_spans` 归档保留天数，过后每日清理 |

## API 端点

> 所有端点需管理员 JWT。响应契约：`{ok: bool, data: ..., error: ...}`。
> 前缀：`/admin/neural-flow`

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| GET | `/api/profiles` | Domain Profile 注册表（含拒载档案与错误详情） |
| GET | `/api/spans` | Span 回放查询（`from`、`to`、`domain`、`trace_id`、`limit` ≤ 2000） |
| GET | `/api/events` | SSE 实时流（`topics=flow`，支持 `Last-Event-ID` 断线续传） |

### SSE 并发控制

- 默认最大并发 SSE 连接：**2**（环境变量 `NF_SSE_MAX_CONNECTIONS`，范围 1–16）
- 闸满 → HTTP 503 + `Retry-After: 10`（不排队）
- 出流依赖不可用 → HTTP 503 + `Retry-After: 60`（建流前拒，不占闸位）

## Python 依赖

必需：无
可选：`pyyaml`（外部 Domain Profile 扫描；缺失时仅内置档案可用）

## 权限

- `routes`、`scheduler`、`events`

## Hook

- **提供**：无
- **订阅**：`agent.task.completed`、`plugin.installed`、`plugin.enabled`、`plugin.disabled`

## 兼容版本

- 全部版本（`compatible_editions: []`，无限制）

## 卸载

卸载时删除 `neural_flow` schema 与 `nf_flow_spans` 表，零残留。若 schema 内存在外来对象，则拒绝 DROP（宁留残表不误删）。

## 许可证

本插件为 VeroRun 平台的一部分，遵循平台统一许可证协议。
