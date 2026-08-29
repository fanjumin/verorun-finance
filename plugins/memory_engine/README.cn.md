# 认知进化 CogEvolution (memory_engine)

面向多智能体系统的分层记忆、反思自进化与 Prompt 指标引擎。

## 功能

- **分层记忆**: 工作记忆 + 长期向量记忆，存储于独立 `memory_engine` PostgreSQL schema。支持用户级、全局级和 Agent 级作用域。
- **反思引擎**: 失败或低置信度任务自动触发反思，产出结构化 `{问题, 教训, 行动, 评分}` 记录，反哺 Agent 行为。
- **Prompt 进化**: 每日按 Prompt 版本聚合指标（成功率、平均评分、Token 消耗），达到统计显著性后生成优化建议，经管理员审批后生效。
- **进化环**: 纯 SVG 交互式可视化。多轮回放（播放/暂停/上一轮/下一轮），点击阶段节点可钻取该阶段产物（记忆、反思、Prompt 版本）。
- **隐私优先**: 用户级 Opt-in 开关（配置默认值 + `user_profiles.meta` 逐用户覆盖）、PII 过滤（密码/密钥/手机号/身份证）、独立 schema 隔离。
- **优雅降级**: pgvector 为可选依赖，缺失时自动切换关键词检索，功能不中断。

## 安装

1. 确保 PostgreSQL 16+ 可用（pgvector 扩展为可选，缺失时关键词检索兜底）。
2. 将本插件目录放入系统的 `plugins/memory_engine/`。
3. 在管理后台 → 插件管理 中启用本插件。
4. 首次启用时自动执行 schema 迁移，无需手动执行 SQL。

## 配置

所有配置项在插件设置页面中管理：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `embedding_dim` | 1536 | 向量维度 |
| `top_k` | 5 | 每次检索返回的记忆数 |
| `max_memory_block_len` | 1200 | 注入到系统提示中的记忆块最大字符数 |
| `enable_auto_extract` | true | 任务完成后自动提取记忆 |
| `enable_reflexion` | true | 失败/低置信度任务触发反思 |
| `reflexion_min_confidence` | 0.4 | 触发反思的置信度阈值 |
| `reflexion_failure_only` | true | 仅对失败任务反思 |
| `retention_days` | 365 | 记忆保留天数（超期自动归档） |
| `max_memories_per_owner` | 500 | 每用户记忆上限（超限归档最旧/最低质量） |
| `allow_global_memory` | false | 启用管理员维护的跨用户全局记忆 |
| `memory_opt_in_default` | true | 新用户默认是否同意记忆收集 |
| `daily_extract_budget` | 200 | 每日提取调用次数上限 |

## 管理界面

- `/admin/memory` — 记忆浏览器：关键词搜索、按类型/所有者过滤、软删除
- **进化环** 标签页 — 动态环可视化，含轮次时间轴播放器、阶段钻取面板、Agent 过滤

## 架构

全部业务逻辑位于 `plugins/memory_engine/`。AI 引擎内核仅需三处低侵入补丁：

1. **补丁 A** — `UnifiedLLM.get_embedding()`: 向量化能力
2. **补丁 B** — `EventName.AGENT_TASK_COMPLETED`: Agent 每次运行结束后发射任务完成事件
3. **补丁 C** — `before_prompt_resolve` 过滤器挂点: 记忆块注入系统提示的入口

插件不直接持有或调用 LLM——所有推理经内核 `AgentRunner`（含模型策略与预算闸门），向量化经内核嵌入能力。成本与模型选择权始终在管理员手中。

### 数据流

```
任务完成 → EventBus → MemoryExtractor（异步）→ memories 表
新会话 → PromptResolver → before_prompt_resolve 过滤器 → MemoryRetriever → 注入记忆块
检测到失败 → ReflexionService（异步）→ reflexion_logs + lesson 记忆
每日定时 → PromptEvolutionService → prompt_metrics 聚合 + 轮次归档
管理后台 → 进化环 → GET /admin/memory/graph → SVG 可视化
```

### 数据库

全部插件数据位于 `memory_engine` PostgreSQL schema，与主系统 schema 完全隔离：

| 表 | 用途 |
|----|------|
| `memories` | 分层记忆（偏好/事实/决策/纠正/教训） |
| `reflexion_logs` | 结构化反思记录 |
| `prompt_metrics` | 各版本 Prompt 性能指标 |
| `evolution_rounds` | 进化生命周期轮次（进化环数据源） |
| `schema_version` | 迁移版本追踪 |

卸载时执行 `DROP SCHEMA memory_engine CASCADE`，零残留。

### 内置 Agent

- **Memory Curator** (`memory_curator`): tier-`cheap` 子代理，负责记忆提取与反思。使用结构化 JSON 输出契约。Prompt 文件：`agents/memory_curator_prompt.md`。
