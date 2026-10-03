# multi_asset — 金融版角色编排接入登记（R1 / R2 / R3）

本文件是**交付给发行版维护者的登记单**，不是本插件内部文档。
本插件的开发范围被限定为"只改 `plugins/multi_asset/`"，因此下面三项**未由插件自身修改**，
需要发行版维护者在 `deploy/editions/finance-desktop.yaml` 与
`agent_matrix/roles/finance-desktop/*.yaml` 中补齐。

## 背景：归属裁决怎么算

`plugin_manager/discovery.py` 加载插件时调用 `agent_matrix.models.resolve_agent_roles()`：

1. ① **归属声明**：`plugin.json` 的 `agent_role` 落在**本版核心角色集**内 → 命中；
2. ② **角色侧认领**：本版某核心角色的 `managed_modules` 含本插件 identifier → 命中；
3. 都不命中 → 记 `last_error`，`enable` 被拒绝。

本插件在 `plugin.json` 声明 `agent_role: "stock_analyst"`——这是**版本无关的默认归属**，
只在**官方版**成立（`agent_matrix/roles/11-stock_analyst.yaml` 的 `editions: [official]`）。

**在金融桌面版**上，核心角色集来自 `agent_matrix/roles/finance-desktop/*.yaml`
（`athena / ops / rs_planner / rs_fundamental / rs_quant / rs_risk / rs_pm / rs_compliance`），
**不含 `stock_analyst`** → ① 必然落空 → 必须走 ②。

> 结论：不改任何文件时，金融桌面版上 `multi_asset` 会以
> `missing/invalid agent_role` 落 `last_error`，插件不可启用。这不是缺陷，是发行版分工的登记缺口。

## R1 — 发包 include（必做）

`deploy/editions/finance-desktop.yaml` → `plugins.include` 追加：

```yaml
plugins:
  include:
    - _base
    - stock_analysis
    - im_gateway
    - email
    - memory_engine
    - vault
    - net_proxy
    - multi_asset        # ← 新增：不进 include 则不进安装包
```

说明：`include` 只做**发包裁剪**（stage 脚本按它物理拷贝目录），运行时可见性由
`compatible_editions: ["finance"]` + 分流规则决定（本插件已声明，无需额外规则）。

## R2 — 角色侧认领 managed_modules（必做）

在 `agent_matrix/roles/finance-desktop/` 下，把 `multi_asset` 追加到**应当承载多资产数据**
的角色 `managed_modules`。推荐最小集（2 个）：

| 角色文件 | 为什么 |
| --- | --- |
| `rs_quant.yaml` | 量价/数据面：期货、期权行情与合约规格的主消费者 |
| `rs_pm.yaml` | 组合决策：需要基金/债券的净值与估值序列做资产配置 |

```yaml
# agent_matrix/roles/finance-desktop/rs_quant.yaml
managed_modules:
  - stock_analysis
  - multi_asset      # ← 新增
```

```yaml
# agent_matrix/roles/finance-desktop/rs_pm.yaml
managed_modules:
  - stock_analysis
  - multi_asset      # ← 新增
```

可选（按业务口径决定，不强求）：

- `rs_risk.yaml`：若要基于期货/期权敞口做风险敞口与保证金校验；
- `rs_compliance.yaml`：若要把衍生品的 `risk_disclosure` / 适当性字段纳入合规留痕。

生效后 `attach_plugin_capabilities()` 会把本插件的 capabilities
（`asset.futures` / `asset.options` / `asset.funds` / `asset.bonds` / `asset.data_fetch` /
`asset.storage` / `asset.provenance`——后两项见文末 2026-10-01 补充）
幂等聚合到上述角色的 `capabilities` 列。

### 关于"是否需要新增一个专属角色"

**不需要，本版不建议新增。** 理由：

- 归属机制②按"功能区各自配一个角色"表达归属，本身就是为**避免为每个插件造角色**而设计的
  （见 `resolve_agent_roles` docstring）；新增 `rs_multi_asset` 会引入一个与
  `rs_quant` 职责高度重叠的角色，属于同域重复。
- 只有当多资产数据演化出**独立的决策产出**（例如"跨资产套利策略"成为一条独立流水线）时，
  才值得新增角色。届时把 `multi_asset` 从 `rs_quant` 的 `managed_modules` 移过去即可，
  插件侧无需改动（这正是把版本相关信息放在**角色侧**的意义）。

