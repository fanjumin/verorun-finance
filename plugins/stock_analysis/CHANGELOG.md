# Changelog

## Unreleased（内核红利接线 · 方案《VeroRun 投研内核红利接线实施方案 v1.0》：A1–A3 + 阶段 B）

### Features

- **接线点② 误信号回流 Reflexion（A1）**：新增 `reflexion_feedback.py`——`signal_quality.realize_signals` 回算尾部按 `DAILY_CAP=5` / 偏离度降序筛出方向相悖误信号（buy 后 5 日跌幅超 3% / sell 后涨幅超 3%），以合成 `AGENT_TASK_COMPLETED(failed=True)` 事件回流内核 Reflexion，经 memory_engine 产出教训记忆并注入后续研判 prompt；发射侧全静默 try/except 隔离，事件总线缺失 / Reflexion 关闭均不影响回算主链路。
- **接线点③ 事件联动链（A2）**：`plugin.json` hooks 声明 `stock.batch.completed`（provides）+ `scheduler.job_completed`（listens），settings 新增 `auto_deep_research_on_batch`（默认关）；`__init__.py` 按 memory_engine 范式注册/注销 `_on_job_completed` 处理器；因内核调度器不发射 `SCHEDULER_JOB_COMPLETED`，改由 `batch.run_batch` 收尾自产事件（advisory lock 单跑 + run_id 幂等）→ 命中 job 后对当日高置信标的（`latest_high_confidence_symbols`，置信度 ≥0.7 取前 3）自动入队深研（`submit_job` full）。
- **接线点⑤ 场景化 Prompt（A3）**：`stock_skill.py` 新增 `scenario_task_type()`（交易日 9:30 / 15:00 时段打标 `stock.preopen` / `stock.intraday` / `stock.postclose`，非交易日回退 `postclose`）与 `_scene_prompt()`（对齐内核 PromptResolver 第三层语义：`agent_prompts` 表 scene 行按 `task_triggers` 精确匹配、`priority DESC, version DESC` 取首条命中）；`_call_llm` 系统提示词组装尾部按当前场景追加模板，场景缺失/异常自动回退原 prompt（零事故）。
- 新增 `tests/test_scenario_prompt.py`：9:30 / 15:00 / 非交易日打标边界、`_scene_prompt` 空表/命中/未命中/malformed triggers 容错、`_call_llm` 场景追加与回退路径（全 mock，不触网不连库）。
- **接线点① 多空对辩研判（阶段 B，模式 A 异步任务）**：新增 `discuss_research.py`——插件侧复刻内核 Agent Discussion 四阶段协议（Planner 多头 → Reviewer 风控空头 → Revise 修订 → Decider 决策委员，生成与评审分离），角色语义归属股票投研而非内核硬编码的建站/运维域；四阶段提示词英文呈现（既定 i18n 标准），Decider 要求中文研判正文 + 尾部严格 JSON；`jobs_queue` 新增 `submit_discuss_job`（scope/type=discuss，同日幂等复用）与 `_run_discuss_job`（取证据 → 对辩 → 终态 + `record_signal(kind='discuss')` + KB 幂等沉淀 `kb_stock_<symbol>_<日期>_discuss`），受既有 advisory lock / `_MAX_CONCURRENT=2` 保护；`POST /api/discuss` 入队返回 job_id，状态走既有 `GET /api/jobs/<id>` 契约（规避 4 轮 LLM 同步链路 Nginx 60s 504）；`sse_stream` 放行 `discuss` 主题并对辩逐轮经 `sa_sse_events` 出流（B-3，事件名 `discuss.round`）；`record_signal` 语义兼容 16:00 信号兑现回算（hold 自然跳过）。
- 新增 `tests/test_discuss_research.py`：PROMPTS 四键/占位符、四轮调用与 emit 顺序、结构化命中/兜底两路径、emit 异常隔离、中途失败传播（LLM_FACTORY 注入缝，不触网不连库）。

### Notes

