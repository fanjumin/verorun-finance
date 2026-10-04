# multi_asset — 多资产数据插件

期货 / 期权 / 基金 / 债券四类资产的**取数 + 建模 + 落库**插件，定位为
**资产覆盖 + 数据治理底座**。

它对 `stock_analysis` 是**同域补位**，而不是第二套数据层：标的识别复用
`asset_symbol.py`，取数复用 `stock_analysis` 的 `BaseProviderV2` 契约与 `DataGateway`。
在此之上，本插件自辖存储层，并把**许可与分级标签做进数据结构**（见 §6）。

> 定位一句话：**股票归 `stock_analysis`，非股票归 `multi_asset`；两者共用同一内核契约。
> `multi_asset` 另外持有落库与溯源这一层——每一行行情都随行携带 `license_id` /
> `security_level` / `origin` / `dataset_id`。**
>
> 依据 GB/T 42775-2023「从外部获取的数据不得低于提供方所定级别」：用户导入的授权数据
> 在库内**仍受原许可约束**，来源与许可必须原样继承，不能"入库即自由"。

## 1. 为什么需要它

`stock_analysis` 的 secmaster / provider 路由只认识 6 位股票代码：
`market != GLOBAL` 时会被 secmaster 收窄，"`SA605` 是什么""`519xxx` 是沪债还是深 B 股"
这类问题在股票域里没有答案。`multi_asset` 用**一个权威识别模块**回答它，并把结果落进
**独立 schema**，不污染股票域的表。

## 2. 目录结构

```
plugins/multi_asset/
├── plugin.json                 # 清单（permissions / hooks / settings / menu / capabilities）
├── __init__.py                 # MultiAssetPlugin（生命周期 + 跨插件 API）
├── asset_symbol.py             # ★ 唯一权威标的识别（CFI / MIC / 交易所代码规则）
├── asset_data.py               # data/*.json 懒加载器（把 CJK 字面量挡在 .py 之外）
├── models.py                   # 独立 schema 建表 / upsert / 查询 / 取数留痕
├── trade_calendar.py           # 交易日归属（夜盘归次一交易日）
├── data_link.py                # 编排：resolve -> fetch -> persist -> audit
├── events.py                   # 派发 asset.data.ready（hook registry 通道）
├── ref_sync.py                 # 参考数据同步（主力合约 / ETF 列表）
├── routes.py                   # Flask Blueprint /admin/multi-asset
├── adapters/
│   ├── base.py                 # AssetProviderBase（对齐真实 BaseProviderV2 契约）
│   ├── providers.py            # akshare / sina / sa_gateway 三个真实源
│   └── __init__.py             # 注册表 + 每类资产双源链 + 冷却 failover
├── data/                       # JSON 数据资产（vendor 列名、中文品种名）
├── i18n/{en.yml,zh-CN.yml}     # 键集一致的翻译（英文源串即 Key）
├── templates/multi_asset.html  # iframe 独立页面（menu.embed_url）
├── docs/role-integration.md    # 金融版角色编排接入登记
├── SKILL.md / README.md
└── tests/                      # 统一验收测试（V-01…V-14）
```

## 3. 标的识别规则（对齐行业标准）

| 标准 | 用途 |
| --- | --- |
| ISO 10962:2021 CFI | 资产类别首字母：`E` 股 / `D` 债 / `C` 基金 / `F` 期货 / `O` 期权 |
| ISO 10383 MIC | 交易所市场识别码（广期所官方 = `XGFE`，非 `XGEF`） |
| ISO 6166 ISIN | 代码预留位（未启用付费源时不生成） |
| 交易所本地代码规则 | 见下表 |

| 交易所 | 合约代码规则 | 例 |
| --- | --- | --- |
| CZCE 郑商所 | **大写** + 3 位月份（无世纪位） | `SA605`、`TA605` |
| SHFE/INE/DCE/GFEX | **小写** + 4 位（年份末位 + 月份） | `rb2610`、`sc2612`、`m2609` |
| CFFEX 中金所 | **大写** + 4 位 | `IF2603`、`T2603` |
| 连续合约 | 品种 + `0` | `RB0`、`SA0` |

