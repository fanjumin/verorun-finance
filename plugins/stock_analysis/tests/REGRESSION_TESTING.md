# stock_analysis 插件回归测试规范（REGRESSION_TESTING）

> 本文件为 **2026-08-31 重建版**。原版在 v1.5.0 回归测试前已被删除且从未进入 git 历史
> （2026-08-31 核查确认 `git log --all -- tests/REGRESSION_TESTING.md` 无任何提交）。
> 重建依据：`VeroRun_stock_analysis_v1.5.0_回归测试报告_20260830.md` 引用的用例 ID/优先级/章节，
> 以及插件 v1.5.0 代码现状。用例编号与 §8 门槛与原报告引用一致；个别原版细节无法恢复，
> 以「重建推断」标注。使用前请与插件当前版本代码核对。

---

## 1. 总则

### 1.1 目的
对 `plugins/stock_analysis` 插件进行系统回归，覆盖数据网关、分析引擎、缓存、边界容错、合规输出、
鉴权限流、Watchlist、批量与 DB 落库，为版本发布提供验收依据。

### 1.2 范围
- 插件：`plugins/stock_analysis`（版本随被测分支）
- 服务端：`https://agent.easykai.cn`（生产/预发）
- 本地替代环境：真实数据源直驱（新浪/腾讯），用于服务端不可用时的可覆盖用例

### 1.3 优先级定义
| 级别 | 定义 |
|---|---|
| P0 | 核心链路可用性 / 数据正确性 / 安全漏洞。P0 失败即阻塞发布 |
| P1 | 主要功能缺陷 / 可用性问题，须在发布前修复 |
| P2 | 次要功能 / 边界 / 体验问题，可排期修复 |

### 1.4 缺陷分级与判定
- **P0（Blocker）**：核心链路不可用、**数据错误**（陈旧/错值/失真输出且用户无感知）、安全漏洞。
- **P1（Major）**：主要功能降级、可用性受损（如单点误操作影响全局、报错不可理解）。
- **P2（Minor）**：次要问题，不影响主链路。
- 判定用例失败前，必须先排除环境干扰（冷却污染、缓存掩盖、非交易日），见 §7.2。

---

## 2. 测试环境与配置

### 2.1 目标环境
- 服务端：`https://agent.easykai.cn`（`guxiao` 账号）
- 本地替代环境（服务端不可用时）：Python 3.11.7 + `F:/Sites/VeroRun/.stock_deps` + psycopg2 桩（仅进程内存）

### 2.2 数据源
| 源 | 用途 | 备注 |
|---|---|---|
| tushare（token） | K线/基本面/资金流 | 无 token 时自动降级 |
| akshare | K线备源 | 可选依赖，未安装自动降级 |
| sina | K线/新闻兜底 | 北交所早期代码段存在数据停更风险 |
| tencent | 行情/指数 | — |

### 2.3 测试账号
管理员：`guxiao`（登录 + `is_admin` claim 需验证）。

### 2.4 配置项记录
| 配置项 | 记录位置 | 说明 |
|---|---|---|
| `DATA_PROVIDER` | config.yaml | 首选源覆盖；未配置按 ROUTE 默认顺序 |
| `TUSHARE_TOKEN` | env / config.yaml | 未配置时探针输出 `tushare probe skipped` |
| `DATA_CACHE_DIR` | config.yaml / env `STOCK_DATA_CACHE_DIR` | 当日落盘缓存目录 |
| 插件 ACTIVE | 管理后台 | 需人工确认 |
| 服务器时区 | 服务器 | 影响交易日/批次判定 |
| `is_admin` claim | JWT | 需验证 |

---

## 3. 测试约束与红线

### 3.1 只读原则
测试不得修改生产数据；除 Watchlist/批量用例外，不得产生写操作。

### 3.2 数据源要求
- 真实数据源（新浪/腾讯/tushare）驱动，非 mock；复现性以隔离进程 + 全新标的保证。
- 数据源不可用时，用例标注「阻塞-数据源不可用」，不判失败。

### 3.3 环境搭建
```bash
cd /f/Sites/VeroRun
PYTHONPATH="F:/Sites/VeroRun/.stock_deps" \
  "C:/Users/Administrator/.platformio/penv/Scripts/python.exe" <script.py>
```
脚本头部注入 psycopg2 桩（仅 `sys.modules`，不落盘、不改项目），见报告 §3.1。

