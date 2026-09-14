# VeroRun 股票分析插件（stock_analysis）

> **当前版本**：v1.7.1（以 `plugin.json` 为准，变更详见 `CHANGELOG.md`）
> **文档定位**：本文件面向**开发/运维/审计**，描述架构、模块职责、数据流、存储模型与部署约束。
> 面向使用者的能力清单、端点用法与桌面端契约，见 [`USAGE.md`](./USAGE.md)；平台技能声明见 [`SKILL.md`](./SKILL.md)。

面向 VeroRun 的 A 股研究插件，为**管理员**与**金融分析 Agent** 提供技术面、估值、情绪面及 AI 综合研判能力。插件通过 VeroRun 标准生命周期加载，不绑定内核具体模型，不保存交易订单或用户资产数据。

> **重要声明**：本插件输出的是**研究信息与风险提示，不是自动交易指令**。所有分析结果受数据延迟、停牌、缺失指标与模型误判影响，仅供研究参考，**不构成投资建议或收益承诺**。

> 当前 VeroRun 没有社区服务，因此本插件**不包含**社区客户端、社区 API、社区密钥、发帖、投票或经验包上传功能。

---

## 一、能力矩阵

| 能力域 | 说明 | 主要实现 |
| --- | --- | --- |
| 技术面 | MA5/20/60、MACD、KDJ、RSI（Wilder）、BOLL、支撑/阻力与技术信号 | `indicators.py`、`kline_service.py` |
| 估值 | PE(TTM)/PB 实时字段评分；tushare 授权时叠加近 5 年分位 | `valuation.py` |
| 情绪面 | 新浪财经新闻标题关键词统计，附近期新闻示例便于核验 | `stock_skill.py` |
| AI 综合研判 | 经 `UnifiedLLM` 网关综合技术面 + 证据链，输出结构化信号与中文研判 | `stock_skill.py`、`evidence.py` |
| 证据链 | 财报四表/资金流/新闻/估值分位打包入 prompt，逐源独立降级并留痕 | `evidence.py` |
| 市场概况 | 上证/深成/创业板指实时点位与涨跌幅 | `gateway.py`（INDEX → tencent） |
| 自选池与批量 | watchlist 管理、定时批量分析、结果检索与导出 | `batch.py`、`routes.py` |
| 异步任务 | 分析任务队列（跨 worker 安全、崩溃兜底） | `jobs_queue.py` |
| 实时事件 | SSE 事件流（alerts/jobs 主题，心跳 + 断线续传） | `sse_stream.py` |
| 告警 | 6 类规则周期扫描（60s）+ 事件落库 + 钩子派发 | `alert_engine.py` |
| 信号兑现 | 信号落库 + 增量回算实际收益，输出质量统计 | `signal_quality.py` |
| 结论沉淀 | 成功分析幂等写入平台知识库 | `kb_publish.py` |
| 深度研判 | `stock_deep_research` DAG 工作流节点 | `deep_research.py` |
| MCP 工具 | 5 个快路径工具（JSON-RPC 2.0 over stdio） | `tools/mcp_server.py` |

---

## 二、目录结构