**大小写是判别位**：`parse_future_symbol()` 保留原始大小写判别交换所以及品种→交易所归属，
绝不先 `upper()` 再判断（这正是设计初稿的 P0-1 缺陷）。

6 位数字代码走**最长前缀段表**（`parse_cn_code`）：沪 `600/601/603/605`、科创 `688/689`、
深 `000/001/002/003`、创业 `300/301/302`、北交所 `43x/83x/87x/88x/92x`（8 开头需细分
以免与沪债 `019` 混淆）、沪 B `900`、深 B `200`、沪基金 `5xx`、深基金 `159/15x/16x/18x`、
沪债 `019/018/010/110/111/113…`、深债 `112/123/127/128…`。

## 4. 数据源与降级链

| 资产 | 链（failover 顺序） | 说明 |
| --- | --- | --- |
| 期货 | `akshare` → `sina` | akshare `futures_zh_daily_sina`；备源直连新浪期货日线 jsonp |
| 期权 | `akshare` → `sina` | akshare `option_hist_{shfe,dce,czce,gfex}`（按交易所探测） |
| 基金 | `akshare` → `sa_gateway` | ETF `fund_etf_hist_em` / LOF `fund_lof_hist_em` / 场外净值 `fund_open_fund_info_em` |
| 债券 | `akshare` → `sa_gateway` | `bond_zh_hs_daily`（自动补 `sh`/`sz` 前缀） |

- 冷却：同一 (资产, 源) 连续 3 次失败 → 摘除 300 秒（照搬 stock_analysis gateway 语义）。
- 设置项 `data_provider` 非空时，命中的源被提到链首。
- `stock_analysis` 不在时 `CONTRACT_AVAILABLE=False`，所有源抛 `ProviderUnavailable`，
  健康检查显式报不可用（不假装数据源是好的）。

## 5. HTTP 接口

全部位于 `/admin/multi-asset`，统一契约 `{ok, data, error, meta}`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | iframe 工作台页面（平台注入 `?token=`） |
| GET | `/api/constants` | 资产类型 / 交易所 / MIC / 频率 / 源链 |
| GET | `/api/resolve?symbol=` | 标的归一化（类型、交易所、MIC、存储键） |
| GET | `/api/bars?symbol=&freq=&datalen=&persist=` | 行情/净值序列（取数 → 落库 → 留痕） |
| GET | `/api/quote?symbol=` | 报价快照 |
| GET | `/api/profile?symbol=` | 标的概况 |
| GET | `/api/search?q=` | 品种检索 + 参考表检索 |
| GET | `/api/storage` | 表规模 + 近期取数留痕 |
| GET | `/api/health` | 源链 / 契约 / 冷却状态 |

鉴权与限流：JWT（`Authorization: Bearer` 或 `?token=`），权限 `multi_asset.read`；
401 / 403 / 429 语义与 `stock_analysis` 一致。期货与期权的响应在 `meta` 里强制带
`risk_disclosure` 与 `suitability: professional_only`。

## 6. 数据模型

独立 schema `multi_asset`（`SET search_path TO multi_asset, public`），DDL 全部幂等
（`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`），**不使用 PG 枚举类型**
（`CREATE TYPE ... AS ENUM` 无 `IF NOT EXISTS`，重跑必然 `duplicate_object`）——
资产类型用 `VARCHAR(8) + CHECK` 表达等价语义。

| 表 | 主键/唯一键 | 说明 |
| --- | --- | --- |
| `ma_bars` | `(asset_type, symbol, exchange, freq, trade_date, bar_time)` | 全系统**唯一**行情/净值序列表 |
| `ma_fund_ref` | `(code, exchange)` | 基金参考（名称、类型、交易所） |
| `ma_bond_ref` | `(code, exchange)` | 债券参考（票息、到期、评级） |
| `ma_future_contracts` | `(symbol, exchange)` | 合约规格（乘数、最小变动、最后交易日、主力标记） |
| `ma_option_contracts` | `(symbol, exchange)` | 期权合约（类型、行权价、到期、标的） |
| `ma_option_greeks` | `(symbol, trade_date)` | 希腊字母（Delta/Gamma/Vega/Theta/Rho） |
| `ma_fetch_log` | `id` | 取数留痕（来源、provenance、成败、降级原因） |