### 3.4 生产数据红线
- 禁止创建新数据库、新表、新 migration、独立连接配置；
- 禁止修改系统核心模块（auth-center / admin / main_site / plugin_manager 等）；
- 禁止在服务器直接修改生产文件；测试改动须先本地验证；
- 测试产生的缓存/临时文件不得污染仓库（落盘缓存写临时目录或当日即清）。

---

## 4. 测试用例（57 条）

### 4.1 鉴权与限流（AUTH，8 条）

| 编号 | 用例 | 优先级 | 步骤 | 预期 |
|---|---|---|---|---|
| TC-SA-AUTH-01 | 页面未登录拦截 | P0 | 未登录访问批量面板 | 401，空响应体 |
| TC-SA-AUTH-02 | API 未登录拦截 | P0 | 未登录调 analyze 等 API | 401 |
| TC-SA-AUTH-03 | 三种凭据均可用 | P0 | Cookie / Header / 会话三途径 | 均放行 |
| TC-SA-AUTH-04 | 普通用户越权 | P0 | 非管理员调批量/导出 | 403 |
| TC-SA-AUTH-05 | 伪造/过期 token | P1 | 篡改签名/过期 token | 401 拒绝 |
| TC-SA-AUTH-06 | analyze 限流 429 | P1 | >30 次/60s | 429 |
| TC-SA-AUTH-07 | batch/run 限流 429 | P1 | >5 次/60s | 429 |
| TC-SA-AUTH-08 | export 限流 429 | P1 | >10 次/60s | 429 |

### 4.2 分析类型（ANALYZE，8 条）

| 编号 | 用例 | 优先级 | 步骤 | 预期 |
|---|---|---|---|---|
| TC-SA-ANALYZE-01 | technical 600519 | P0 | analyze technical | 结构完整，`data` 含 latest/ma/rsi/macd/signal/支撑阻力 |
| TC-SA-ANALYZE-02 | fundamental 600519 | P0 | analyze fundamental | scope/`basic` 字段完整；无 token 时无 `valuation` 键 |
| TC-SA-ANALYZE-03 | sentiment 600519 | P0 | analyze sentiment | method/news_count/samples≤5 |
| TC-SA-ANALYZE-04 | llm 链路 | P0 | analyze llm | 需 UnifiedLLM 上下文，返回结构完整 |
| TC-SA-ANALYZE-05 | llm 失败降级 | P1 | llm 异常 | 降级不 500，有明确 error |
| TC-SA-ANALYZE-06 | 指数 technical | P1 | analyze sh000001 | 指数前缀保留，结构完整（重建推断） |
| TC-SA-ANALYZE-07 | months 边界 | P2 | months=0/6/999 | 不报错；months 仅作用于 llm 路径 |
| TC-SA-ANALYZE-08 | 非法 type | P1 | type 非白名单 | HTTP 400 |

### 4.3 行情与市场（QUOTE，4 条）

| 编号 | 用例 | 优先级 | 步骤 | 预期 |
|---|---|---|---|---|
| TC-SA-QUOTE-01 | signal 600519 | P0 | signal 接口 | signal/confidence/reasons 与 technical 一致 |
| TC-SA-QUOTE-02 | market 三指数 | P0 | market_overview | available=3 failed=0，键名符合文档 |
| TC-SA-QUOTE-03 | sectors 能力未接入 | P2 | 调 sectors | 501 明确提示 |
| TC-SA-QUOTE-04 | 单指数失败容错 | P2 | 模拟单指数失败 | 部分失败不 500，返回可用指数 |

### 4.4 数据网关（GATEWAY，6 条）

| 编号 | 用例 | 优先级 | 步骤 | 预期 |
|---|---|---|---|---|
| TC-SA-GATEWAY-01 | 无 token K 线路由 | P1 | 无 token 取 K 线 | kline→sina、quote→tencent，链中无 tushare |
| TC-SA-GATEWAY-02 | fundamental-detail/moneyflow 无 token | P1 | 调两端点 | 404 降级文案 |
| TC-SA-GATEWAY-03 | 有 token fundamental | P1 | 配 token 调 fundamental | 四表结构可用 |
| TC-SA-GATEWAY-04 | moneyflow days clamp | P1 | days 越界 | 数据行数符合 clamp 规则 |
| TC-SA-GATEWAY-05 | 数据源失败冷却摘除 | P2 | 连败 3 次 | 1800s 冷却；**标的不存在不得触发冷却**（#SA-01 回归点） |
| TC-SA-GATEWAY-06 | 交叉校验 | P2 | K 线 vs quote | 健康标的零冲突；**陈旧数据必须被拒绝输出**（#SA-02 回归点） |

