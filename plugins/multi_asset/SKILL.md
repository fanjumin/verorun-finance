---
identifier: multi-asset-skill
name: Multi-Asset Data Skill
description: 多资产（期货/期权/基金/债券）行情与参考数据技能 — CFI 对齐的标的识别、交易所双字段建模、链式 failover 取数与本地落库，输出带来源与风险披露的结构化数据
tagline: 期货/期权/基金/债券 统一取数与入库
version: 1.2.0
author: VeroRun
tags: [finance, futures, options, funds, bonds, multi-asset, agent]
---

# Multi-Asset Data Skill v1.2.0

面向 VeroRun 的**多资产数据 + 数据治理底座**技能，由 `multi_asset` 插件驱动。
它不重复造数据层：标的识别与取数链路复用 `stock_analysis` 的 provider 契约
（`BaseProviderV2`）与 `DataGateway`；在此之上自辖落库与溯源，让每一行行情
随行携带许可与分级标签（`license_id` / `security_level` / `origin` / `dataset_id`）。

## 技能定位

- 输入：原始标的代码（如 `SA605`、`IF2603`、`510300`、`019547`、`SR605`）。
- 输出：归一化标的身份 + OHLCV/净值序列 + 报价/概况，标注**数据源**与**是否落库**。
- 期货/期权输出**强制附带杠杆与适当性披露**，不构成投资建议，不虚构数据。

## 标的识别（本插件的核心）

| 资产 | 代码形态 | 识别要点 |
| --- | --- | --- |
| 期货 | CZCE 大写 3 位月份（`SA605`）；SHFE/INE/DCE/GFEX 小写 4 位（`rb2610`）；CFFEX 大写 4 位（`IF2603`） | **大小写是判别位**，绝不 `upper()` 后判断 |
| 期权 | 同期货品种前缀 + 行权价后缀（合约明细来自 `ma_option_contracts`） | 品种→交易所，行权/到期来自参考表 |
| 基金 | `510300`（沪 ETF）/`159915`（深 ETF）/`16xxxx`/`18xxxx`（LOF）/6 位场外 | 最长前缀段表，区分沪深 |
| 债券 | 沪 `019xxx`/`018xxx`/`010xxx`/`110xxx`…；深 `112xxx`/`123xxx`/`127xxx`/`128xxx`… | 与深市 B 股（`200xxx`）、北交所（`43x`/`83x`/`87x`/`88x`/`92x`）显式分离 |
| 股票 | 沪 `6xx`、深 `00x`/`30x`、科创 `688`/`689`、北交所 | 落库但取数仍走 `stock_analysis` |

CFI 分类（ISO 10962:2021 首字母）：`E` 股票、`D` 债券、`C` 集合投资工具、
`F` 期货、`O` 期权。交易所 MIC（ISO 10383）：郑商所 `XZCE`、大商所 `XDCE`、
上期所 `XSHG` 系、广期所 `XGFE`、中金所 `XCFE`… 见 `asset_symbol.EXCHANGE_MIC`。

## 数据源与降级

- 每类资产声明**双源链**，链内 failover，连续 3 次失败冷却 300 秒：
  - 期货/期权：`akshare` → `sina`（新浪期货日线 jsonp）
  - 基金/债券：`akshare` → `sa_gateway`（复用 stock_analysis DataGateway 单例）
- 用户在插件设置里选 `data_provider` 时，该源被提到链首。
- **缺 `stock_analysis` 依赖时不假装可用**：`CONTRACT_AVAILABLE=False`，所有 provider 抛
  `ProviderUnavailable`，健康检查明确报"不可用"而非绿灯。

## 使用方式

### HTTP API（iframe 页面 `menu.embed_url` = `/admin/multi-asset/`）

```
GET /admin/multi-asset/api/constants             # 资产类型/交易所/MIC/频率/源链
GET /admin/multi-asset/api/resolve?symbol=SA605   # 归一化标的身份
GET /admin/multi-asset/api/bars?symbol=rb2610&freq=daily&datalen=120
GET /admin/multi-asset/api/quote?symbol=510300
GET /admin/multi-asset/api/profile?symbol=019547
GET /admin/multi-asset/api/search?q=纯碱
GET /admin/multi-asset/api/storage               # 表规模 + 近期取数留痕
GET /admin/multi-asset/api/health                # 源链/契约/冷却状态
```