- **版本未升**：本批为内核红利接线项，plugin.json 版本保持 1.7.1，随下次发布统一 bump。
- **运行期动作（服务器执行）**：`hooks.provides/listens` 声明修改后需在管理端对插件 **disable→enable** 激活（平台事实：仅修改声明不会重新同步）；`auto_deep_research_on_batch` 默认关闭，逐项灰度开启。
- **接线点② 生效前提（方案 §3.4）**：`system_config.prompt_resolver_enabled=true`、memory_engine `enable_reflexion=true`、`reflexion_failure_only=true`（默认值均符合）。

## v2.0.2 — 2026-10-01

### Notes

- **版本号对齐说明**：plugin.json 当前为 `2.0.2`；本 CHANGELOG 最新正式记录为 `v1.7.1`，`1.8.0 ~ 2.0.x` 的变更未在此留档。此处仅补登当前版本号，不回溯虚构历史；实现详情以 README 与代码为准。

## v1.7.1 — 2026-09-03

### Fixes（第三方审计 D-1/D-4/D-3 收尾，随本版正式发布）

- **#SA-20260903-03（P1）A4 结构化解析主链路接线**：`_llm_analysis` 信号提取补接 `parse_structured_output` 优先链（`_parse_structured` 命中即用、未命中回退 `_extract_signal`）；1.7.0 发布提交曾遗漏接线，现「命中即用、未命中回退」宣称与代码一致，`evidence_refs`/confidence 收敛/reasons 过滤随主研判生效。
- **#SA-20260903-04（P3）KB 沉淀收敛至调用方**：移除 `_llm_analysis` 内部沉淀，统一由 `routes.analyze` / `jobs_queue` 按 `analysis_type` 沉淀，消除 llm 类任务同锚点双写 + 双向量化（对齐审计方案 b）。
- **#SA-20260903-05（P3）工程欠账**：`requirements.txt` 显式声明 `numpy>=2.0`（不再依赖 pandas 传递引入）；清理经全仓引用核验的死代码：`stock_skill._market_symbol`/`_index_symbol`/`_TENCENT_FIELDS`/`_ff`/`_combined_analysis`、`models_sa.get_alert`（未知类型回落技术面语义不变）。

### Notes

- 版本号 1.7.0 → 1.7.1（plugin.json / SKILL.md / README 对齐）。
- **运行期动作（服务器 + 管理端执行，仓库无法自验）**：`git pull` → 重启 admin/main → 插件 **disable→enable** 激活 `mcp_servers`/`hooks` 声明（平台事实：仅修改声明不会重新同步）→ 黑盒确认 `mcp__stock_analysis__stock__*` 工具与告警钩子可消费 → SKILL.md 经 `/skills/submit|import` 注册并审核晋升 approved → A2 技术指标基线 `tools/capture_baseline.py` 重录归档 → 按手册第八章验收清单逐项回执。

## v1.7.0 — 2026-09-03

### Features（阶段 S/A/B，对齐《VeroRun股票分析系统实施手册 2026-09-03》）

