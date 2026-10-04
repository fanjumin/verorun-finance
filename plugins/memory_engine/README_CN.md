# 认知进化 CogEvolution (memory_engine)

面向多智能体系统的分层记忆、反思自进化与 Prompt 指标引擎。

## 功能

- **长期记忆**: 向量记忆存储于独立 `memory_engine` PostgreSQL schema。支持用户级、全局级和 Agent 级作用域。
- **反思引擎**: 失败或低置信度任务自动触发反思，产出结构化 `{问题, 教训, 行动, 评分}` 记录，反哺 Agent 行为。
- **Prompt 进化**: 每日按 Prompt 版本聚合指标（成功率、平均评分、Token 消耗）。建议采用双比例 Z 检验对每个 Agent 最近两个版本做比较，仅当两版本样本均 ≥10 条时才生成显著性标记；是否应用仍由管理员手动决定。
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
| `prompt_evolution_enabled` | false | 是否启用 Prompt 进化建议 |
| `daily_extract_budget` | 200 | 每日提取调用次数上限 |
| `enable_sedimentation` | true | 高价值记忆经管理员审核后沉淀至共享知识库 |
| `sedimentation_fact_min_confidence` | 0.9 | 事实记忆沉淀的最低置信度 |
| `sedimentation_lesson_min_rating` | 4 | 教训记忆沉淀的最低反思评分（1-5） |
| `sedimentation_min_quality_score` | 0.7 | 记忆沉淀的最低质量分 |
| `sedimentation_daily_budget` | 50 | 每日沉淀扫描候选上限 |
| `abtest_enabled` | false | 50/50 确定性分流用户并记录任务结局，度量记忆注入收益 |
| `abtest_control_pct` | 50 | 对照组百分比（0-100） |

## 管理界面

- `/admin/memory` — 记忆浏览器：关键词搜索、按类型/所有者过滤、软删除
- **进化环** 标签页 — 动态环可视化，含轮次时间轴播放器、阶段钻取面板、Agent 过滤

## A/B 测试与评测

用于度量「记忆注入是否真的有效」的两项可选能力，默认全部关闭。

### 注入 A/B 测试

- **分流键是用户，不是会话。** `sha256('abtest|<user_id>') % 100 < abtest_control_pct` → `control`，否则 `treatment`。`before_prompt_resolve` 挂点只拿到 `user_id`、`agent_id`、`user_query`、`task_type`，没有 `session_id`/`task_id` 可供分流。因此同一用户恒定落在同一臂（无闪烁），分流在用户之间随机。统计请以「用户 × 天」为单元。
- **行为：** 对照臂用户完全不注入记忆块；实验臂用户照常注入。`abtest_enabled=false`（默认）时行为与未安装本能力完全一致。
- **结局记录：** 每次 `agent.task.completed` 向 `ab_events` 写入一行（`arm`、`injected`、`block_len`、`failed`、`confidence`、`retries`），对照臂同样落行（`injected=false`）。Token 消耗在出报告时按 `task_id` 从 `agent_token_logs` 只读回查。以 `(task_id, user_id)` 去重；写入异常静默，绝不影响任务主链路。
- **报告：** `GET /admin/memory/abtest/report?days=14&min_sample=30` — 按臂给出样本量、成功率、平均置信度、平均 Token，并附双比例 Z 检验（显著性 ±1.96），结论取值 `memory_helps` / `memory_hurts` / `no_significant_difference` / `insufficient`。

### 检索回归评测

零 LLM 成本的评测夹具，直接驱动检索层（`MemoryRetriever`）对固定用例集打分——Prompt 或检索逻辑改动后可做回归验证，无需支付推理费用。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/memory/eval/cases?active=true` | 列出评测用例。 |
| POST | `/admin/memory/eval/cases` | 新增用例（`name`、`owner_id`、`query`；可选 `agent_id`、`expected_memory_id`、`expected_keywords`）。 |
| POST | `/admin/memory/eval/run` | 跑全部活跃用例，结果留档到 `eval_runs`。 |
| GET | `/admin/memory/eval/runs` | 历史运行（`case_count`、`hit_at_k`、`mrr`），用于变更前后对比。 |

打分基于 Top-5 检索（`_TOP_K = 5`）：**hit@5** 为 1 表示期望记忆 id 或任一期望关键词出现在结果中；**MRR** 为首次命中排名的 `1 / rank` 的平均值。

**已知项：** 评测运行会推进命中行的 `hit_count` / `last_hit_at`（检索层始终记账），因此评测运行会在「今日注入」统计中可见；评测指标本身不受影响。

**已知项：** `ab_events.block_len` 记录的是实际处理该提示词的 worker 观测到的注入块长度。多 gunicorn worker 下，任务完成事件可能由缓存值为 `0` 或过期的 worker 处理，故实验臂偶见 `block_len = 0`。报告仅在臂级聚合，不影响结论。

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

## 已知限制

- **success_rate 口径基于反思样本而非全量任务**: Reflexion 默认只记录失败/低置信度任务（`reflexion_failure_only=true`），故 `prompt_metrics.success_rate` 表示"反思样本中未失败占比"而非全量任务成功率。
- **`prompt_evolution_enabled` 配置门控**: 每日 prompt 指标聚合与进化轮次归档仅当该布尔配置显式为 `true` 时运行。此前代码从未读取此标志——这是向 plugin.json 声明的对齐（opt-in 策略）。
- **Agent 作用域检索**: 用户记忆按 `agent_id` 过滤检索（S10）。`agent_id=''` 的记忆视为通用记忆、注入所有 Agent；`owner_type='global'` 的全局记忆不受 agent 过滤影响。
- **pgvector 缺失**: 无 `vector` 扩展时 `embedding` 列降级为 `TEXT`，所有向量路径自动退化为关键词检索。
