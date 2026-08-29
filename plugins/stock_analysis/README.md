# VeroRun 股票分析插件（stock_analysis）

> **当前版本**: v1.1.0

面向 VeroRun 的 A 股研究插件，为**管理员**与**金融分析 Agent** 提供技术面、估值、情绪面及 AI 综合研判能力。插件通过 VeroRun 标准生命周期加载，不绑定内核具体模型，不保存交易订单或用户资产数据。

> **重要声明**：本插件输出的是**研究信息与风险提示，不是自动交易指令**。所有分析结果受数据延迟、停牌、缺失指标与模型误判影响，仅供研究参考，**不构成投资建议或收益承诺**。

> 当前 VeroRun 没有社区服务，因此本插件**不包含**社区客户端、社区 API、社区密钥、发帖、投票或经验包上传功能。

---

## 一、核心能力

- **技术面分析**：均线（MA5/20/60）、RSI、MACD、支撑位/阻力位及技术信号
- **估值分析**：基于实时行情字段的 PE(TTM)、PB 与估值评分（`scope=valuation_only`，不含完整财报）
- **情绪面分析**：新浪财经新闻标题关键词统计（`method=keyword_counting`），附近期新闻示例便于核验
- **AI 综合研判**：经 VeroRun `UnifiedLLM` 网关（standard tier 模型策略）综合分析，Agent 系统提示词强制生效
- **市场概况**：上证指数、深证成指、创业板指实时点位与涨跌幅
- **管理后台内嵌页面**：`/admin/stock-analysis/`，与系统主题同步
- **专业分析 Agent**：`stock_analysis_agent`，供金融分析 Agent 编排调用

---

## 二、快速开始

### 目录结构

```text
plugins/stock_analysis/
├── __init__.py                  # 插件入口：插件类 + Agent 声明
├── plugin.json                  # manifest（版本/菜单/权限/Agent/配置 Schema）
├── routes.py                    # 管理端页面与 4 个 API 路由（含 admin 鉴权）
├── stock_skill.py               # 自包含分析引擎（行情抓取/指标/缓存/LLM 编排）
├── config.yaml                  # 插件本地配置文件
├── setup.sh                     # 独立 venv 安装脚本（仅供独立运行）
├── requirements.txt             # 插件自身依赖声明
├── agents/
│   └── stock_analysis_agent_prompt.md   # Agent 系统提示词（含合规护栏）
├── templates/
│   └── stock_analysis.html      # iframe 管理页面（i18n + 主题同步）
├── i18n/
│   ├── en.yml                   # 英文词条（key=英文源串）
│   └── zh-CN.yml                # 中文词条（与 en 键集一致）
├── SKILL.md                     # Hermes/OpenClaw Skill 接口说明
└── README.md                    # 本文件
```

### 安装

将整个目录复制到 VeroRun 的插件目录，确保目录名为 `stock_analysis`：

```text
verorun/plugins/stock_analysis/
```

插件根目录必须包含 `plugin.json`、`__init__.py`、`routes.py` 与 `agents/`。安装后由 VeroRun PluginManager 读取 manifest，并在启用时注册管理路由与 Agent。

### 运行环境

- Python 3.11+
- VeroRun 0.59.3+（manifest `min_app_version` 校验）
- **pandas >= 2.0**：已加入主项目 `requirements.txt` 与 `requirements.lock`，部署时随系统依赖一并安装（无需手动处理插件 venv）
- 行情数据源（Sina / Tencent Finance）的网络访问权限

---

## 三、配置

插件配置存在**两个通道**，读取时以 PluginManager 持久化配置为准、文件配置兜底：

| 配置项 | 类型 | 说明 |
| --- | --- | --- |
| `DATA_PROVIDER` | string | 行情数据提供方，当前仅实现 `sina`（settings_schema 枚举约束） |
| `DATA_CACHE_DIR` | string | 行情缓存目录，预留文件级缓存（当前实现为进程内 TTL 缓存） |

- 本地文件：`config.yaml`
- 持久化配置：管理后台插件设置页写入（经 `settings_schema` 校验）

**模型策略**：模型选择、API Key、配额、缓存与用量审计全部由 VeroRun `UnifiedLLM` 内核负责。插件 manifest 使用 `standard` tier，不直接强制指定 provider、model 或密钥。插件通过 `get_agent_by_slug("stock_analysis_agent")` + `resolve_model_args` 获取模型解析结果，并将 Agent 系统提示词显式前置为 system 消息，确保合规护栏实际生效。

---

## 四、管理界面与 API

### 页面入口

```text
/admin/stock-analysis/
```

所有页面与 API 均要求 VeroRun 管理员身份（`sso_token` cookie / `Authorization` Bearer / `X-Token`，校验 `is_admin` 声明）。

### API 端点