- **S2 交易日历**：`market_calendar.py` 新增进程内年度缓存（`_load_year` + `_YEAR_CACHE`），同一年度只解析一次 CSV；内置 `data/calendar/2024.csv`（242 交易日）与 `2025.csv`（243 交易日）；新增 `tools/gen_calendar.py` 生成脚本（tushare token 版）。缺失年度走周末兜底（2027 休市安排交易所未公布前不伪造，待公布后用脚本补齐）。
- **S4 信号全路径落库**：`models_sa.py` 新增 `record_signal()`（同锚点幂等、空信号不落行、截断 8 字符），`routes.analyze` 与 `jobs_queue._process` 成功分支统一调用，失败仅告警不影响响应；signal 记录不再依赖 `upsert_result` 间接写入。
- **S6 下架无能力端点**：删除 `/api/sectors` 路由与 `stock_skill.sector_ranking()` 及 CLI `--sectors`（硬编码 501 的假能力），行业数据待真实源接入后重建。
- **A1 证据链注入**：新增 `evidence.py`——`build_evidence_context()` 打包财报四表摘要/资金流/新闻/估值分位入 prompt（每子源独立降级并留痕「未获得(原因)」，预算按剩余字符截断），`digest_fundamental()` 对脏记录容错；`_build_llm_prompt` 注入证据段 + 复权口径标注。
- **A2 指标口径统一**：信号路径 RSI（`_technical_snapshot`）由 Cutler 平滑改为 `indicators.rsi()` Wilder 权威实现（阈值 25/40/70/75 不变），消除信号路径与引擎展示路径的双实现漂移。**口径变更：技术指标基线需随版重录归档。**
- **A4 结构化解析**：`parse_structured_output()` 解析 LLM 围栏/裸 JSON（非法值收敛、reasons 过滤、多 JSON 块取尾）；`_parse_structured` 命中即用、未命中回退 `_extract_signal`。
- **B1 MCP 工具服务器**：新增 `tools/mcp_server.py`（行分隔 JSON-RPC 2.0 over stdio），暴露 5 个快路径工具 `get_quote/get_kline/get_technical_signal/get_fundamental_digest/market_overview`；`plugin.json` 新增 `mcp_servers` 声明（name=stock）。stdout 仅协议帧、日志走 stderr、工具异常一律 isError。
- **B2 知识库沉淀**：新增 `kb_publish.py`——成功分析结论写 `public.knowledge_blocks`（幂等锚点 `kb_stock_<symbol>_<日期>_<kind>` + ON CONFLICT DO NOTHING）并尽力向量化；jobs 成功分支与 LLM 分析返回前接线，失败仅留痕。
- **B3 技能注册 + USAGE**：`SKILL.md` 按平台技能格式重写（补 `identifier`/`tagline`，version 对齐 1.7.0，消除 v1.0 标题与 frontmatter 1.6.0 矛盾）；新增 `USAGE.md`（能力清单/端点表/tushare token 配置/桌面端对接契约），商店同步自动入 KB。
- **B4 告警钩子**：`alert_engine` 每次 SSE 告警写入后派发 `stock.alert.triggered` 钩子；`plugin.json` `hooks.provides` 声明，供通知/审计/第三方推送消费者接入，钩子故障不影响 60s 扫描主链路。
- **B6 深度研判 DAG 节点**：新增 `deep_research.py`——`stock_deep_research` 节点（严格 `(node_def, input_data)` 两参），复用上游 `node_*_output.evidence_text`，LLM 失败收敛 `success=False`；`__init__.py` 新增 `register_dag_nodes()`。

### Fixes

- **#SA-20260903-01（P1）akshare 降级路径 NameError**：`akshare_provider` 降级分支引用未定义的模块日志器 `_log`（文件从未 `import logging` 并命名），raw 不可用需降级 hfq-only 帧时抛 NameError。修复：模块级补 `import logging` + `_log` 命名。
- **#SA-20260903-02（P2）config.yaml 大写键不生效**：`apply_preference` 只读 `data_provider` 小写键，仅 DB 设置页（小写 schema）路径生效，config.yaml 大写 `DATA_PROVIDER` 被忽略。修复：`data_provider` 小写优先、`DATA_PROVIDER` 兜底。

### Notes

