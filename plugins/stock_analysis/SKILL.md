---
name: stock-analysis-skill
description: A股股票分析插件 — 自包含技术面/基本面/情绪面分析 + VeroRun UnifiedLLM 综合研判
version: 1.0.0
author: easykai
tags: [finance, stock, a-share, analysis, agent]
---

# Stock Analysis Skill v1.0

基于自包含引擎的 A 股分析技能，提供均线、RSI、MACD、估值字段和新闻情绪分析。

## 前提条件

- Python 3.11+
- VeroRun 0.59.3+
- VeroRun 已配置可用的 UnifiedLLM 模型

## CLI 使用

```bash
# 分析单只股票
python stock_skill.py 600519

# 查看帮助
python stock_skill.py --help
```

## Python API

```python
from stock_skill import StockAnalysisSkill

skill = StockAnalysisSkill()

# 分析股票
result = skill.analyze("600519", analysis_type="llm")
print(result.to_text())

```

## 数据源

| 数据 | 来源 | 说明 |
|------|------|------|
| 历史行情 | Sina Finance | 日线 OHLCV |
| 实时行情 | Tencent Finance (qt.gtimg.cn) | 实时价格、PE、换手率 |
| 基本面 | Tencent Finance + 本地评分引擎 | PE/PB 等实时估值字段 |
| 情绪分析 | Sina Finance + 本地词典 | 新闻标题情绪统计 |
