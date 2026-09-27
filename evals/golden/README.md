# golden/ — 黄金任务集

`tasks.json` 是 VeroRun 的**评测数据集**。当前覆盖 3 个数据集，全部锚定 A1 受污染的
提示词契约（它们恰是最缺回归保护的部分）。

## 字段语义

| 字段 | 说明 |
|---|---|
| `id` | 数据集唯一标识；`run_evals.py` 会校验唯一性与样例结构 |
| `kind` | `json_contract`（校验 JSON 输出契约的键）或 `prompt_enumeration`（校验枚举值被列举） |
| `snapshot` | 引用 `../prompt_snapshots/contracts.json` 里的 `key`；由它定位被测提示词 |
| `expect_keys` / `expect_absent_keys` | `json_contract` 专用：契约里必须出现 / 必须不出现的 JSON 键 |
| `expect_labels` | `prompt_enumeration` 专用：提示词必须列举的枚举值 |
| `samples[].input` | 送入模型的用户输入（**在线判分的输入**） |
| `samples[].expect_*` | 期望结果（**在线判分的评分锚点**）；离线不参与判分 |
| `samples[].rationale` | 该期望为何重要（防后来者误删样例） |

## 三个数据集

| id | 守护什么 | 为什么需要 |
|---|---|---|
| `self_review_json_contract` | 自评契约键 `confidence` / `issues` / `suggestion`，且**禁止** `confidence_` | A1 事故本体：键名漂移导致自评恒 0.85、重试闭环失效 |
| `intent_classification` | 意图 6 类 + 情绪 4 类枚举口径 | 枚举改名会让路由与统计**静默错配**，无异常、无日志 |
| `tool_routing_priority` | 工具枚举 10 项，以及 `ads` 优先级高于 `site_build` 的关系 | A1 在此处产生 6 处污染（其中 4 处为英文关键词被 `_()` 包裹），是污染最密集的一处 |

## 离线校验什么 / 不校验什么

**校验**（`python evals/run_evals.py --offline`，不调模型）：

- 数据集结构自洽：`id` 唯一、`samples` 非空、每例有 `input` 且至少一个 `expect*`；
- `snapshot` 引用有效；
- `expect_keys` / `expect_labels` 确实写在被引用的提示词契约里；
- `expect_absent_keys` 确实不在契约里。

**不校验**：模型对 `samples` 的真实输出是否符合 `expect_*`。
这需要 LLM 凭据，属后续工作——不要把这套离线检查当成"模型质量已验证"。

## 如何接入在线判分（后续）

1. 为 `run_evals.py` 增加在线模式（去掉 `--offline`，读取 LLM 凭据）；
2. 按 `kind` 分派评分器：
   - `json_contract` → 让被测 Agent 跑 `samples[].input`，解析输出的 JSON，比对键与阈值；
   - `prompt_enumeration` → 跑 `input`，断言返回的 tool / intent 等于 `expect_*`。
3. 建议在 CI 中保持**离线集合为必过门禁**，在线集合按运行成本择时执行（如 nightly），
   避免把不确定的模型调用塞进 PR 门禁。

## 新增样例的纪律

- 保持 `id` 唯一；每个样例必须能被**人工判断对错**，不要写"差不多就行"的期望；
- 期望值必须来自**产品口径**，而不是从当前实现反推——反推会把 bug 固化成基线
  （本仓 A1 的"残留污染被基线固化"正是这个失败模式）。