- **B5**：`register_agents()` 为历史死契约代码，保留但不再作为能力承诺（README 已修订）；v1.7.0 启用校验（核心角色校验与能力合并）在启用日志确认。
- **S1 黑盒验收前置**：akshare 降级修复、信号落库、MCP/B2/B4/B6 生效项均需服务器部署后按手册第八章验收（重启 admin，disable→enable 使 MCP/钩子声明生效）。
- **#SA-20260903-03（P1，审计 D-1 修复）**：`_llm_analysis` 信号提取补接 `parse_structured_output` 结构化解析优先链（发布提交曾遗漏接线，原只走 `_extract_signal` 兜底；修复后"命中即用、未命中回退"宣称成立）。
- **#SA-20260903-04（P3，审计 D-4）**：KB 沉淀收敛至调用方——移除 `_llm_analysis` 内部沉淀，统一由 `routes.analyze` / `jobs_queue` 按 `analysis_type` 沉淀（消除 llm 类任务同锚点双写 + 双向量化；语义对齐审计方案 b）。
- **#SA-20260903-05（P3，审计 D-3 工程欠账）**：`requirements.txt` 显式声明 `numpy>=2.0`（此前仅靠 pandas 传递引入）；清理经全仓引用核验的死代码：`stock_skill._market_symbol`/`_index_symbol`/`_TENCENT_FIELDS`/`_ff`/`_combined_analysis`、`models_sa.get_alert`（`analyze()` 未知类型回落技术面的语义保持不变，行为与修复前一致）。

## v1.6.0 — 2026-09-01

### Features（P0 三件套）

- **P0-1 复权**：技术指标改在后复权空间计算（`*_hfq` 列），展示/互验保留不复权实际价，`price_basis` 列留痕口径。tushare 经 `adj_factor` 后复权、akshare `adjust="hfq"`、新浪兜底降级不复权。修复除权/分红造成的均线与趋势假信号。
- **P0-2 信号兑现回测闭环**：新增 `sa_signal_realized` 表 + `signal_quality.py`，只读回算历史信号 T+5/T+20 收益与命中率；新增 `GET /api/signal-quality`、`POST /api/signal-realize` 与交易日 16:00 回算任务。信号写入路径零改动。
- **P0-3 文档对齐**：修正版本漂移、`subprocess` 安全声明、两级缓存与数据源表描述。

### Fixes

- **#SA-20260901-01（P1）`/api/signal-quality` 冷启动首调 500**：端点未先建表兜底，`sa_signal_realized` 表不存在时聚合查询抛异常→500。修复：端点内先 `_sa()`（ensure_tables 幂等，沿用 #SA-20260831-05 模式）。`/api/signal-realize` 因 `signal_quality.realize_signals` 内部已有 ensure_tables 不受影响。
- **#SA-20260901-02（P1）akshare hfq 在生产走不到**：旧实现以 raw 为前置门槛，raw 失败即整链 failover 到新浪（不复权），东财 hfq 正常时后复权也不生效（部署实测根因）。修复（F4）：akshare 改为 **hfq 优先 + raw 尽力而为**——hfq 失败才 failover；raw 可用则 raw 供展示/互验 + hfq 供指标；raw 不可用则降级 hfq-only 帧（指标仍正确）并打 `_raw_unavailable` 标记，网关 `_crosscheck` 对该标记跳过互验（避免 hfq 价 vs 实时价假冲突）。
- **#SA-20260901-03（P2）设置页 token 录入断链**：`settings_schema.tushare_token` 已提供设置页录入，但 `tushare_client.resolve_token()` 只读环境变量/config.yaml，设置页保存的 token 不生效。修复：`resolve_token()` 优先级改为 环境变量 > PluginManager 持久化配置（`pm.get_config("stock_analysis").tushare_token`）> config.yaml；无 Flask 上下文/异常时安全回落，不阻塞探针。注意：token 经探针缓存至进程生命周期，设置页保存后需重启 admin 服务生效。

## v1.5.2 — 2026-08-31

### Fixes（第三方重测报告驱动 · #SA-20260831-12 /13/14）