```text
plugins/stock_analysis/
├── __init__.py                  # 插件入口：路由/Agent/调度任务/DAG 节点注册
├── plugin.json                  # manifest（版本/菜单/权限/Agent/设置 Schema/MCP/钩子）
├── routes.py                    # 管理页面 + 25 个 admin 鉴权 API（含限流）
├── stock_skill.py               # 分析引擎（技术/估值/情绪/LLM 编排）+ CLI
├── gateway.py                   # 数据网关：provider 路由、失败摘除、两级缓存、并发与配额
├── providers/                   # 数据源适配器（统一 BaseProvider 接口 + supports() 探针）
│   ├── base.py                  #   DataCategory 枚举 / Meta / BaseProvider 抽象
│   ├── tushare_provider.py      #   授权主源（K线/财报/资金流）
│   ├── akshare_provider.py      #   备源（K线）
│   ├── sina.py                  #   兜底（K线）+ 新闻
│   ├── tencent.py               #   实时行情 + 指数
│   └── commons.py               #   符号归一化（sh/sz/bj 前缀）
├── indicators.py                # 权威指标实现（INDICATOR_VERSION = iv-4）
├── kline_service.py             # /api/kline 契约组装（bars + 指标 + 复权基准）
├── valuation.py                 # PE(TTM)/PB 近 5 年历史分位（需 tushare）
├── market_calendar.py           # A 股交易日历（CSV 优先 + 周末兜底）
├── jobs_queue.py                # 分析任务队列（advisory lock + 崩溃回收）
├── sse_stream.py                # SSE 事件流（轮询出流 + 心跳 + 断点续传）
├── alert_engine.py              # 告警规则求值、扫描、事件派发
├── signal_quality.py            # 信号兑现回算与质量统计
├── models_sa.py                 # 独立 schema stock_analysis 数据层（9 张表）
├── evidence.py                  # 证据链打包 + LLM 结构化输出解析
├── deep_research.py             # stock_deep_research DAG 节点
├── kb_publish.py                # 分析结论沉淀平台知识库
├── batch.py                     # 定时批量分析
├── config.yaml                  # 插件本地配置（文件通道）
├── requirements.txt             # 依赖声明
├── setup.sh                     # 独立 venv 安装脚本（仅供独立运行）
├── tools/
│   ├── mcp_server.py            # MCP stdio 工具服务器（5 个工具）
│   ├── gen_calendar.py          # 交易日历 CSV 生成脚本（需 tushare token）
│   ├── capture_baseline.py      # 录制回归基线快照
│   └── compare_baseline.py      # 基线逐字段 diff（支持 --strict）
├── data/calendar/               # 年度交易日历 CSV（2024/2025/2026）
├── agents/
│   └── stock_analysis_agent_prompt.md   # Agent 系统提示词（含合规护栏）
├── templates/
│   └── stock_analysis.html      # iframe 管理页面（i18n + 主题同步）
├── i18n/
│   ├── en.yml                   # 英文词条（key = 英文源串）
│   └── zh-CN.yml                # 中文词条（与 en 键集一致）
├── tests/                       # 单元测试 + 回归测试规范
│   ├── test_indicators.py
│   ├── test_alert_engine.py
│   ├── test_alerts_api.py
│   ├── test_sse_stream.py
│   └── REGRESSION_TESTING.md
├── SKILL.md                     # 平台技能声明（v1.7.1）
├── USAGE.md                     # 用法说明（商店同步自动入 KB）
└── README.md                    # 本文件
```

### 安装

将整个目录复制到 VeroRun 插件目录，目录名必须为 `stock_analysis`：

```text
verorun/plugins/stock_analysis/
```

插件根目录必须包含 `plugin.json`、`__init__.py`、`routes.py` 与 `agents/`。安装后由 PluginManager 读取 manifest，并在启用时注册管理路由、Agent、调度任务与 DAG 节点。

### 运行环境

- Python 3.11+
- VeroRun ≥ 0.59.3（manifest `min_app_version` 校验）
- 依赖：`pyyaml>=6.0`、`numpy>=2.0`、`pandas>=2.0`、`requests>=2.31`、`tushare>=1.4.0`
  - 可选备源：`akshare>=1.14.0`（延迟导入，缺装不影响主链路）
  - pandas 已加入主项目依赖，部署时随系统依赖安装，无需单独维护插件 venv
- 外呼公网数据源的网络访问权限（Tushare / AkShare / Sina / Tencent）

---

## 三、架构与数据流