### 4.5 缓存（CACHE，4 条）

| 编号 | 用例 | 优先级 | 步骤 | 预期 |
|---|---|---|---|---|
| TC-SA-CACHE-01 | 进程内 K 线 TTL 300s | P2 | 连续两次取 K 线 | 二次命中缓存（加速），仍记合规来源 |
| TC-SA-CACHE-02 | 行情 TTL 60s | P2 | 连续两次取行情 | 二次命中缓存，available 不变 |
| TC-SA-CACHE-03 | 当日落盘缓存 | P2 | 检查 data/cache | 当日 gz 可正常解压 |
| TC-SA-CACHE-04 | 跨日缓存失效 | P2 | 跨日观察 | 非当日缓存不返回（需跨日观察） |

### 4.6 边界与容错（EDGE，8 条）

| 编号 | 用例 | 优先级 | 步骤 | 预期 |
|---|---|---|---|---|
| TC-SA-EDGE-01 | symbol 缺失 400 | P1 | 无 symbol | 400 |
| TC-SA-EDGE-02 | symbol 超长 400 | P1 | >12 字符 | 400 |
| TC-SA-EDGE-03 | 非法字符 400 | P1 | 含特殊/Unicode 字符（如"茅台"） | 400（`isascii()` 校验） |
| TC-SA-EDGE-04 | 不存在代码 999999 | P1 | analyze 999999 | 结构化错误，无 500、无异常抛出 |
| TC-SA-EDGE-05 | 北交所代码 | P1 | 430047/830799/871981 | 归一化正确；**陈旧数据不得输出**（#SA-02） |
| TC-SA-EDGE-06 | 带前缀符号 | P1 | sh600519/sz000001/sh000001 | 前缀保留，与纯数字等价，无双前缀/错前缀 |
| TC-SA-EDGE-07 | 停牌股 | P2 | 停牌标的 | 不 500，有明确提示 |
| TC-SA-EDGE-08 | 全源失败 | P2 | 模拟全部数据源故障 | 可理解降级文案，不 500 |

### 4.7 合规与 i18n（COMPLIANCE，4 条）

| 编号 | 用例 | 优先级 | 步骤 | 预期 |
|---|---|---|---|---|
| TC-SA-COMPLIANCE-01 | 输出结构合规 | P0 | 各类 analyze | disclaimer 恒非空；data_sources 每项含 source/authorized/fetched_at |
| TC-SA-COMPLIANCE-02 | 风险提示文本 | P1 | 三类分析报告 | technical/sentiment/fundamental 各自风险提示文案 |
| TC-SA-COMPLIANCE-03 | i18n 中英切换 | P1 | 切换语言 | 键集一致，无缺失 |
| TC-SA-COMPLIANCE-04 | 无凭据泄露 | P1 | 抓包/日志 | token 不出现在响应/日志/错误信息 |

### 4.8 Watchlist（WL，5 条，需服务器）

| 编号 | 用例 | 优先级 | 预期 |
|---|---|---|---|
| TC-SA-WL-01 | 添加自选 | P1 | 200 落库 |
| TC-SA-WL-02 | 重复添加 | P2 | 409 |
| TC-SA-WL-03 | 自选列表 | P1 | 列表完整 |
| TC-SA-WL-04 | 删除自选 | P1 | 200 生效 |
| TC-SA-WL-05 | 非法 kind 兜底 | P2 | 默认 technical，不 500 |

### 4.9 批量（BATCH，10 条，需服务器）