## R3 — 分流与兼容性核对（必做一次）

1. 本插件 `plugin.json` 已声明 `compatible_editions: ["finance"]`（§18.6.1 的 7 个 ID 之一），
   因此**默认对金融版可见**。若发行版另建了 `plugin_distribution_rules` 条目，
   须确认其 `editions` 含 `finance`，否则会被 `not_in_rule_editions` 隐藏。
2. 商店/Registry 侧若维护"本版插件清单"快照，需同步加入 `multi_asset`，否则管理台可能
   显示为未登记。
3. `python scripts/i18n_check.py` 需在合入后重跑（本插件 i18n 键集一致，已自检通过）。

## 依赖与加载顺序

`plugin.json` 声明 `dependencies: {"stock_analysis": ">=2.0.1"}`，平台按依赖序加载
（plugin-standard-v1.8 §10.4），保证 `plugins.stock_analysis.providers.base_v2` 可导入。
若 `stock_analysis` 缺失或版本过低，本插件仍能加载，但 `CONTRACT_AVAILABLE=False`：
所有 provider 抛 `ProviderUnavailable`，健康检查明确报告数据源不可用。

## 复核清单（维护者自检）

- [ ] `plugins.include` 含 `multi_asset`
- [ ] `rs_quant.yaml` / `rs_pm.yaml` 的 `managed_modules` 含 `multi_asset`
- [ ] `multi_asset` 出现在本版插件注册表/清单快照中
- [ ] 重跑 `scripts/i18n_check.py` 无新增违规
- [ ] 启用后日志无 `missing/invalid agent_role`、无 `在本版无归属角色`

---

## 2026-10-01 补充：定位升级后的变化

本插件已从"非股票资产数据"升级定位为**资产覆盖 + 数据治理底座**
（`plugin.json` 的 description、`README.md` 定位句、`SKILL.md` 边界三处已同步）。
对发行版维护者的**实际影响只有一处**：

**capabilities 由 5 项增至 7 项**，新增的两项都对应**既有实现**，不是虚报：

```yaml
capabilities:
  - asset.futures
  - asset.options
  - asset.funds
  - asset.bonds
  - asset.data_fetch
  - asset.storage        # ma_bars 落库与查询（models.upsert_bars / get_bars）
  - asset.provenance     # 取数留痕（models.record_fetch / ma_fetch_log）
```

`attach_plugin_capabilities()` 是幂等聚合，R2 的 `managed_modules` 补齐后会自动带上新能力，
**维护者无需额外动作**。

同时提醒一条**不要做的事**：本插件**刻意没有声明** `data.ingest` / `data.pit` /
`data.quality` / `data.classification`。它目前只建了治理**表结构**（6 张空表），没有任何
读写实现；声明这些能力会让清单与实现不符。待治理实现（P1–P4）落地后再补声明，
届时需要在 `plugin.json` 与 `__init__.py` 的 `CAPABILITIES` 两处同步。

### 仍未解决：治理层的角色归属（Q6）

`plugin.json` 的 `agent_role` **仍是 `stock_analyst`，本次未改**。原因是治理能力
（许可台账、数据集注册、质量报告、出域控制）在业务上属**风控 / 合规域**，而
`agent_role` 是**单值字段**，与取数归属不是同一域。本插件**不为这一条做决定**。

三个可选方向，供维护者按发行版口径取舍：

| 方向 | 做法 | 代价 |
|---|---|---|
| 保持现状 | `agent_role` 维持 `stock_analyst`，治理能力随 R2 一并归到 `rs_quant` / `rs_pm` | 治理语义弱化：数据治理挂在量化角色下，与风控/合规角色的职责边界不清 |
| 增加认领 | R2 时把 `multi_asset` 追加到 `rs_risk.yaml` 与 `rs_compliance.yaml` 的 `managed_modules` | 同一插件被 4 个角色认领；机制上允许，语义上需确认 |
| 拆分归属 | 待治理能力（P1–P4）真正实现后，再决定是否把治理部分拆为独立插件/角色 | 最贴合职责边界，但要等实现落地 |

**本插件建议方向一（维持现状），直到 P1–P4 落地。** 理由：`agent_role` 是单值、版本无关的
归属声明；治理功能当前尚不存在（只有空表），此时为它调整角色归属，调整的是一个还没有内容的边界。