```text
                       ┌──────────────── 管理端 / 桌面端 / MCP ────────────────┐
                       │  routes.py（25 API，admin 鉴权 + 逐端点限流）          │
                       └──────────────────────────┬───────────────────────────┘
                                                  │
     ┌────────────────────┬───────────────────────┼──────────────────┬─────────────────┐
     │                    │                       │                  │                 │
┌────▼─────┐      ┌───────▼───────┐      ┌────────▼────────┐  ┌──────▼──────┐  ┌──────▼───────┐
│stock_skill│      │ kline_service │      │  jobs_queue     │  │alert_engine │  │ sse_stream   │
│ 分析引擎  │      │  K线+指标契约  │      │  异步任务队列    │  │  告警扫描    │  │  SSE 事件流   │
└────┬─────┘      └───────┬───────┘      └────────┬────────┘  └──────┬──────┘  └──────┬───────┘
     │                    │                       │                  │                 │
     │            ┌───────▼───────────────────────▼──────────────────▼─────────────────▼───┐
     │            │                          gateway.py 数据网关                            │
     │            │  provider 路由（supports 探针裁剪）· 失败摘除 · 两级缓存 · 并发/配额      │
     │            └───────┬────────────────────────────────────────────────────────────────┘
     │                    │
     │            ┌───────▼──────────────────────────────────────────┐
     │            │  providers: tushare → akshare → sina / tencent   │
     │            └──────────────────────────────────────────────────┘
     │
     ├── evidence.py      证据链打包（财报四表/资金流/新闻/估值分位，逐源降级留痕）
     ├── UnifiedLLM       内核统一 LLM 网关（模型/密钥/配额/审计由内核负责）
     ├── evidence.parse_structured_output()  结构化解析（非法值收敛、reasons 过滤）
     └── kb_publish.py    结论幂等沉淀知识库

持久化（独立 schema `stock_analysis`，经公共连接池借连接）：
  models_sa.py ── sa_watchlist / sa_analysis_run / sa_analysis_result /
                  sa_signal_log / sa_signal_realized / sa_jobs /
                  sa_alerts / sa_alert_events / sa_sse_events
```

### AI 研判链路

```
技术快照 (MA/MACD/KDJ/RSI/BOLL)
      + 证据链 (evidence.build_evidence_context：四表/资金流/新闻/估值分位，逐源降级留痕)
      + 复权口径标注
        ↓  _build_llm_prompt
  UnifiedLLM (standard tier；Agent 系统提示词显式前置为 system 消息)
        ↓
  evidence.parse_structured_output()   命中即用 → 未命中回退 _extract_signal
        ↓  收敛：signal 白名单 / confidence clamp 到 [0,1] / reasons 过滤空串(≤5) / evidence_refs 过滤空串(≤8)
  AnalysisResult（signal / confidence / reasons / report / disclaimer / evidence_refs）
        ↓
  record_signal()（同锚点幂等落库）+ kb_publish（幂等沉淀）
```

---

## 四、数据源与路由

### 路由表（`gateway.ROUTE`）

| 数据类别 | Provider 顺序 | 说明 |
| --- | --- | --- |
| `KLINE`（历史日线） | tushare → akshare → sina | failover 链，按 `supports()` 探针裁剪 |
| `FUNDAMENTAL`（财报四表） | tushare | 无 token 自动降级，证据链留痕「未获得(原因)」 |
| `MONEYFLOW`（资金流） | tushare | 同上 |
| `NEWS`（新闻标题） | sina | 情绪关键词统计 |
| `QUOTE`（实时行情） | tencent | 现价、涨跌幅、PE(TTM)、PB、换手率 |
| `INDEX`（指数） | tencent | 上证/深成/创业板 |

- `DATA_PROVIDER` 语义为**首选源覆盖**：指定值被排到其所在链头部；空值完全按上表顺序。
- **失败摘除**：连续失败的 provider 摘除 `COOLDOWN = 1800s`（30 分钟）。
- **新鲜度闸门**：K 线数据陈旧超过 `MAX_STALE_DAYS = 10` 自然日一律拒绝输出，转 failover 下一源（10 天可覆盖春节/国庆长假且不误伤正常周末）。

### 两级缓存

| 层 | 实现 | 说明 |
| --- | --- | --- |
| 进程内 TTL | 有界字典 `MAX_MEM_CACHE = 1024` | 超限先逐过期项，仍超限则逐出最接近过期者 |
| 当日落盘 | gzip（DataFrame 经 `to_json(orient='split')`） | 当日有效，目录见下 |

按类别 TTL（秒）：