| 编号 | 用例 | 优先级 | 预期 |
|---|---|---|---|
| TC-SA-BATCH-01 | 批量运行 | P1 | 全部成功 |
| TC-SA-BATCH-02 | 批量幂等 | P1 | 重复 run_id 不重复落库 |
| TC-SA-BATCH-03 | 批量禁止 llm | P1 | llm 被白名单剔除 |
| TC-SA-BATCH-04 | 参数校验 | P1 | 非法参数 400 |
| TC-SA-BATCH-05 | 非交易日跳过 | P1 | is_trading_day=False 不执行 |
| TC-SA-BATCH-06 | 结果检索 | P1 | 按 run_id/symbol/signal 筛选 |
| TC-SA-BATCH-07 | 导出 | P1 | export JSON 全量不分页 |
| TC-SA-BATCH-08 | 空自选批量 | P2 | 明确提示，不 500 |
| TC-SA-BATCH-09 | 落库验证 | P1 | DB 行数与结果一致 |
| TC-SA-BATCH-10 | 日任务注册 | P1 | APScheduler 注册成功 |

---

## 5. 回归工具

| 工具 | 用途 |
|---|---|
| `tools/capture_baseline.py` | 基线录制（各类 analyze 快照） |
| `tools/compare_baseline.py` | 双模式对比：实时容差 ±0.5% / mock 回放严格；自动过滤 timestamp/fetched_at |

---

## 6. 基线管理

### 6.1 录制时机
- 版本发布前录制 `baseline_v<major>.<minor>0.json`；
- 基线文件存放于 `tests/fixtures/`。

### 6.2 基线录制纪律
- **fundamental/含估值分位类必须在配置 Tushare token 或服务器侧执行**，否则无 token 的
  scope=valuation_only 快照会污染基线（不同 token 权限产生不同输出，基线不可比）；
- 基线录制须在**交易日收盘后**进行（避免盘中数据波动导致对比噪声）；
- 不得以无 token 环境录制的输出充当有 token 基线；录制环境须记录 token/权限级别。

---

## 7. 执行纪律

### 7.1 进程隔离
- **异常/边界/失败类用例必须独立进程执行**，或在全部正常用例完成后执行；
- 判定「用例失败」前，须先检查 `gateway._cooldown_until` 排除冷却污染。

### 7.2 执行纪律（含 2026-08-30 回归暴露的规范缺口）
1. **冷却污染跨用例传播**：`gateway` 为模块级单例，连败 3 次触发 1800s 冷却，污染同进程后续
   所有 K 线用例（2026-08-30 EDGE-06 首轮因此误判失败）。**异常用例必须独立进程执行**。
2. **缓存掩盖故障**：进程内 TTL 与当日落盘缓存会让已分析标在冷却期仍返回成功。
   验证冷却/降级类用例必须使用**此前未分析过**的全新标的。
3. **数据新鲜度纳入常规断言**：`data_sources` 中 K 线数据点末行日期应在最近 N 个交易日内；
   出现 `crosscheck` 项时核对 `conflict.diff_pct` 并判定是否可接受（#SA-20260830-02 教训）。
4. **报错可读性**：断言错误信息对用户可理解，禁止以 500/异常抛出替代结构化错误。

### 7.3 交易日约束
- 批量/批次类用例仅在交易日执行（`data/calendar/2026.csv` 判定）；
- 周末/节假日执行时相关用例标注「阻塞-非交易日」。

---

## 8. 准出标准

### 8.1 通过率口径
- 全量口径 = 通过 / 用例总数；
- 已执行口径 = 通过 / 已执行用例数（排除阻塞项）。

### 8.2 准出门槛
| 门槛 | 要求 |
|---|---|
| P0 用例 | **100% 通过，且无未关闭 P0 缺陷** |
| P1 用例 | 通过率 ≥95% |
| 总通过率 | ≥90% |
| 基线对比 | `compare_baseline` 退出码 0 |
| llm 链路 | 在线验证可用 |
| 数据源降级 | 无 token 时按预期 404/降级，不 500 |
| 交付物 | 报告 + 基线 + 缺陷清单 + 证据齐备 |

### 8.3 一票否决条款
出现以下任一情形，无论其他指标如何，版本一律**不通过**：
1. P0 级数据错误缺陷未关闭（如陈旧/错值数据输出且用户无感知）；
2. P0 级安全漏洞（越权、凭据泄露）；
3. P0 级核心链路不可用（鉴权、分析主链路）。

---

*本规范重建版本 v1.5.1。生成：2026-08-31。*
