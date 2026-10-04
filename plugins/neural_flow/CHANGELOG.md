# Changelog

## Unreleased

### Changed

- **DEF-27（P2）** `compatible_editions` 由 `["finance"]` 恢复为 `[]`（空数组 = 全版本兼容）。
  NF-03 当初收窄为 finance 的立论是"非金融版实时帧全丢、空转心跳"；该问题现已由
  `probe_outbox()` 在运行期优雅降级（outbox 不可用即 503 + 有界退出，不再空转）解决，
  实时通道不再构成版本收窄理由。同时解开"现网已装、卸载后因版本闸门不可回装"的死结。

## v1.0.2 — 2026-10-03

### Compatibility

- **老内核兼容垫片**：`PluginInstallError` 改为模块级 `try` 导入，0.61.x 等
  PF-02 之前的内核（`plugin_manager.exceptions` 无该异常类）上不再于
  `setup()` 内抛裸 `ImportError`；导入失败时回退为构造签名对齐
  `(identifier, detail)` 的 `RuntimeError` 子类，由老内核既有 setup 异常
  策略记录 `last_error`。新内核行为零变化（导入成功即使用内核异常，
  PF-02 硬失败闸门保持）。

## v1.0.1 — 2026-10-03

依据第三方真实测试报告（111 例动态断言 + 4 例补强取证）修复 14 项缺陷，
插件自带单测由 13 例增至 29 例，全部通过。

### Fixes

- **NF-01（P0）** 修复 `models_nf._SCHEMA` 被顶层逗号解析成 1 元组、后 5 条
  DDL（含唯一索引 `uq_nfs_source`）沦为被丢弃表达式的结构性缺陷：改为显式
  括号元组，`ensure_tables()` 现真实执行全部 6 条 DDL；新增 DDL 覆盖率回归
  断言（实跑语句集合 ⊒ `migrations/v1.0.0_init.sql` 全部 7 条语句）。
- **NF-02（P1）** `setup()` 建表失败由 `return False` 改为抛
  `PluginInstallError`，恢复 PF-02 硬失败闸门，杜绝「ACTIVE 但能力全空」
  的半成品状态。
- **NF-03（P2）** `compatible_editions` 收敛为 `["finance"]`——实时通道唯一
  依赖 `stock_analysis`，非金融版不再出现实时帧全丢的空转心跳。
- **NF-04（P2）** 运行期三域 `llm`/`agent`/`system` 以 platform 子域别名接入
  （`PLATFORM_SUBDOMAINS` + `list_profiles()`），Profile 驱动的域切换器可
  完整表达平台产出；外部档案显式占用同名域时优先外部。
- **NF-05（P2）** `Last-Event-ID` 经 `parse_last_id()` 在 SSE 响应建立前
  校验，非法 header 返回 400，不再在 200 OK 之后崩进生成器。
- **NF-06（P2）** 游标初始化失败后，`run_once()` 在 advisory 锁内惰性重试
  `init_cursors(conn)`（SAVEPOINT 包裹，失败回滚不残留会话锁），启动瞬间
  数据库抖动不再导致可视化永久停摆。
- **NF-07（P2）** `_scan_external()` 对内置保留域（platform/stock）拒载并
  写入 `_ERRORS` 留痕，第三方档案无法再同名覆盖内置渲染档案。
- **NF-08（P3）** `cost_usd` 经 `_safe_cost_usd()` 收敛：非数值 / NaN / ±Inf
  归零，埋点传参失误不再抛穿打断业务主流程。
- **NF-09（P3）** `span_timer` 异常分支透传 `tokens`/`cost_usd`/`confidence`，
  失败调用的消耗如实进账。
- **NF-10（P3）** 修正 `"%s: %s" % (a, b)[:200]` 下标先于 `%` 绑定的 no-op，
  异常摘要正确截断到 200 字符。
- **NF-11（P3）** Profile 校验覆盖可选四区
  decision/routes/statusbar/nouns 的基础结构，畸形档案不再原样下发渲染端。
- **NF-12（P4）** 采集循环移除每轮 `CREATE SCHEMA IF NOT EXISTS`（迁移职责
  归位 `setup()`），保留廉价的 `SET search_path`。
- **NF-13（P4）** `collector_interval_seconds` 双向钳制（1..3600），超上界
  钳制并 warning 留痕，误配不再静默退化为天级采集。
- **NF-14（P4）** `parse_topics` 保序去重，`flow,flow` 不再展开为冗余
  IN 占位。

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