| 类别 | KLINE | QUOTE | INDEX | NEWS | FUNDAMENTAL | MONEYFLOW |
| --- | --- | --- | --- | --- | --- | --- |
| TTL | 300 | 60 | 60 | 600 | 300 | 300 |

- **空数据不缓存**，保留重试能力
- 所有 HTTP 请求均设置超时，仅访问硬编码公网域名

### 并发与配额

为避免打爆数据源与 admin 进程内存，`gateway` 内置双层节流：

| 机制 | 取值 |
| --- | --- |
| 单源进程内并发信号量 | tushare 4 / akshare 2 / sina 4 / tencent 8 |
| 跨 worker 60s 窗口总量 | tushare 300 / akshare 120 / sina 180 / tencent 600 |

---

## 五、存储模型

插件在**独立 schema `stock_analysis`** 下自有 9 张表，经公共连接池 `plugins/_base/db` 借连接（无自建连接池）：

| 表 | 用途 |
| --- | --- |
| `sa_watchlist` | 自选池标的 |
| `sa_analysis_run` | 批量运行记录（总数/成功/失败/状态） |
| `sa_analysis_result` | 批量分析结果 |
| `sa_signal_log` | 信号落库（同锚点幂等，空信号不落行） |
| `sa_signal_realized` | 信号兑现回算结果 |
| `sa_jobs` | 异步任务队列（含状态/进度/结果） |
| `sa_alerts` | 告警规则 |
| `sa_alert_events` | 告警触发事件 |
| `sa_sse_events` | SSE 事件流水（供断线续传按 `id` 回放） |

---

## 六、异步任务队列

`jobs_queue.py` 负责分析任务的跨 worker 安全执行：

- **状态机**：`queued → running → done | failed`
- **跨 worker 安全**：每任务先 `pg_try_advisory_lock` 独占，再原子认领 `queued→running`
- **崩溃兜底**：轮询循环定期把卡在 `running` 超过 **30 分钟**的任务回置 `queued`（会话锁随进程消亡释放）
- **并发封顶**：`_MAX_CONCURRENT = 2`（finance 版 admin `-w 2` 内存约束）；每轮扫描候选上限 `_MAX_DISPATCH = 5`，槽位耗尽则任务留在 `queued` 下轮再取
- **当日复用**：同 `symbol` + `scope` 当日已有 `done` 任务时直接复用（`reuse=true`），`force=true` 可强制重算
- **失败回执**：`error_code` + `error.message`，前端可直接展示并重试

---

## 七、实时事件与告警

### SSE 事件流（`GET /api/events`）

| 参数 | 取值 |
| --- | --- |
| 可用主题 | `alerts`、`jobs`（`quotes` 主题暂缓，待 watchlist 语义确定） |
| 默认主题 | `alerts,jobs` |
| 出流轮询间隔 | 1.0s |
| 心跳间隔 | 15.0s（`: heartbeat` 注释行） |
| Token 重验间隔 | 30.0s |
| 每轮拉取上限 | 200（同时是断线补发上限） |

支持 `Last-Event-ID` 断点续传：断线后按 `id` 回放未消费事件，不丢已发事件。

### 告警引擎

- **6 类规则**（与桌面端 `ALERT_TYPE_META` 对齐）：`price_above`、`price_below`、`change_pct`、`rsi_oversold`、`rsi_overbought`、`signal_change`
  - 除 `signal_change` 外均需 `threshold`
  - 支持免打扰时段（`silent_from` / `silent_to`）
- **扫描节奏**：60s 一轮（`stock_analysis_alert_scan`），`scheduled_scan()` 内自持 advisory lock（`sa_alert_scan`），多 worker 各自调度时仅一方执行
- **钩子派发**：每次告警事件写入后派发 `stock.alert.triggered`，供通知/审计/第三方推送消费；**钩子故障不影响扫描主链路**
- **频道值**：保留桌面端中文值（`站内信` / `邮件` / `IM`）

---

## 八、调度任务

