# stock_analysis 插件用法说明

VeroRun 股票分析插件：为管理员与金融分析 Agent 提供 A 股技术面、估值、情绪面与 AI 综合研判能力。
本文件由插件商店同步自动入平台知识库（`kb_plugin_usage_stock_analysis`），供"股票分析插件怎么用"类问题检索。

## 能力清单

- 技术面分析：MA5/20/60、RSI（Wilder）、MACD、支撑/阻力与技术信号
- 估值分析：PE(TTM)/PB 实时字段评分；tushare 授权时叠加近 5 年估值分位与资金流
- 情绪面分析：新浪新闻标题关键词统计（附近期示例可核验）
- AI 综合研判：UnifiedLLM（standard tier）综合技术面+证据链，输出结构化信号与中文研判
- 证据链：财报四表/资金流/新闻/估值分位打包入 prompt，逐源降级留痕
- 市场概况：上证/深成/创业板指实时点位与涨跌幅
- 自选池与批量任务：watchlist 管理、定时批量分析、结果检索与导出
- 信号质量与兑现：`sa_signal_log` 同锚点幂等落库；16:00 增量回算 `sa_signal_realized`
- 告警规则：price_above/below、涨跌幅等规则周期扫描（60s）+ SSE 事件流 + `stock.alert.triggered` 钩子
- 结论沉淀：成功分析自动写入平台知识库（`kb_stock_<symbol>_<日期>_<kind>`，幂等）
- 交易日历：内置 2024/2025 年度数据，节假日/开市判定，缺年度走周末兜底
- 工作流集成：`stock_deep_research` DAG 节点（证据收集 → LLM 深研 → 结构化信号）

## 端点表（管理后台）

前缀 `/admin/stock-analysis`，全部要求管理员身份（`sso_token` Cookie / Bearer / `X-Token`）。

统一响应信封：`{ "ok": bool, "data": …, "error": …, "meta": … }`。失败（含 401/403/429）为 `{ "ok": false, "data": null, "error": "…", "meta": null }` + 对应状态码，无 `success` 字段。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/` | 管理页面（iframe） |
| GET | `/api/analyze?symbol=600519&type=llm` | 个股分析（type: technical/fundamental/sentiment/llm；months 1-36） |
| GET | `/api/signal?symbol=600519` | 快速技术信号 |
| GET | `/api/market` | 市场概况 |
| GET/POST/DELETE | `/api/watchlist` | 自选池增删查 |
| POST | `/api/batch/run` | 批量分析 watchlist |
| GET | `/api/batch/results` | 批量结果检索 |
| GET | `/api/batch/export` | 批量结果导出 |
| GET | `/api/fundamental-detail?symbol=600519` | 财报四表明细（需 tushare） |
| GET | `/api/moneyflow?symbol=600519` | 资金流（需 tushare） |
| GET | `/api/signal-quality` | 信号质量统计 |
| POST | `/api/signal-realize` | 手动触发信号兑现回算 |
| GET/POST | `/api/deps/status` `/api/deps/install` | 可选依赖（akshare）状态与安装 |
| GET | `/api/kline?symbol=600519&limit=60` | 日 K（含复权基准标注） |
| GET | `/api/quotes?symbols=...` | 批量实时行情 |
| POST | `/api/jobs` | 创建定时分析任务 |
| GET | `/api/jobs/<job_id>` | 任务状态/结果 |
| GET/POST/DELETE | `/api/alerts` | 告警规则增删查 |
| GET | `/api/alerts/events` | 告警事件列表 |
| GET | `/api/events` | SSE 事件流（alerts/jobs 主题） |

## tushare token 配置

财报四表/估值分位/资金流为授权数据，需用户自备 Tushare Pro token。读取优先级：

1. 环境变量 `TUSHARE_TOKEN`
2. PluginManager 持久化配置：管理后台插件设置页 `tushare_token`
3. 插件 `config.yaml` 的 `TUSHARE_TOKEN`（兼容小写键）

未配置 token 时相关能力自动降级（探针裁剪/留痕"未获得(原因)"），不影响技术面与行情功能。

## 桌面端对接契约摘要

| 能力 | 契约 |
| --- | --- |
| 行情快查（聊天页工具） | MCP 工具 `mcp__stock_analysis__stock__get_quote/get_kline/get_technical_signal/get_fundamental_digest/market_overview` |
| 知识库展示 | B2 生效后知识库页可见 `kb_stock_*` 沉淀条目 |
| 工作流 | 可用 `stock_deep_research` 节点运行深研流程（config: `{"symbol":"600519"}`） |
| 告警投递 | 监听 `stock.alert.triggered` 钩子事件（payload: alert_id/symbol/name/type/threshold/observed/message/channels/at） |

## 免责声明

本插件输出为研究信息与风险提示，不构成投资建议；`confidence` 为信号强度而非统计置信度。
