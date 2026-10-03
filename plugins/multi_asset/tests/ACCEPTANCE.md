# multi_asset 统一测试验收报告

- **对象**：`plugins/multi_asset/`（新建插件，30 个文件）
- **依据**：《多资产插件设计》+ 专家审核报告（有条件放行：2×P0 / 7×P1 / 7×P2 / 7×P3 + i18n 专项）
- **原则**：按审核报告**修缺陷**，不照抄设计文档里的错误样例代码
- **执行环境**：Windows / Git Bash；Python `3.13.12`（managed venv）
- **本机 PostgreSQL 未运行** → 2 条真实建库用例按设计跳过（不伪造通过）

## 1. 复现方式

```bash
cd F:/Sites/VeroRun

# 单元 + 契约 + 路由 + 清单一致性（160 条，约 0.5s）
python -m unittest discover -s plugins/multi_asset/tests -t . -v

# i18n 门禁（默认；本次改动后与改动前同为 2 条 ai_relay 历史违规）
python scripts/i18n_check.py
python scripts/i18n_check.py --check-cn     # 追加硬编码中文扫描，本插件贡献 0 条

# 平台侧清单解析 + 插件审计（需替身绕开 DB 依赖的 agent_matrix 导入）
#   discovery: identifier/name/version/deps/hooks/permissions/settings 全部正确，last_error 为空
#   audit    : structure / similarity / dangerous / ast_dangerous / permission_consistency 五段全空
```

## 2. 施工结果总览

| 层面 | 结论 |
| --- | --- |
| 测试 | **160 通过 / 0 失败 / 2 跳过（DB）** |
| 编译 | `compileall` 全绿 |
| i18n 门禁 | 默认门禁无新增违规；`--check-cn` 本插件命中 0 条 |
| 平台 discovery | 清单解析通过，capability namespace 校验无告警，无 `admin_url` 弃用告警 |
| 平台 audit | 五段全部为空（仅"官方作者需人工复核"这一预期项） |
| 目录 | 30 个文件；Python 代码约 4.2k 行量级 |

## 3. 缺陷修复对照（P0 / P1）

| 缺陷 | 原问题 | 修复与证据 |
| --- | --- | --- |
| **P0-1** 资产识别算法失效 | `raw.upper()` 使小写分支**永假**；`startswith('T')` 吞并 CZCE `TA`；SHFE 白名单仅 4 品种 | 重写为**保留大小写**判别 + 显式品种映射表（CFFEX 8 / CZCE 26 / 商品 47）+ 月份合法性校验。`test_asset_symbol.FuturesParsingTest`（`TA605`→CZCE、`rb2610`→SHFE、品种表规模断言） |
| **P0-2** 债券分支死代码 | `head == '0'` 恒假；缺北交所/深 B | 重写为**最长前缀段表**并补齐 BSE `43/83/87/88/92`、深 B `200`、沪 B `900`。`CnCodeParsingTest`（债券 10 例可达、B 股是 EQUITY 而非 BOND） |
| **P1-1** 虚构 API `shared_fetch_via_gateway` | 全仓 0 命中 | 改用真实 `DataGateway` 单例（`adapters.providers.sa_gateway()`）。断言：插件内不存在该标识符 |
| **P1-2** 虚构锚点 `sa_bars` | 无此表 | 统一为**本系统第一张落库行情表** `ma_bars`。断言：插件内 `sa_bars` 0 命中、`ma_bars` 存在且唯一 |
| **P1-3** adapter 契约不匹配 | 样例 `_do_fetch(self, category, symbols, **kwargs)` 位置参数 + 复数符号 + 虚构 `required_secret` | 对齐真实契约：`_do_fetch(self, cat, *, symbol=None, **kw)`、`categories` frozenset、`SecretResolver` 注入。`ContractShapeTest` 用 `inspect.signature` 校验 |
| **P1-5** DDL 非幂等 | `CREATE TYPE ... AS ENUM` 无 `IF NOT EXISTS` | 改 `VARCHAR(8) + CHECK` + 全量 `IF NOT EXISTS`；断言无 `CREATE TYPE` / `AS ENUM`，且每个 `CREATE` 头含 `IF NOT EXISTS` |
| **P1-6** bars / ma_bars 自相矛盾 | 文档两套表名 | 断言"唯一时间序列表"：`endswith('bars')` 的表**有且仅有** `ma_bars` |
| **P1-7** routes / UI / jobs / health 缺位 | 设计只有数据层 | 补 `routes.py`（9 个端点 + 401/403/429）、`templates/multi_asset.html`（iframe 工作台）、`register_jobs`（17:30 参考数据刷新）、`register_health_checks`（DB + 源链双检查） |
| **P3-3** MIC 码存疑 | 设计写广期所 `XGEF` | 核验 ISO 20022 MIC 附录（2025-05）→ 官方 `XGFE`，已采用并断言 |
| i18n 专项 | 源码硬编码中文 | akshare 中文列名/参数值 + 中文品种名 → 移入 `data/*.json`（`asset_data.load_data`），`--check-cn` 命中 0 |