鉴权：`Authorization: Bearer <jwt>`（或 `?token=`，iframe 由平台注入）。
缺 token → 401；缺 `multi_asset.read` 权限 → 403；超频 → 429。

### Python API（跨插件，`get_instance` 后调用）

```python
from plugin_manager import get_plugin_manager

ma = get_plugin_manager().get_instance("multi_asset")
ma.resolve_symbol("SA605")                       # -> {asset_type, code, exchange, mic, key}
ma.fetch_bars("rb2610", freq="daily", datalen=60)  # -> {ok, rows, written, bars, source, ...}
ma.chain_sources("FUTURE")                        # -> ["akshare", "sina"]
```

### 提供的事件（hooks.provides）

- `asset.data.ready` — 参考数据同步/行情落库完成后派发（走 hook registry `do_action`）。

## 存储（独立 schema `multi_asset`）

| 表 | 内容 |
| --- | --- |
| `ma_bars` | **唯一**时间序列表（`asset_type, symbol, exchange, freq, trade_date, bar_time` 唯一键，幂等 upsert） |
| `ma_fund_ref` / `ma_bond_ref` | 基金/债券参考（代码、交易所、名称、类型） |
| `ma_future_contracts` / `ma_option_contracts` / `ma_option_greeks` | 合约规格、到期、行权、希腊字母 |
| `ma_fetch_log` | 取数留痕（来源、provenance、成功/失败、降级原因） |

### 治理骨架表（只有结构，当前无读写实现）

| 表 | 用途 |
| --- | --- |
| `ma_license` | 许可台账；`allow_export/forward/llm` 默认全 0 = 默认最严 |
| `ma_dataset_registry` | 数据集注册（来源性质、许可、级别、增量水位线） |
| `ma_pit_fundamentals` / `ma_pit_consensus` | PIT 双时态（`valid_from` × `system_from` / `system_to`） |
| `ma_index_membership` | 指数成分历史（含 `valid_to`） |
| `ma_quality_report` | 质量校验报告落点 |

`ma_bars` 另有 5 列治理标签：`license_id` / `security_level`（1~4 级）/ `origin` /
`dataset_id` / `ingested_at`。`origin` 与 `source` 不同——`source` 是取数通道名，
`origin` 是来源性质。

**交易日归属口径**：期货夜盘（≥21:00）成交归属**次一交易日**，日盘归属当日；基金按净值
公布日；统一由 `trade_calendar.assign_trade_date()` 计算，展示层不得自行推断。

## 使用前提

- Python 3.11+，`stock_analysis >= 2.0.1`（provider 契约 + DataGateway，硬依赖）
- `akshare >= 1.14.0`（主源）；无网络或免费源限流时按链降级并在 `ma_fetch_log` 留痕
- 需可用的 PostgreSQL（独立 schema 建表，幂等自愈）

## 边界与护栏

- **免费源无 SLA**：akshare/新浪为公开免费接口，字段与可用性可能变动；本插件"如实降级"
  而非编造数据——取不到就报 `ProviderUnavailable` 并留痕。
- 期货/期权为杠杆衍生品，接口返回的 `meta.risk_disclosure` 与 `suitability=professional_only`
  是必须随数据一起透出的合规字段。
- 频率诚实性：免费链只提供 `daily/weekly/monthly`（周/月由日线聚合，非伪装）。日内频率
  （60m/30m/15m/5m/1m）若数据源不支持，provider 直接报 `ProviderUnavailable`（接口返回
  503 `source_unavailable`），**绝不把日线改个 key 存成 60m**。
- 未接通付费源时，期权历史与部分债券/场外基金可能取不到数据——这是免费源的客观边界，
  已如实暴露（`/api/health`、`ma_fetch_log`），不静默返回空序列冒充成功。
- 本插件只做**数据**；不做交易指令、不做仓位建议。
- **治理能力尚未实现**：许可台账 / 数据集注册 / PIT 双时态 / 质量报告（共 6 张表）本轮
  只建结构，出域闸与 as-of 查询均未实现，因此 `capabilities` **不声明** `data.*` 能力。