| 方法 | 路径 | 用途 | 备注 |
| --- | --- | --- | --- |
| GET | `/admin/stock-analysis/api/analyze?symbol=600519&type=llm` | 个股分析 | `type` ∈ `technical`/`fundamental`/`sentiment`/`llm`；`months` 1-36（默认 6） |
| GET | `/admin/stock-analysis/api/signal?symbol=600519` | 快速获取技术信号 | 基于技术面 |
| GET | `/admin/stock-analysis/api/market` | 市场概况 | 三大指数点位与涨跌幅 |
| GET | `/admin/stock-analysis/api/sectors?top_n=10` | 行业排名 | 能力未接入时返回 `501` + `success:false` |

- `symbol` 校验：仅允许字母/数字/点，长度 ≤ 12，且自动归一化为 `sh`/`sz`/`bj` 前缀（含北交所 `43/83/87/88/920` 段识别）
- 分析结果统一结构：`{ "success": bool, "result": { symbol, timestamp, signal, report, json_data } }`

---

## 五、数据源与缓存

| 数据 | 来源 | 说明 |
| --- | --- | --- |
| 历史日线行情 | Sina Finance KLine API | OHLCV，`scale=240` 日线 |
| 实时行情 | Tencent Finance（qt.gtimg.cn） | 现价、涨跌幅、PE(TTM)、换手率、每股净资产 |
| 新闻标题 | Sina Finance 个股新闻页 | 情绪关键词统计 |
| 指数行情 | Tencent Finance | 上证/深成/创业板 |

**进程内 TTL 缓存**（gunicorn 各 worker 独立，管理端研究场景足够）：

| 缓存域 | 键 | TTL |
| --- | --- | --- |
| 历史 K 线 | `kline:{symbol}:{datalen}` | 300s |
| 实时行情 | `quote:{symbol}` | 60s |
| 新闻页面 | `news:{symbol}` | 600s |

- 空数据（K 线无返回）不缓存，保留重试能力
- 所有 HTTP 请求均设置超时（行情 5s / K 线与新闻 10s），且仅访问硬编码公网域名

---

## 六、合规与风险披露

插件在各分析路径内置风险披露，明确不构成投资建议：

- **技术面**：报告末尾附「风险提示: 技术指标不构成投资建议」
- **估值分析**：报告注明「数据范围: 实时行情估值字段，未包含完整财报」+「风险提示: 本分析仅基于实时估值字段」；`json_data` 含 `scope="valuation_only"` 与 `disclaimer` 字段
- **情绪面**：报告注明「方法: 基于新浪财经新闻标题关键词统计，可能存在误判」+「风险提示: 情绪面不构成投资建议」，并附最多 3 条近期新闻示例；`json_data` 含 `method`、`samples`（最多 5 条）
- **LLM 综合**：Agent 系统提示词强制前置，包含「不虚构价格/指标/新闻/持仓、中性措辞、注明数据来源与时间戳、失败即报告」等金融护栏
- 信号中的 `confidence` 为启发式强度值，语义为「信号强度」而非统计置信度，请勿据此重仓决策

---

## 七、安全边界

- **全路由 admin 鉴权**：页面与 4 个 API 均校验 `is_admin`，未授权返回 401
- **符号白名单**：`symbol` 仅接受字母/数字/点、≤12 字符，杜绝注入与任意输入
- **无 SSRF**：所有外呼 URL 硬编码为公网数据源，用户输入仅作为查询参数
- **无危险执行**：无 `subprocess`/`os.system`/`eval`/`pickle`，无硬编码凭据
- **数据隔离**：插件不创建数据库 schema、不持久化业务数据（无 `models.py`），无独立连接池
- **i18n**：UI 词条全部经 `_()` 查表，`i18n/{en,zh-CN}.yml` 键集一致，缺失键回退英文源串

---

## 八、Python API / CLI

### Python API

```python
from stock_skill import StockAnalysisSkill

skill = StockAnalysisSkill()

# 全维度分析
result = skill.analyze("600519", analysis_type="llm", months=6)
print(result.to_text())

# 快速技术信号
signal = skill.get_signal("600519")
print(signal)
```

### CLI（独立运行）

```bash
# 分析单只股票
python stock_skill.py 600519

# 查看帮助
python stock_skill.py --help
```

---

## 九、部署注意事项

1. **依赖**：pandas 已随主项目 `requirements.lock` 安装；部署时确保 admin 服务环境包含 pandas，否则插件路由将静默不挂载
2. **i18n 播种**：新增/修改 `i18n/*.yml` 词条后，admin 服务重启时由 PluginManager 自动幂等播种到 i18n 库
3. **验证建议**：使用真实管理员账号登录后，实测 `/admin/stock-analysis/` 页面及各 API 端点（技术面/估值/情绪/LLM/市场/信号）

---

*本插件为研究工具，数据与模型输出均可能出错；任何交易决策由使用者自行承担风险。*