## 4. 施工中自行发现并修复的真实缺陷（审核报告未列）

| # | 缺陷 | 影响 | 修复 |
| --- | --- | --- | --- |
| A | `AssetProviderBase.categories` 为空 `frozenset` | 父类 `fetch()` 第一步 `supports_category()` 即拒绝 → **所有取数必然失败** | 新增 `cats()` 助手，三个 provider 显式声明 `KLINE/QUOTE/PROFILE` 等真实能力 |
| B | 日内频率会把日线"改个 key"存成 60m | 静默错数据（正是父类 `kline_freqs` 注释要防的那类） | 新增 `_guard_freq()`：日内频率直接抛 `ProviderUnavailable`（接口 503 `source_unavailable`），绝不伪装 |
| C | 大写厂商符号（`RB0`/`RB2610`）被拒 | akshare/新浪主力合约列表全是大写 → 参考数据同步会**整体失败** | 大写分支回退小写映射表（CFFEX/CZCE 集合优先，三集互斥已验证不误判） |
| D | `data_link.fetch_bars` 语法错误（`append(...)    try:` 粘连） | 模块无法导入 | 修复并重构该函数 |
| E | `fetch_bars` 未返回序列本体、日期非纯 JSON | 页面拿不到数据、下游 agent 反序列化失败 | 返回 `bars` 列表 + `trade_date_from/to` 转 ISO 字符串；`test_data_link` 断言 `json.dumps` 可通 |
| F | `_row_for_code` 返回 numpy 标量 | Flask JSON 编码抛 `TypeError` | 经 `to_json` 往返，保证纯 JSON 类型 |
| G | `_upsert` 的空 `update_cols` 分支脆弱 | 唯一键=全列时生成非法 SQL | 改为 `DO NOTHING` 分支 |
| H | `upsert_bars` 返回批量大小而非真实写入数 | 指标虚报 | 改用 `cursor.rowcount`，取不到时回退批量值 |

## 5. 覆盖矩阵

| 验收主题 | 用例文件 | 结果 |
| --- | --- | --- |
| 资产识别（期货/期权大小写与月份位、6 位代码分段、CFI/MIC） | `test_asset_symbol.py`（36） | 通过 |
| 编排契约（payload 形状、JSON 可序列化、降级留痕、失败语义） | `test_data_link.py`（16） | 通过 |
| provider 契约 / 双源链 / failover / 冷却 / 频率诚实性 / 列名映射 | `test_contract_and_chain.py`（25） | 通过 |
| DDL 幂等 / 单表命名 / upsert 语义 / 真实建库往返 | `test_models_ddl.py`（20，2 跳过） | 通过（DB 项跳过） |
| 交易日归属（夜盘归次一交易日、CFFEX 无夜盘、周末顺延） | `test_trade_calendar.py`（12） | 通过 |
| HTTP 鉴权（401/403/429/成功、失败开放、输入校验、契约信封） | `test_routes_auth.py`（23） | 通过 |
| i18n 键集一致 / 模板无中文 / 清单↔实现一致 / settings 只暴露真实源 | `test_manifest_and_i18n.py`（28） | 通过 |

## 6. 诚实声明：本次**未**覆盖的范围

1. **未做真实联网取数**。测试全程 mock 适配层，未调用 akshare / 新浪 / 网关的真实接口。
   免费源字段与可用性属上游不确定项，须在联网环境做一次冒烟（建议：`/api/bars?symbol=rb2610`）。
2. **未在本机建库**。`ensure_tables()` 的"跑两遍不报错"与 `ma_bars` 幂等往返两条用例因 PostgreSQL
   未运行而**跳过**（报告中标记为 skipped，未计入通过数）。需在有 PG 的环境补跑。
3. **未在真实平台加载**。`PluginDiscovery` / `audit` 通过替身绕开了 `agent_matrix.models` 的
   DB 导入链；`resolve_agent_roles()` 的真实裁决（含发行版角色集）未在本机跑通。
4. **未在金融桌面版启用**。按"只改本插件"的约束，`plugins.include` 与 `rs_*` 的
   `managed_modules` 未改动 —— 该缺口已登记于 `docs/role-integration.md`（R1/R2/R3），
   **在维护者补齐前，本插件在金融桌面版上会被 `missing/invalid agent_role` 拒绝启用**。
5. 期权历史、部分债券/场外基金的免费源覆盖度随上游变动，未做全样本验证。