| 任务 ID | 触发 | 职责 |
| --- | --- | --- |
| `stock_analysis_daily_batch` | cron，周一至周五 15:05 | 收盘后批量分析 watchlist 标的 |
| `stock_analysis_signal_realize` | cron，周一至周五 16:00 | 增量回算信号兑现（失败不影响批量主链路） |
| `stock_analysis_alert_scan` | interval，60s | 告警规则周期扫描 |

---

## 九、配置

配置存在**两个通道**，读取时以 PluginManager 持久化配置为准、文件配置兜底：

| 配置项 | 类型 | 说明 |
| --- | --- | --- |
| `tushare_token` | string（password） | 用户自备 Tushare Pro token；空值自动走免费源，功能不受影响 |
| `data_provider` | enum | `""` / `sina` / `tencent` / `akshare` / `tushare`；空值按默认路由顺序 |
| `data_cache_dir` | string | 落盘缓存目录，默认 `./data/cache` |
| `auto_deep_research_on_batch` | boolean | 批量分析完成（`stock_analysis_daily_batch`）后自动对当日高置信标的（置信度 ≥0.7 取前 3）入队 LLM 深研；默认 `false`，逐项灰度开启 |

**环境变量覆盖**：

| 变量 | 覆盖对象 | 优先级 |
| --- | --- | --- |
| `TUSHARE_TOKEN` | tushare token | 最高 |
| `STOCK_DATA_CACHE_DIR` | 落盘缓存目录 | 高于 `config.yaml` 的 `DATA_CACHE_DIR` |

**模型策略**：模型选择、API Key、配额、缓存与用量审计全部由 VeroRun `UnifiedLLM` 内核负责。插件 manifest 使用 `standard` tier，不直接指定 provider/model/密钥；通过 `get_agent_by_slug("stock_analysis_agent")` + `resolve_model_args` 获取模型解析结果，并将 Agent 系统提示词显式前置为 system 消息，确保合规护栏实际生效。

---

## 十、管理界面与 API

### 页面入口

```text
/admin/stock-analysis/
```

所有页面与 API 均要求 VeroRun 管理员身份（`sso_token` Cookie / `Authorization: Bearer` / `X-Token`，校验 `is_admin` 声明），未授权返回 401。

### 端点与限流

前缀 `/admin/stock-analysis`。限列为「次数 / 窗口（秒）」：

| 方法 | 路径 | 用途 | 限流 |
| --- | --- | --- | --- |
| GET | `/` | 管理页面（iframe） | — |
| GET | `/api/analyze` | 个股分析（`type` ∈ technical/fundamental/sentiment/llm；`months` 1–36，默认 6） | 30 / 60 |
| GET | `/api/signal` | 快速技术信号 | 60 / 60 |
| GET | `/api/market` | 市场概况 | — |
| GET | `/api/watchlist` | 自选池查询 | — |
| POST | `/api/watchlist` | 自选池新增 | 20 / 60 |
| DELETE | `/api/watchlist` | 自选池删除 | — |
| POST | `/api/batch/run` | 批量分析 watchlist | 5 / 60 |
| GET | `/api/batch/results` | 批量结果检索 | — |
| GET | `/api/batch/export` | 批量结果导出 | 10 / 60 |
| GET | `/api/fundamental-detail` | 财报四表明细（需 tushare） | 30 / 60 |
| GET | `/api/moneyflow` | 资金流（需 tushare） | 30 / 60 |
| GET | `/api/signal-quality` | 信号质量统计 | — |
| POST | `/api/signal-realize` | 手动触发信号兑现回算 | 3 / 60 |
| GET | `/api/deps/status` | 可选依赖（akshare）状态 | — |
| POST | `/api/deps/install` | 可选依赖安装（需管理员确认） | 3 / 600 |
| GET | `/api/kline` | 日 K + 服务端权威指标（`period`/`adjust`/`limit`/`offset`/`until`） | 60 / 60 |
| GET | `/api/quotes` | 批量实时行情（`symbols` ≤ 50，逗号分隔） | 120 / 60 |
| POST | `/api/jobs` | 创建分析任务（`symbol`/`scope`/`force`） | 10 / 60 |
| GET | `/api/jobs/<job_id>` | 任务状态与结果 | 60 / 60 |
| POST | `/api/discuss` | 多空对辩研判（`symbol`；异步入队 scope/type=discuss，结果走 `/api/jobs/<id>`，同日幂等复用） | 10 / 60 |
| GET | `/api/alerts` | 告警规则列表 | — |
| POST | `/api/alerts` | 创建告警规则 | 20 / 60 |
| DELETE | `/api/alerts` | 删除告警规则 | 20 / 60 |
| GET | `/api/alerts/events` | 告警事件列表 | — |
| GET | `/api/events` | SSE 事件流（`topics`，支持 `Last-Event-ID`） | — |

