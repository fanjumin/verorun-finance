# Changelog

## v1.0.0 — 2026-09-27

### Changes

- 首个正式版本：平台级 AI 数据流可视化基座（NeuralHub 后端）
- 埋点 SDK：`emit_span`（双通道：sa_sse_events 实时 + nf_flow_spans 归档）与
  `span_timer`（start/end 自动成对，异常自动补 error 帧）
- Domain Profile 注册中心：内置 platform / stock 双档案，支持行业插件以
  `domain.yaml` 声明接入（schema 校验、拒载留痕、ASCII domain 白名单）
- 平台采集器：agent_token_logs 增量游标 → llm 域 span（advisory lock 单连接
  持锁互斥；游标初始化失败保持 idle，防全量历史回放；源行幂等唯一索引）
- 端点：`GET /admin/neural-flow/api/profiles`（档案下发）、`/api/spans`
  （回放查询，默认最新优先，时序回放显式 asc）、`/api/events`（SSE 备用
  实时通道，并发闸 NF_SSE_MAX_CONNECTIONS 可配，默认 2）
- EventBridge 订阅：agent.task.completed / plugin.installed / enabled /
  disabled（activate 内 bus.on 幂等订阅，deactivate 反注册）
- 合规：独立 schema `neural_flow`（§9.1/§11.2）、agent_role=ops 聚合（§2.2）、
  per-plugin logger（§10.5）、migrations 目录（§10.6）、i18n 目录（§12.1）、
  EventBus 订阅走 bus.on（manager 斜杠键与 EventBus 点分键空间不相交的实证修复）