### 6.1 治理骨架表（结构预留，本轮无读写实现）

以下 6 张表在 2026-10-01 一次建成，**只有结构、没有业务代码**。理由是表结构属于
不可逆决策，晚改比早改贵一个数量级；而当时 `ma_bars` 尚无真实数据，加列/建表是
纯结构变更，等数据进来之后来源与许可就再没有可回填的依据了。

| 表 | 唯一键 | 说明 |
| --- | --- | --- |
| `ma_license` | `license_id` | 许可台账。`allow_export` / `allow_forward` / `allow_llm` **默认全 0 = 默认最严**（导入当下无法验证用户许可，事后也无法自证） |
| `ma_dataset_registry` | `dataset_id` | 数据集注册（来源性质、许可、级别、增量水位线） |
| `ma_pit_fundamentals` | `(symbol, exchange, report_period, metric, valid_from, system_from)` | 财报 PIT 双时态 |
| `ma_pit_consensus` | `(symbol, exchange, forecast_period, metric, valid_from, system_from)` | 一致预期 PIT 双时态 |
| `ma_index_membership` | `(index_code, symbol, valid_from)` | 指数成分历史（含 `valid_to`，支撑无偏回测） |
| `ma_quality_report` | 无（append-only 流水） | 质量校验报告落点（`severity ∈ info/warn/block`） |

**`ma_bars` 同时新增 5 列**：`license_id` / `security_level`（1~4 级，GB/T 42775-2023）/
`origin` / `dataset_id` / `ingested_at`。与既有 `source` 列的分工是明确的：
`source` = **取数通道名**（akshare / sina）；`origin` = **数据来源性质**
（exchange / legal_disclosure / public_feed / vendor / user_file）。

**交易日归属**：日/周/月线取数据源自带交易日期；日内线经 `trade_calendar.assign_trade_date()`
计算——夜盘（≥21:00）归属**次一交易日**，日盘归属当日；CFFEX 指数期货无夜盘。

## 7. 定时任务与健康检查

- 任务 `multi_asset_ref_refresh`：周一至周五 17:30，同步主力合约与 ETF 列表（best-effort，
  源不可用只记日志）。
- 健康检查 `multi_asset_db`（schema 可连）、`multi_asset_sources`（每类资产源链 ≥ 2 且契约可用）。
  冷启动空参考表视为健康，不产生恒假告警。

## 8. 依赖

- 硬依赖：`stock_analysis >= 2.0.1`（provider 契约 + DataGateway）
- Python：`pyyaml`、`pandas`、`akshare >= 1.14.0`
- 无强制外部 API key：免费源开箱可用，付费源未接入前按链降级并留痕

## 9. 已知边界（诚实声明）

1. 免费源无 SLA，字段随时可能变动；取不到就报错+留痕，不编造。
2. 期权历史依赖 akshare 的交易所专用接口，覆盖度随上游变动；未接通付费源时可能空缺。
3. 日内频率不由日线伪装；无原生日内源时接口返回 503 而不是错位数据。
4. `sa_gateway` 源只服务股票型代码，基金/债券用它属于**兜底**，通常走不到链首。
5. **治理能力尚未实现**：§6.1 的 6 张表当前只建了结构。出域闸（导出 / 日志 / LLM 上下文）、
   PIT as-of 查询、校验规则集一个都没实现，因此 `capabilities` **刻意不声明**
   `data.ingest` / `data.pit` / `data.quality` / `data.classification`——声明未实现的能力
   等于让清单说谎，与本仓清单诚实性要求冲突。完整分期与触发条件见
   《VeroRun 金融版 — 长历史数据接入与合规边界方案》§8、§11。