- **#SA-20260831-14（P0）K 线落盘缓存 data_date 陈旧**：当日早间（收盘前）写入的 K 线缓存其末行停留在上一交易日，`_disk_get` 仅按「当天有效」判定导致整天命中旧数据（ANALYZE-01/EDGE-08 仍报 data_date=2026-08-28 的根因）。修复：`market_calendar.py` 新增 `latest_trading_day`/`recent_trading_days`；`gateway._disk_put` 记录 `data_date`，`_dispatch` 缓存命中路径新增 `_kline_cache_stale` 校验——交易日已收盘（≥15:00）时末行必须已更新到最近交易日，否则视为过期落回网络链重取；盘中/节假日不误伤。
- **#SA-20260831-13（P0）batch already-run 缺统计字段**：`run_batch` 幂等分支 `already-run` 返回 `{"skipped":...}` 缺 total/ok/failed/status（BATCH-02 判定失败）。修复：`models_sa.get_run_stats(run_id)` 查询批次统计，`batch.py` already-run 分支返回完整契约（DB 无记录时保留 skipped 兜底）。
- **#SA-20260831-11（P1）导出 500 复现（v1.5.1 遗留 NameError）**：v1.5.1 将 `batch_export` 改为 `current_app.json.dumps` 但模块顶部未导入 `current_app`，运行时 NameError→500。修复：`routes.py` 顶部 import 补 `current_app`。
- **#SA-20260831-12（P1）akshare 依赖缺失**：服务器环境未安装 akshare，北交所/非新浪标的取数失败（EDGE-07/EDGE-04）。此为部署环境项：服务器需 `pip install akshare`；代码侧错误文案与 `retryable=False` 语义在 v1.5.1-pre 已就绪。

## v1.5.1 — 2026-08-31

### Fixes（第三方回归报告驱动 · #SA-20260831-01 ~ /11）

- **#SA-20260831-06（P0）PB 系统性失真**：腾讯 88 字段中 `parts[47]/[48]` 实为涨停价/跌停价（=昨收×1.1/0.9），原被错误映射为「每股净资产」，导致 PB 恒落 0.90~0.93、评分恒 85、`pb>=6` 估值风险分支成死代码、茅台信号 HOLD(55)→BUY(85)。修复：`providers/tencent.py` 市净率直接取 `parts[46]`，`stock_skill._fundamental_analysis` PB 直读 `basic["pb"]`；PB 不可用时跳过 PB 评分并输出「PB 数据不可用」披露行（含 reasons 留痕）。
- **#SA-20260831-07（P1）新闻语料噪声**：`providers/sina.py` 由全页 `<a>` 抓取改为仅提取 `<div class="datelist">` 新闻列表容器内锚点，并剔除 JS 模板字符串，消除站内导航/功能链接噪声（实测噪声占比约 1/3 降为 0）。
- **#SA-20260831-08（P1）crosscheck 冲突条目缺三元组**：`gateway._crosscheck` 冲突留痕补齐 `authorized/fetched_at`，满足 data_sources 每条含 source/authorized/fetched_at 合规约束。
- **#SA-20260831-05（P1）全新部署 watchlist/导出 500**：`routes.py` 新增 `_sa()` 建表兜底（沿用 batch 的 ensure_tables 幂等模式），watchlist/results/export 共 5 端点查询前建表，不再依赖 batch.run 隐式初始化。
- **#SA-20260831-10（P1）symbols=[] 短路**：`routes.py` 移除 `or None` 短路，显式 `symbols=[]` 走 400 非空校验，不再静默退化为 watchlist 驱动。
- **#SA-20260831-11（P1）导出 Decimal 500**：`routes.py batch_export` 改用 Flask JSON provider（`current_app.json.dumps`，Decimal→float），修复 NUMERIC 列导出 TypeError→500。
- **#SA-20260831-09（P2）交叉校验阈值过严**：`gateway._crosscheck` 历史 K 线分支容差 0.1%→1%（实测健康标的跨源口径差最大 0.6%），仍可拦截 430047 级真陈旧冲突。
- **#SA-20260831-02（P2）版本号**：plugin.json `1.5.0` → `1.5.1`。
- **#SA-20260831-01（P0）部署滞后**：data_date / `_check_freshness` / F4 isascii 三项修复在本版本已随仓库部署到位（v1.5.1-pre 遗留，本次随版本号正式发布）。

## v1.5.1-pre — 2026-08-31

### Fixes（第三方回归报告驱动 · #SA-20260830-01 / #SA-20260830-02）