> 参数细节与调用示例见 [`USAGE.md`](./USAGE.md)。

### 响应信封（集成方必读）

插件存在**双信封**，对接时须区分：

| 端点族 | 信封 | 示例 |
| --- | --- | --- |
| `/api/analyze`（历史契约） | `{ "success": bool, "result": { symbol, timestamp, signal, report, json_data } }` | 旧契约，保持兼容 |
| 桌面端契约端点（`/api/kline`、`/api/quotes`、`/api/jobs`、`/api/alerts*`、`/api/events` 等） | `{ "ok": bool, "data": …, "error": …, "meta": … }` | `_contract_error` / `_contract_ok` |

`symbol` 校验：仅允许字母/数字/点，长度 ≤ 12，自动归一化为 `sh`/`sz`/`bj` 前缀（含北交所 `43/83/87/88/920` 段识别）。

---

## 十一、MCP 工具服务器

manifest 声明 stdio 传输的 MCP 服务器（`name = stock`），供桌面端/聊天页直接调用：

```json
{ "command": "python", "args": ["plugins/stock_analysis/tools/mcp_server.py"], "transport": "stdio" }
```

暴露 5 个快路径工具：`get_quote`、`get_kline`、`get_technical_signal`、`get_fundamental_digest`、`market_overview`。

- 行分隔 JSON-RPC 2.0；**stdout 仅协议帧，日志走 stderr**
- 工具内部异常一律返回 `isError`，不崩溃进程

---

## 十二、合规与风险披露

插件在各分析路径内置风险披露，明确不构成投资建议：

- **技术面**：报告末尾附「风险提示: 技术指标不构成投资建议」
- **估值分析**：报告注明「数据范围: 实时行情估值字段，未包含完整财报」；`json_data` 含 `scope="valuation_only"` 与 `disclaimer`
- **情绪面**：报告注明「方法: 基于新浪财经新闻标题关键词统计，可能存在误判」，附最多 3 条近期新闻示例（`json_data.samples` 最多 5 条）
- **LLM 综合**：Agent 系统提示词强制前置，包含「不虚构价格/指标/新闻/持仓、中性措辞、注明数据来源与时间戳、失败即报告」等金融护栏
- **置信度语义**：`confidence` 为启发式信号强度值（已 clamp 至 `[0,1]`），**非统计置信度**，请勿据此重仓决策
- **证据链留痕**：每个子源失败时写入「未获得(原因)」，避免模型在无数据支撑时臆测

---

## 十三、安全边界

- **全路由 admin 鉴权**：页面与全部 API 均校验 `is_admin`，未授权返回 401
- **逐端点限流**：写操作与重开销端点均有次数/窗口约束（见 §十），`deps/install` 收紧至 3 次 / 600s
- **符号白名单**：`symbol` 仅接受字母/数字/点、≤12 字符，杜绝注入与任意输入
- **无 SSRF**：所有外呼 URL 硬编码为公网数据源，用户输入仅作为查询参数
- **无危险执行**：仅 `routes.deps_install` 一处 `subprocess.run`，用于白名单可选依赖（akshare）的 `pip` 安装——固定命令、无 shell 拼接、超时保护、需管理员确认；除此之外无 `os.system` / `eval` / `pickle`，无硬编码凭据
- **数据隔离**：独立 schema `stock_analysis`，经公共连接池借连接，无自建连接池
- **i18n**：UI 词条全部经 `_()` 查表，`i18n/{en,zh-CN}.yml` 键集一致，缺失键回退英文源串

