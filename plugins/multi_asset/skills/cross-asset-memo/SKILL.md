---
identifier: cross-asset-memo
name: Cross-Asset Investment Memo Skill
description: 跨资产（股票/期货/基金/债券）投资研究工作流技能——按金融版研究角色流水线组织研究，强制证据链与反方意见，复用单股分析与多资产取数能力，产出带合规留痕的投资备忘录（非交易指令）
tagline: 股期债基组合研究 → 合规投资备忘录
version: 1.0.0
author: VeroRun
tags: [finance, portfolio, asset-allocation, multi-asset, research-workflow, memo]
---

# Cross-Asset Investment Memo Skill v1.0.0

面向金融版（finance / finance-desktop）的**跨资产投研编排**技能。它不重复造单股分析、
也不重复造取数：把 `stock_analysis` 的单股研判/组合指标与 `multi_asset` 的多资产数据
**编排**成一条可审计的研究流水线，最终交付一份合规的投资备忘录。

## 技能定位（本技能做什么 / 不做什么）

- **做**：组织跨资产研究流程、组合层面的配置视角、强制反方意见与合规留痕、输出备忘录。
- **不做**：
  - 不做单股技术/估值/情绪研判 → 委托 `stock-analysis-skill`（`mcp__stock_analysis__stock__*`、DAG `stock_deep_research`）；
  - 不做期货/期权/基金/债券的行情取数 → 委托 `multi_asset`（`resolve_symbol` / `fetch_bars`）；
  - 不做自动下单、不做仓位指令（`multi_asset` 与本技能同此边界）。

## 何时启用（task_types）

挂在金融版对话通道 `chat` 上（与个股诊断技能同通道）。注入器为精确匹配，且同次对话只
注入一个技能——在对话中由 Agent 依据本技能方法论，自行识别"跨资产配置 / 组合建议 /
投资备忘录"类问题并启用，不主动接管单股诊断类问题。

## 研究流水线（六步，对齐金融版角色）

| 步 | 角色 | 动作 | 产出物 |
|---|---|---|---|
| 1 定题 | rs_planner | 明确投资问题、约束、风险偏好、可投资产范围 | 研究提纲 |
| 2 个股深度 | rs_fundamental | 对候选个股调用 `stock_deep_research` / 单股信号 | 单股证据包 |
| 3 多资产数据 | rs_quant | 用 `multi_asset.fetch_bars` 取期货/基金/债序列，组合 `portfolio.py` 的行业暴露/集中度/Beta/VaR/Brinson | 组合风险画像 |
| 4 反方（强制） | rs_risk | 必须唱反调：列多头逻辑的证伪点、衍生品保证金/杠杆风险、尾部情景 | 反向意见节 |
| 5 配置 | rs_pm | 给出股/期/债/基的**方向性配置建议**（区间而非精确点位） | 配置草案 |
| 6 过闸 | rs_compliance | 核对披露与适当性，不通过则退回 4/5 | 合规留痕 |

## 证据链与诚实性（反幻觉硬要求）

- 每条结论必须挂来源：单股→`evidence_bundle`；多资产→`ma_fetch_log` 的 source/provenance。
- **PIT 双时态当前未实现**（`multi_asset` 的 `ma_pit_*` 仅空表）：一律使用"事后可见数据"，
  备忘录须写明"非 point-in-time，存在未来信息偏差"，不得假装已做 PIT。
- 取不到的数据如实报缺口与降级原因，**禁止编造序列**。

## 合规与适当性硬护栏

- 凡涉及期货/期权，输出必须随附 `risk_disclosure` 与 `suitability=professional_only` 字段。
- 全文结尾固定声明："本备忘录为研究信息，不构成投资建议；衍生品含杠杆，参与需满足适当性要求。"
- 未经 rs_risk 反方节、rs_compliance 过闸，不得输出最终配置结论。

## 输出物模板（投资备忘录）

```
# 投资备忘录 · <标题>
1. 投资观点（一句话）
2. 证据摘要（个股 / 多资产，逐条挂来源）
3. 组合风险画像（行业暴露、集中度、Beta/VaR、Brinson 归因）
4. 反方意见（rs_risk 强制）
5. 配置方向（股/期/债/基，区间建议）
6. 合规留痕（适当性、披露、PIT 声明）
7. 风险提示与免责声明
```

## 依赖与前提

- 插件：`stock_analysis >= 2.0.1`（组合指标/单股证据）、`multi_asset >= 0.1.0`（多资产数据）。
- 金融版角色：rs_planner / rs_fundamental / rs_quant / rs_risk / rs_pm / rs_compliance。
- 仅在 `editions=[finance]` 生效。
- 免费数据源（akshare/新浪）无 SLA，日内频率取不到即如实 503，不把日线伪装成分钟线。