- **#SA-20260830-01（P1）冷却污染**：`ProviderError` 新增 `retryable` 语义（`providers/base.py`）。标的不存在（empty payload）、可选依赖缺失（akshare not installed）等用户侧/环境性错误标记 `retryable=False`，`gateway._dispatch` 仅对真实源故障（`retryable=True`）累加连败并触发冷却摘除；修复「连续查询 3 个不存在标的后 kline 全链封锁 30 分钟」。
- **#SA-20260830-01 全链兜底文案**：全部数据源冷却/不可用时由 `no provider supports kline` 改为「数据源暂时不可用（xxx），请稍后重试」。
- **#SA-20260830-02（P0）北交所陈旧 K 线**：`gateway._check_freshness` 新增 K 线新鲜度校验（`MAX_STALE_DAYS=10` 自然日，覆盖长假不误伤周末）。陈旧数据不写缓存、不计成功、不计冷却，触发 failover；网络取数与落盘缓存命中两条路径均生效（修复 430047/830799/871981 等北交所早期代码段输出 2025 年陈旧行情的问题）。
- **#SA-20260830-02 交叉校验修正**：`gateway._crosscheck` 比较基准按 K 线末行日期选择（当日 K 线对齐 quote 现价 >5% 才标冲突；历史 K 线末行对齐昨收 >0.1% 标冲突），消除交易时段现价波动造成的健康标的误报；极端冲突（>20%）由静默留痕改为抛 `retryable=False` 阻断输出；quote 侧故障一律旁路不计入 K 线源失败。
- **数据日期标注（合规兜底）**：`stock_skill._technical_analysis` 输出 `data_date`（K 线截止日）至 `json_data` 与报告「数据截至」行，供前端/用户核对数据新鲜度。
- **F4 输入校验**：`routes.py` 符号校验增加 `isascii()`，封堵 `isalnum()` 接受中文/全角字符（如"茅台"）绕过 400 校验的缺口。
- **F3 并发安全**：`gateway._usage` 改为 `threading.local()`（线程局部），消除 batch 多线程（8 worker）并发下 `data_sources` 归属串数据风险（合规输出完整性）。
- **落盘缓存反序列化修复（v1.5.0 遗留）**：`to_json(orient='split')` 将 DatetimeIndex 序列化为毫秒时间戳整数，`_deserialize` 原以默认纳秒解析导致索引畸变为 1970-01-01；改为 `unit="ms"`。该缺陷系 #SA-20260830-02 新鲜度校验上线后暴露（此前落盘缓存命中无任何日期相关逻辑）。
- **F6 测试规范恢复**：重建 `tests/REGRESSION_TESTING.md`（57 用例：AUTH 8 / ANALYZE 8 / QUOTE 4 / GATEWAY 6 / CACHE 4 / EDGE 8 / COMPLIANCE 4 / WL 5 / BATCH 10；含 §6.2 基线纪律、§7.2 执行纪律、§8.2 准出门槛、§8.3 一票否决）。原版从未进入 git 历史（2026-08-31 核查），重建依据回归报告引用结构，个别细节标注「重建推断」。

## v1.5.0-pre — 2026-08-29

### Fixes（审计驱动 · 2026-08-29）