---

## 十四、Python API / CLI

### Python API

```python
from stock_skill import StockAnalysisSkill

skill = StockAnalysisSkill()

# 全维度分析（type: technical / fundamental / sentiment / llm）
result = skill.analyze("600519", analysis_type="llm", months=6)
print(result.to_text())     # 人类可读报告
print(result.to_json())     # 结构化 dict

# 快速技术信号
signal = skill.get_signal("600519")

# 市场概况
overview = skill.market_overview()
```

### CLI（独立运行）

```bash
python stock_skill.py 600519                      # 默认全维度分析
python stock_skill.py 600519 --type technical     # 指定分析类型
python stock_skill.py 600519 --months 12          # 历史数据月数
python stock_skill.py 600519 --format json        # text / json / signal
python stock_skill.py --market                    # 市场概况
python stock_skill.py --help
```

> v1.7.0 已下架硬编码返回 501 的 `--sectors` 假能力（S6），行业数据待真实数据源接入后重建。

---

## 十五、测试与回归

```bash
# 单元测试
pytest plugins/stock_analysis/tests/

# 指标基线回归（升级 indicators.py 后必做）
# 1) 录制基线（建议以版本号命名，便于归档）
python tools/capture_baseline.py --out tests/fixtures/baseline_v171.json
# 2) 代码/指标变更后，录制当前输出
python tools/capture_baseline.py --out /tmp/current_v172.json
# 3) 逐字段比对（--strict 数值容差 0）
python tools/compare_baseline.py tests/fixtures/baseline_v171.json /tmp/current_v172.json --strict
```

- 现有单测覆盖：指标、告警引擎、告警 API、SSE 事件流
- 回归测试规范（用例编号、优先级、验收门槛）见 [`tests/REGRESSION_TESTING.md`](./tests/REGRESSION_TESTING.md)
- **口径变更提示**：v1.7.0 将信号路径 RSI 由 Cutler 平滑改为 `indicators.rsi()` Wilder 实现（`INDICATOR_VERSION = iv-4`），**技术指标基线需随版重录归档**

---

## 十六、部署注意事项

1. **依赖**：pandas / numpy 需随主项目依赖安装；缺失时插件路由静默不挂载
2. **激活声明变更**：`mcp_servers` / `hooks` 等 manifest 声明仅在 **disable → enable** 时同步，仅修改文件不会生效
3. **i18n 播种**：新增/修改 `i18n/*.yml` 词条后，admin 服务重启时由 PluginManager 自动幂等播种
4. **交易日历**：`data/calendar/` 内置 2024/2025/2026 年度数据；缺失年度自动走周末兜底（交易所未公布休市安排前不伪造，公布后用 `tools/gen_calendar.py` 补齐）
5. **Tushare token**：未配置时财报/资金流/估值分位自动降级，技术面与行情功能不受影响
6. **已知不一致**：`requirements.txt` 已显式声明 `numpy>=2.0`，但 `plugin.json` 的 `python_dependencies.required` 未包含 numpy。当前依赖 pandas 传递引入可用，建议后续在 manifest 补齐声明
7. **验证建议**：使用真实管理员账号登录后，实测 `/admin/stock-analysis/` 页面与关键 API（analyze / kline / quotes / jobs / alerts / events）

---

## 十七、相关文档

| 文件 | 定位 |
| --- | --- |
| [`USAGE.md`](./USAGE.md) | 使用者向：能力清单、端点表、token 配置、桌面端对接契约（商店同步自动入 KB） |
| [`SKILL.md`](./SKILL.md) | 平台技能声明（front-matter + 正文，v1.7.1） |
| [`CHANGELOG.md`](./CHANGELOG.md) | 版本变更历史与审计修复记录 |
| [`tests/REGRESSION_TESTING.md`](./tests/REGRESSION_TESTING.md) | 回归测试规范（用例编号 / 优先级 / 验收门槛） |

---

*本插件为研究工具，数据与模型输出均可能出错；任何交易决策由使用者自行承担风险。*
