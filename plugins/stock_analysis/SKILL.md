---
identifier: stock-analysis-skill
name: Stock Analysis Skill
description: A股股票分析技能 — 自包含技术面/估值/情绪面分析 + UnifiedLLM 综合研判，输出带证据引用的结构化信号与中文研判
tagline: A 股研究助手：技术、估值、情绪与 AI 综合研判
version: 1.7.1
author: easykai
tags: [finance, stock, a-share, analysis, agent]
---

# Stock Analysis Skill v1.7.1

面向 VeroRun 的 A 股研究技能，由 `stock_analysis` 插件的自包含分析引擎驱动。
给任务型对话/工作流使用，用于回答单只 A 股或市场维度的研究类问题。

## 技能定位

- 输入：单个 A 股代码（如 `600519`，自动归一化 `sh/sz/bj` 前缀）。
- 输出：`signal`（buy/sell/hold）+ `confidence` + 证据引用的 `reasons` + 中文研判文本。
- 输出一律带风险披露，不构成投资建议，不虚构数据。

## 分析维度

| 维度 | 说明 |
| --- | --- |
| 技术面 | MA/RSI/MACD/支撑阻力，信号路径 RSI 采用 Wilder 权威实现 |
| 估值 | PE(TTM)/PB 实时字段评分；tushare 授权时叠加近 5 年估值分位 |
| 情绪面 | 新浪新闻标题关键词统计，附近期新闻示例 |
| 综合研判 | UnifiedLLM（standard tier）综合，Agent 护栏系统消息强制前置 |
| 证据链 | 财报四表/资金流/新闻/估值分位打包入 prompt，逐源降级留痕（"未获得(原因)"） |

## 使用方式

### CLI

```bash
python stock_skill.py 600519
python stock_skill.py --help
```

### Python API

```python
from stock_skill import StockAnalysisSkill

skill = StockAnalysisSkill()
result = skill.analyze("600519", analysis_type="llm", months=6)
print(result.to_text())

signal = skill.get_signal("600519")
```

### MCP 工具（对平台 Agent 暴露）

`mcp__stock_analysis__stock__*`：`get_quote` / `get_kline` / `get_technical_signal` /
`get_fundamental_digest` / `market_overview`（快路径，秒级返回，不触发 LLM）。

### 工作流（DAG）节点

- `stock_deep_research`：证据收集 → LLM 深度研判 → 结构化信号；可复接任意上游节点产出的 `evidence_text`。

## 结论沉淀与扩展点

- 每次成功分析写入 `sa_signal_log`（同锚点幂等）。
- 分析结论自动沉淀平台知识库（`kb_stock_<symbol>_<date>_<kind>`，ON CONFLICT 幂等）。
- 告警触发派发 `stock.alert.triggered` 钩子，供通知/审计/第三方推送消费者接入。

## 数据源

| 数据 | 来源 | 说明 |
| --- | --- | --- |
| 历史日线 | tushare（授权主源，含后复权）→ akshare（备源）→ sina（兜底） | failover 链按 `supports()` 探针裁剪 |
| 实时行情 | Tencent Finance (qt.gtimg.cn) | 现价、涨跌幅、PE(TTM)、PB、换手率 |
| 基本面 | Tencent Finance + tushare（需 token） | PE/PB 实时估值 + 深财报四表 |
| 情绪分析 | Sina Finance + 本地词典 | 新闻标题情绪统计 |
| 交易日历 | 内置 2024/2025 年度 CSV + 进程内年缓存 | 节假日/开市判定，缺年度走周末兜底 |

## 使用前提

- Python 3.11+，VeroRun 0.59.3+
- VeroRun 已配置可用的 UnifiedLLM 模型
- 财报/估值分位/资金流需用户自备 tushare token（`TUSHARE_TOKEN` 环境变量或插件设置页 `tushare_token`），无 token 自动降级不报错
- 行情数据源（Sina / Tencent Finance）网络可达

## 边界与护栏

- 分析结果为研究信息与风险提示，不是自动交易指令。
- `confidence` 为启发式信号强度，非统计置信度。
- 符号输入仅接受字母/数字/点，≤12 字符；外部 URL 硬编码，无 SSRF。