- P1-1 交易日历：`data/calendar/2026.csv` 剔除法定节假日休市日（元旦/春节/清明/五一/端午/中秋/国庆，共 19 个工作日，按沪深北交易所 2026 休市公告）
- P1-2 限流热路径：`plugins/_base/ratelimit.py` `check_rate_limit` 改走共享连接池（消除每次调用裸物理建连）+ DDL 进程内一次性执行（消除重复 DDL 锁竞争）
- P2-1 降级语义：moneyflow 不可用由 500 改为 404，文案对齐 fundamental-detail
- P2-2 探针口径：能力探针 fundamental 由 `daily_basic` 改为 `fina_indicator`（与 fetch_fundamental 同门槛，避免低积分 token 探针误报可用）
- P2-3 NameError：sina fetch_kline 双请求均抛异常时不再引用未赋值 `response`（固定文案），消除逃出 failover 的 NameError
- P2-4 LLM 成本护栏：批量 kind 白名单剔除 `llm`（无白名单池 + 无并发信号量时批量 llm 放大 LLM 账单）；模板批量下拉同步移除 llm 选项
- P3-1 manifest 版本：plugin.json `1.3.0` → `1.5.0`
- P3-3 缓存目录：gateway 支持 config.yaml 的 `DATA_CACHE_DIR`（env `STOCK_DATA_CACHE_DIR` 优先）
- P3-5 死代码：删除 stock_skill.py M-2 进程内缓存（`_CACHE_STORE`/`_cache_get`/`_cache_set`）及未使用 `import threading`
- P3-6 锁泄漏：batch.py advisory lock 执行区间包 try/finally，异常路径确保 unlock + close
- P3-7 导出入口：管理端批量面板新增「Export JSON」按钮（调 `/api/batch/export`）

### Changes

