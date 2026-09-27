# evals/ — 离线评测与提示词资产纳管（WP-B6）

本目录是 VeroRun 的**评测基座**。它存在的原因是三类真实事故：

| 事故 | 症状 | 本目录如何拦住 |
|---|---|---|
| **A1 提示词污染** | i18n 自动化把 `_()` 写进发给 LLM 的提示词与 JSON 输出契约；`confidence_` 与读取端 `confidence` 不匹配 → 自评置信度恒为 0.85 → `<0.7` 重试闭环**静默失效** | `prompt_snapshots/` 契约快照 |
| **A1 残留被基线固化** | 同类污染未清尽，反而被"当前命中集"生成的基线当成了合法余量，门禁从此不再报警 | `claims.json` 原文子串断言 |
| **修复被静默改回** | 已修好的执行层白名单 / 危险节点守卫 / 选主逻辑被后续改动无意回退 | `claims.json` 原文子串断言 |

## 目录结构

```
evals/
├── run_evals.py                 # 门禁主程序（仅标准库；不连库、不装运行时依赖）
├── claims.json                  # 【人工维护】锚点断言：file + must_contain / must_not_contain
├── prompt_snapshots/
│   ├── contracts.json           # 【人工维护】提示词契约：key / file / anchor / 契约子串
│   ├── index.json               # 【机器生成】字面量指纹（sha256 / 字符数 / 行号）
│   └── <key>.txt                # 【机器生成】规范化后的提示词全文，供人工 diff
└── golden/
    ├── README.md
    └── tasks.json               # 黄金任务集（3 个数据集 + 样例）
```

## 用法

```bash
# 门禁模式（CI 用）——任一失败 exit 1，并打印可读定位
python evals/run_evals.py --offline

# 查看通过项明细
python evals/run_evals.py --offline -v

# 提示词确需变更：人工评审后刷新指纹
python evals/run_evals.py --update-snapshots
git diff evals/prompt_snapshots/        # 复核差异后再提交
```

> Windows 下如遇编码报错，先 `export PYTHONIOENCODING=utf-8`。

## 提示词字面量如何被提取（口径说明）

本仓的提示词**大多内联在 `.py` 源码里**（不是独立的 `.md`），因此快照用 `ast` 提取
"最外层字符串表达式"的**字面量骨架**：

- 相邻字符串隐式拼接 → 合并为一个整体；
- f-string 的插值段 → 统一替换为 `{*}`（所以**插值位置/数量**变化也可被检出）；
- 转义花括号 `{{` / `}}` → 按运行时语义展开为 `{` / `}`。

骨架是**规范化**结果，与引号风格、换行缩进、拼接方式无关，只随提示词语义变化；
且该实现基于 AST 与源码文本，不依赖 `tokenize` 对 f-string 的版本差异，
**Python 3.11 / 3.12 / 3.13 结果一致**（CI 用 3.11）。

`{*}` 的存在意味着一件事：**被注入的内容本身不在快照范围内**（例如 `task_decompose`
首部的 `{*}` 是 `master_prompt` 注入位）。注入内容的完整性由 WP-B6.3 的签名清单负责。

**跨版本一致性已实测**：快速门禁在 CI 用的 **Python 3.11.15** 下校验由 3.13 生成的快照，
字符数与行号逐项一致（`PASS=30 / FAIL=0`）。这一点很重要——因为 3.11 与 3.12+ 的
f-string 解析器不同（PEP 701），若快照口径随版本漂移，CI 就会误红。

**漂移时的诊断**：判红会直接打印「已提交快照 → 当前字面量」的统一 diff（截断到 16 行），
因此可以立刻分辨是「有人改了提示词」还是「提取口径变化」，而不是只丢一个哈希。

## 与 i18n 门禁的分工（重要）

`scripts/i18n_check.py` 的检查 6 拦的是**字面量里出现 `_(`**。这只能覆盖一类污染：

| 污染形态 | i18n 门禁 | 本目录快照 |
|---|---|---|
| `"title": _("Title 1")` | ✅ 拦得住 | ✅ 也拦（`must_not_contain: "_("`） |
| 键名被改名 `confidence` → `confidence_`（**不含 `_(`**） | ❌ 拦不住 | ✅ 拦得住（契约子串 + 指纹） |
| 提示词被大段改写 / 删除 | ❌ 拦不住 | ✅ 拦得住（锚点丢失 / 指纹不符） |

**A1 事故的真实根因属于第二行**，这正是本目录必须存在、而不是与 i18n 门禁重复的理由。

补充：`evals/` 已加入 `scripts/i18n_check.py` 的 `PROMPT_LITERAL_SKIP_DIRS`
（原为 `{'tools','scripts'}`）。原因是本目录的 `run_evals.py` **必须能复述它守护的模式**
（即 `_(` 本身），否则门禁会因"文档里引用了被禁模式"而自我误红 ——
与 `scripts/`、`tools/` 被排除同理。

## 离线覆盖边界（不夸大）

**离线模式不调用任何模型。** 具体：

- `claims.json` → 静态文本断言，覆盖"修复点是否还在"；
- `prompt_snapshots/` → 覆盖"提示词契约是否被漂移"；
- `golden/tasks.json` → 只校验数据集自洽 + "期望的枚举/JSON 键确实写在提示词契约里"。

**不覆盖**：模型的真实输出质量、路由准确率、置信度打分是否合理。
这些需要 LLM 凭据，属后续工作（`run_evals.py` 中已预留 `--offline` 开关位，
不带该开关时会明确提示"在线评测尚未实现"，不会假装跑过）。

## 提示词纳管的两个阶段（WP-B6.3）

`veroguard/tools/build_manifest.py` 的扫描范围已增加 `agent_matrix/prompts/*.md`，
配合 `tests.yml` 的 "veroguard manifest freshness" 门禁形成第二道防线：

1. **本次改动生效后**：`evals/` 的契约快照立即生效（不需要任何私钥）。
2. **下次签名清单刷新后**（CI 的 "Refresh VeroGuard Manifest" 或打 tag 时触发，
   需 `RELEASE_SIGN_KEY`）：`agent_matrix/prompts/*.md` 被纳入签名清单，
   此后改动提示词而未刷新清单 → **CI 直接失败**。

要回退第 2 项：删除 `build_manifest.py` 中 `_discover_core_prompt_files(...)` 的调用
（一行），并重刷清单即可。

**注意**：清单一旦包含提示词，客户端 `veroguard` 的完整性校验会把本地修改过的提示词
报为违规（`severity: high`，非 critical）。这是**有意的**——提示词属交付资产；
但它同时意味着"允许客户自定义提示词"这类形态需要另行决策。本项已记入审计报告，
由产品口径决定是否长期保留。

## 已知限制 / 后续工作

- **`B1.2_admin_auth_dual_contract` 断言为 pending**：`shared/admin_auth.py` 属 WP-B1，
  尚未落地。该断言在文件缺失时记 SKIP（不判红），文件一旦创建即**自动接管为硬断言**。
- 插件提示词 `plugins/*/agents/*.md` **未纳入**签名清单（口径 A）；其中 veroscholar
  的三对提示词已由 `tests.yml` 的"双源一致性"步骤守护。
- 在线评测（模型真实输出判分）尚未实现。