- 补齐方案缺口（对照实施方案 2026-08-29）：
- 回归工具落位：新增 `tools/capture_baseline.py`（基线录制）+ `tools/compare_baseline.py`（双模式：实时容差 ±0.5% / mock 回放严格，自动过滤 timestamp/fetched_at），补 §3.2/§4.7 工具缺口；`compare_baseline` 自对比 OK
- P0-a 12 条审核清单逐条核对留档：sina K线 URL/重试、tencent 下标 [1][3][4][32][38][39][47]、新闻 GBK/正则/去重/5≤len≤120、情绪正负词表，均与 `git v1.3.0-baseline` 原码逐字一致；缓存 TTL 300/60/600 + 源前缀 key、market_symbol/index_symbol 规则、market_overview 三指数结构、grep 零直连（routes/skill 全走 gateway）核对通过；to_json 基线一致由 compare_baseline 工具支撑（本地无 token 时 fundamental 类复录留待服务器侧补跑）
- 财报四表补齐：`tushare_provider.fetch_fundamental` 改为四表合一（income/balance/cashflow/fina_indicator 各 8 期，pandas to_json→json 往返转纯 Python），`/api/fundamental-detail` 返回四表 JSON；估值分位保留在 analyze 输出（§5.4/§5.7 对齐）
- 批量导出补齐：新增 `/api/batch/export`（JSON 文件下载，run_id/symbol/signal 筛选，全量不分页），满足 §7.6「可回查可导出」
- 依赖锁文件补齐：`requirements.lock` 以 constraint 模式增量加入 tushare==1.4.29 依赖链（simplejson/bs4/beautifulsoup4/soupsieve/websocket-client），版本零漂移（§5.1）
- feat(stock_analysis): tushare authorized primary source, capability probe, valuation percentile (P0-b)
- 新增 `tushare_client.py`：token 只从 `provider_api_keys`（provider='tushare'）读取并解密，不进 config/日志；积分探针按实测可用集合裁剪类别，无 token 返回空集自动降级
- 新增 `providers/tushare_provider.py`（authorized=True）：ts_code 规范化（含 sh/sz/bj 前缀保留与北交所 8/4 段）、`supports()` 动态叠加探针结果；`gateway.py` KLINE 链插队 tushare→akshare→sina，新增 FUNDAMENTAL/MONEYFLOW 类别路由与 `get_fundamental`/`get_moneyflow` 入口
- `gateway._dispatch` 按 `provider.supports()` 裁剪无权限源；缓存 key 含全部 kwargs（支持 moneyflow days 区分）
- 新增 `valuation.py`：Tushare 可用时输出近 5 年 PE(TTM)/PB 分位；`stock_skill._fundamental_analysis` 叠加 `json_data.valuation` 与报告行「PE(TTM) 处于近 5 年 x% 分位（低估/合理/高估区）」，token 缺失/无权限时零回归
- 路由：`/api/fundamental-detail`（估值分位）、`/api/moneyflow`（个股资金流最近 N 日）
- 依赖声明：`plugin.json` required 加 `tushare>=1.4.0`；settings_schema 枚举扩 `tushare`；kline 链注释更新为 tushare→akshare→sina
- token 模式变更（产品定案·用户自备）：`tushare_client` 不再读全局 `provider_api_keys` 表，改为读取环境变量 `TUSHARE_TOKEN`（优先）或 `config.yaml` 的 `TUSHARE_TOKEN`；`settings_schema` 新增 `tushare_token`（password）供桌面端设置页录入；空值自动降级免费源，token 日志仍脱敏
- feat(stock_analysis): akshare fallback, failover chain, crosscheck, rate limit, disk cache (P0-c)
- 新增 `providers/akshare_provider.py` 备源（延迟导入，未安装时自动降级）
- `gateway.py` 升级：类别路由 + failover 链 + 连续失败冷却摘除 + kline/quote 交叉校验 + 进程内信号量 + PG 全局双层限频 + 当日落盘缓存
- 修复落盘缓存读取（手动解析 split 布局，绕开 pandas read_json 字符串路径歧义）与缓存命中合规来源记录
- 修复 crosscheck 引用语义：最近交易时段收盘优先对齐 quote 现价，price=0 时兜底昨收（消除周末假冲突）
- 修复指数符号前缀：KLINE 取数对已带 sh/sz/bj 前缀的符号保留原样，避免 sh000001 被错转 sz000001（上证指数 kline 曾误取平安银行行情）
- 合规：`to_json()` 恒含 `disclaimer` + `data.data_sources`，每数据点含 source/authorized/fetched_at
- `DATA_PROVIDER` 语义修正为「首选源覆盖」（`apply_preference()`），空值按 ROUTE 顺序
- `plugin.json` / `requirements.txt` 声明 akshare 可选依赖；模板 + i18n 展示数据源与免责声明
- 新基线 `tests/fixtures/baseline_v150.json`（13 用例），与复录对比 OK
- feat(stock_analysis): trading-day batch concurrency, sa_* tables, daily job, watchlist admin (P1)
- 新增 `models_sa.py`：独立 schema `stock_analysis` 四表（sa_watchlist / sa_analysis_run / sa_analysis_result / sa_signal_log），`get_db()` 经 `get_pooled_connection()` 借池，`ensure_tables()` 建表即 SELECT 验证，INSERT 均 ON CONFLICT DO NOTHING 幂等
- 新增 `market_calendar.py` + `data/calendar/2026.csv`：交易日离线 CSV 优先 + 周末规则兜底（法定节假日休市日按交易所公告逐年补齐）
- 新增 `batch.py`：三层幂等（日历校验 / 会话级 advisory lock / run_id 已 done 跳过）+ ThreadPoolExecutor 8 线程分批 50；DB 不可用时降级为内存分析
- `register_jobs()`：交易日 15:05 cron 批量分析（APScheduler 标准参数，max_instances/coalesce 由调度器固定）
- 路由：watchlist GET/POST/DELETE、batch/run POST、batch/results GET；模板三视图 + i18n

## v1.4.0-pre — 2026-08-29

### Changes

- refactor(stock_analysis): provider abstraction, zero behavior change (P0-a)
- 新增 `providers/` 包（base / commons / sina / tencent）与 `gateway.py` 数据网关
- `stock_skill.py` 5 处行情/新闻调用点全部改走 gateway（类别路由 + 源前缀缓存）
- 删除 `_get_price_data` / `_get_latest_price`；`_market_symbol` / `_index_symbol` 标记 deprecated
- 回归基线锚点：`git tag v1.3.0-baseline` + `tests/fixtures/baseline_v130.json` + `tools/` 录制/对比脚本
- 13 用例回归对比 OK（逐字段 / 数值容差 ±0.5%），无效 symbol 稳定 error

## v1.3.0 — 2026-08-28

### Changes

- Version bump from v1.2.0
- fix(stock_analysis): harden NaN handling, symbol normalization and field parsing
- fix(plugins): attach edition roles to dedicated sub-agents
