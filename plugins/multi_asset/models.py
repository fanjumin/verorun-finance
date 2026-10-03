"""multi_asset 插件独立 schema 数据层。

对齐 models_sa.py 的既有模式（审查 P1-5 建议的推荐路径）：
  * 统一经 plugins._base.db.get_pooled_connection() 借池（禁自建连接工厂）；
  * DDL 内联在本文件（不引入仓库里不存在的 SQL 迁移基建 / 迁移 runner）；
  * 建表即 SELECT 验证；成功后进程内记忆（_tables_ready），失败不记忆、下个请求重试；
  * 全部 DDL 幂等（CREATE ... IF NOT EXISTS / ALTER ... ADD COLUMN IF NOT EXISTS），
    不使用 PostgreSQL 的 CREATE TYPE AS ENUM（无 IF NOT EXISTS，重跑必 duplicate_object）。
    资产类型用 VARCHAR + CHECK 约束表达，等价语义且天然幂等。

表命名纪律（审查 V-07：全文只允许一套表名）：
  * 时间序列统一进 ma_bars（(asset_type, symbol, exchange, freq, trade_date) 为主键）；
  * 标的参考/规格数据进各资产参考表（ma_fund_ref / ma_bond_ref /
    ma_future_contracts / ma_option_contracts / ma_option_greeks）；
  * 取数留痕进 ma_fetch_log（合规/溯源）；
  * 治理骨架表（ma_license / ma_dataset_registry / ma_pit_fundamentals /
    ma_pit_consensus / ma_index_membership / ma_quality_report）本轮只建结构，
    不含读写实现——它们是「不可逆窗口」的结构预留，先定表结构，避免日后迁移。

数据字典：ma_bars.trade_date 归属口径
  期货夜盘（21:00 起的连续交易时段）成交归属**次一交易日**；日盘归属当日。
  该口径由 trade_calendar.assign_trade_date() 统一计算，展示层不得自行推断，
  否则期货 K 线与基金净值序列会出现错日（审查 V-14）。基金净值归属净值公布日；
  债券/股票归属交易所交易日。
"""

import json
import logging
import threading
from contextlib import contextmanager

from plugins._base.db import get_pooled_connection

SCHEMA = "multi_asset"

_log = logging.getLogger("multi_asset.models")

# 资产类型白名单（对齐 asset_symbol.AssetType，避免 SQL 侧枚举类型无法幂等创建）
ASSET_TYPES = ("EQUITY", "BOND", "FUND", "FUTURE", "OPTION", "SPOT", "SWAP")
_BAR_FREQS = ("daily", "weekly", "monthly", "60m", "30m", "15m", "5m", "1m")

# 建表成功后进程内记忆（P2-12 同款），失败不记忆以便重试；并发首调用由锁串行化。
_tables_ready = False
_ready_report: dict = {}
_ensure_lock = threading.Lock()

TABLES = (
    "ma_bars",
    "ma_fund_ref",
    "ma_bond_ref",
    "ma_future_contracts",
    "ma_option_contracts",
    "ma_option_greeks",
    "ma_fetch_log",
    # ── 治理骨架（仅为结构预留，本轮不含任何读写实现）──────────────────
    "ma_license",
    "ma_dataset_registry",
    "ma_pit_fundamentals",
    "ma_pit_consensus",
    "ma_index_membership",
    "ma_quality_report",
)

_ASSET_TYPE_CHECK = "asset_type IN (%s)" % ", ".join("'%s'" % a for a in ASSET_TYPES)

DDL = [
    # ── 时间序列（本系统第一张真正落库的行情/净值序列表；无同形契约可继承）──
    """
    CREATE TABLE IF NOT EXISTS ma_bars (
        id           BIGSERIAL PRIMARY KEY,
        asset_type   VARCHAR(8)   NOT NULL CHECK (%s),
        symbol       VARCHAR(16)  NOT NULL,
        exchange     VARCHAR(8)   NOT NULL,
        freq         VARCHAR(8)   NOT NULL DEFAULT 'daily',
        trade_date   DATE         NOT NULL,
        bar_time     TIME         NOT NULL DEFAULT '00:00',
        open         NUMERIC(20, 6),
        high         NUMERIC(20, 6),
        low          NUMERIC(20, 6),
        close        NUMERIC(20, 6),
        volume       NUMERIC(24, 4),
        amount       NUMERIC(24, 4),
        value        NUMERIC(20, 6),
        source       VARCHAR(32),
        updated_at   TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_bars UNIQUE (asset_type, symbol, exchange, freq, trade_date, bar_time)
    )""" % _ASSET_TYPE_CHECK,
    """CREATE INDEX IF NOT EXISTS ix_ma_bars_lookup
       ON ma_bars (asset_type, symbol, exchange, freq, trade_date DESC)""",
    # ── 基金参考（ETF/LOF/开放式）──
    """
    CREATE TABLE IF NOT EXISTS ma_fund_ref (
        id           BIGSERIAL PRIMARY KEY,
        code         VARCHAR(16)  NOT NULL,
        exchange     VARCHAR(8)   NOT NULL,
        name         VARCHAR(64),
        name_norm    VARCHAR(64),
        fund_type    VARCHAR(16),
        company      VARCHAR(64),
        manager      VARCHAR(64),
        nav_date     DATE,
        nav          NUMERIC(20, 6),
        acc_nav      NUMERIC(20, 6),
        source       VARCHAR(32),
        updated_at   TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_fund_ref UNIQUE (code, exchange)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_ma_fund_ref_name ON ma_fund_ref (name_norm)""",
    # ── 债券参考（国债/金融债/企业债/可转债）──
    """
    CREATE TABLE IF NOT EXISTS ma_bond_ref (
        id            BIGSERIAL PRIMARY KEY,
        code          VARCHAR(16)  NOT NULL,
        exchange      VARCHAR(8)   NOT NULL,
        name          VARCHAR(64),
        name_norm     VARCHAR(64),
        bond_type     VARCHAR(16),
        coupon_rate   NUMERIC(10, 4),
        issue_date    DATE,
        maturity_date DATE,
        issuer        VARCHAR(64),
        credit_rating VARCHAR(16),
        convert_price NUMERIC(20, 6),
        source        VARCHAR(32),
        updated_at    TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_bond_ref UNIQUE (code, exchange)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_ma_bond_ref_name ON ma_bond_ref (name_norm)""",
    # ── 期货合约规格（含主力/连续映射）──
    """
    CREATE TABLE IF NOT EXISTS ma_future_contracts (
        id              BIGSERIAL PRIMARY KEY,
        symbol          VARCHAR(16)  NOT NULL,
        exchange        VARCHAR(8)   NOT NULL,
        product         VARCHAR(8)   NOT NULL,
        delivery_year   INT,
        delivery_month  INT,
        multiplier      NUMERIC(20, 6) DEFAULT 1,
        tick_size       NUMERIC(20, 6),
        last_trade_date DATE,
        is_main         SMALLINT     DEFAULT 0,
        continuous_of   VARCHAR(16),
        source          VARCHAR(32),
        updated_at      TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_future_contracts UNIQUE (symbol, exchange)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_ma_future_product
       ON ma_future_contracts (exchange, product, delivery_year, delivery_month)""",
    # ── 期权合约规格（对齐 CFI='O'）──
    """
    CREATE TABLE IF NOT EXISTS ma_option_contracts (
        id           BIGSERIAL PRIMARY KEY,
        symbol       VARCHAR(24)  NOT NULL,
        exchange     VARCHAR(8)   NOT NULL,
        underlying   VARCHAR(16),
        option_type  CHAR(1)      CHECK (option_type IN ('C', 'P')),
        strike       NUMERIC(20, 6),
        expiry_date  DATE,
        multiplier   NUMERIC(20, 6) DEFAULT 1,
        source       VARCHAR(32),
        updated_at   TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_option_contracts UNIQUE (symbol, exchange)
    )""",
    # ── 期权希腊值（衍生计算层产出的落库快照）──
    """
    CREATE TABLE IF NOT EXISTS ma_option_greeks (
        id           BIGSERIAL PRIMARY KEY,
        symbol       VARCHAR(24)  NOT NULL,
        exchange     VARCHAR(8)   NOT NULL,
        trade_date   DATE         NOT NULL,
        implied_vol  NUMERIC(12, 6),
        delta        NUMERIC(12, 6),
        gamma        NUMERIC(12, 6),
        vega         NUMERIC(12, 6),
        theta        NUMERIC(12, 6),
        rho          NUMERIC(12, 6),
        source       VARCHAR(32),
        computed_at  TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_option_greeks UNIQUE (symbol, exchange, trade_date)
    )""",
    # ── 取数留痕（合规：每条数据点可溯源）──
    """
    CREATE TABLE IF NOT EXISTS ma_fetch_log (
        id            BIGSERIAL PRIMARY KEY,
        asset_type    VARCHAR(8),
        symbol        VARCHAR(24),
        exchange      VARCHAR(8),
        freq          VARCHAR(8),
        source        VARCHAR(32),
        provenance_id VARCHAR(64),
        as_of         VARCHAR(32),
        delay_seconds INT          DEFAULT 0,
        ok            SMALLINT     DEFAULT 1,
        warning       TEXT,
        created_at    TIMESTAMPTZ  DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS ix_ma_fetch_log_time
       ON ma_fetch_log (created_at DESC)""",

    # ══ R 段：不可逆结构预留（2026-10-01）══════════════════════════════════
    # 范围：只做结构与元数据，不含任何 接入 / 校验 / 出域 / 查询 实现。
    # 窗口期理由：ma_bars 尚无真实数据时加列是纯结构变更；一旦免费源日更积累了
    # 数据，来源与许可就失去可回填的依据（只能猜，或全标最严）。
    # 追加在列表末尾的原因：ensure_tables() 顺序执行 DDL，放在最后可保证存量库
    # 升级时所有基础表先就位、再统一补列，不依赖列表中间的插入位置。
    #
    # ── R1：ma_bars 数据治理标签列 ──
    # 依据 GB/T 42775-2023「从外部获取的数据不得低于提供方所定级别」：用户导入的
    # 授权数据在库内仍受原许可约束，来源与许可范围必须随行继承。
    # 与既有 source 列的分工：source = 取数通道名（akshare / sina）；
    # origin = 来源性质（exchange / legal_disclosure / public_feed / vendor /
    # user_file）。逐列 ADD COLUMN IF NOT EXISTS 幂等：列已存在时整条子句跳过，
    # 内联 CHECK 一并跳过，故重复执行不会撞约束名。
    """
    ALTER TABLE ma_bars
        ADD COLUMN IF NOT EXISTS license_id     VARCHAR(64),
        ADD COLUMN IF NOT EXISTS security_level SMALLINT
            CHECK (security_level IS NULL OR security_level BETWEEN 1 AND 4),
        ADD COLUMN IF NOT EXISTS origin         VARCHAR(32),
        ADD COLUMN IF NOT EXISTS dataset_id     VARCHAR(64),
        ADD COLUMN IF NOT EXISTS ingested_at    TIMESTAMPTZ DEFAULT now()
    """,
    # ── R2：许可台账 ──
    # 无台账则「许可范围」无处声明，出域判定无从谈起。allow_* 默认全 0 = 默认最严：
    # 导入当下无法验证用户许可，事后也无法自证，故默认从严。
    """
    CREATE TABLE IF NOT EXISTS ma_license (
        id            BIGSERIAL PRIMARY KEY,
        license_id    VARCHAR(64)  NOT NULL,
        provider      VARCHAR(128),
        scope         TEXT,
        allow_export  SMALLINT     DEFAULT 0,
        allow_forward SMALLINT     DEFAULT 0,
        allow_llm     SMALLINT     DEFAULT 0,
        valid_from    DATE,
        valid_until   DATE,
        note          TEXT,
        created_at    TIMESTAMPTZ  DEFAULT now(),
        updated_at    TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_license UNIQUE (license_id)
    )""",
    # ── R3：数据集注册 ──
    # 水位线 / 来源 / 级别 / 许可需要一个归属体，否则这些标签只能散落在每行数据上。
    """
    CREATE TABLE IF NOT EXISTS ma_dataset_registry (
        id             BIGSERIAL PRIMARY KEY,
        dataset_id     VARCHAR(64)  NOT NULL,
        name           VARCHAR(128),
        asset_types    VARCHAR(128),
        origin         VARCHAR(32),
        provider       VARCHAR(128),
        license_id     VARCHAR(64),
        security_level SMALLINT,
        watermark      VARCHAR(64),
        row_count      BIGINT       DEFAULT 0,
        note           TEXT,
        created_at     TIMESTAMPTZ  DEFAULT now(),
        updated_at     TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_dataset_registry UNIQUE (dataset_id)
    )""",
    # ── R4：PIT 双时态骨架（财报 / 一致预期 / 指数成分）──
    # valid_from / system_from / system_to 构成「业务时间 × 系统时间」双轴：
    # as-of 查询要问的是「在某历史时点，我们所知的数据长什么样」，单时间轴回答不了。
    # exchange 用 NOT NULL DEFAULT '' 而非可空：它参与唯一键，NULL 不参与唯一性判定。
    """
    CREATE TABLE IF NOT EXISTS ma_pit_fundamentals (
        id             BIGSERIAL PRIMARY KEY,
        symbol         VARCHAR(16)  NOT NULL,
        exchange       VARCHAR(8)   NOT NULL DEFAULT '',
        report_period  DATE         NOT NULL,
        -- A3-a：三表区分（balance/income/cashflow + 派生指标命名空间），
        -- 否则三张报表的同名字段（如 net_profit / total_assets）会撞唯一键。
        statement_type VARCHAR(16)  NOT NULL DEFAULT '',
        metric         VARCHAR(64)  NOT NULL,
        value          NUMERIC(24, 6),
        unit           VARCHAR(16),
        valid_from     TIMESTAMPTZ  NOT NULL,
        -- A4：知识时间必须来自发布事实（§5.3.3 关键设计点 3），无入库默认值，
        -- 写入漏传时直接报错（fail-loud），不得用入库时间静默顶替。
        system_from    TIMESTAMPTZ  NOT NULL,
        system_to      TIMESTAMPTZ,
        revision_of    BIGINT,
        source         VARCHAR(32),
        origin         VARCHAR(32),
        dataset_id     VARCHAR(64),
        license_id     VARCHAR(64),
        security_level SMALLINT,
        created_at     TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_pit_fundamentals
            UNIQUE (symbol, exchange, report_period, statement_type, metric,
                    valid_from, system_from)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_ma_pit_fund_symbol
       ON ma_pit_fundamentals (symbol, report_period, metric)""",
    """
    CREATE TABLE IF NOT EXISTS ma_pit_consensus (
        id              BIGSERIAL PRIMARY KEY,
        symbol          VARCHAR(16)  NOT NULL,
        exchange        VARCHAR(8)   NOT NULL DEFAULT '',
        forecast_period VARCHAR(16)  NOT NULL,
        metric          VARCHAR(64)  NOT NULL,
        value           NUMERIC(24, 6),
        unit            VARCHAR(16),
        analyst_count   INT,
        valid_from      TIMESTAMPTZ  NOT NULL,
        -- A4：知识时间必须来自快照日期等发布事实，无入库默认值。
        system_from     TIMESTAMPTZ  NOT NULL,
        system_to       TIMESTAMPTZ,
        source          VARCHAR(32),
        origin          VARCHAR(32),
        dataset_id      VARCHAR(64),
        license_id      VARCHAR(64),
        security_level  SMALLINT,
        created_at      TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_pit_consensus
            UNIQUE (symbol, exchange, forecast_period, metric, valid_from, system_from)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_ma_pit_consensus_symbol
       ON ma_pit_consensus (symbol, forecast_period, metric)""",
    """
    CREATE TABLE IF NOT EXISTS ma_index_membership (
        id              BIGSERIAL PRIMARY KEY,
        index_code      VARCHAR(24)  NOT NULL,
        index_exchange  VARCHAR(8)   NOT NULL DEFAULT '',
        symbol          VARCHAR(16)  NOT NULL,
        symbol_exchange VARCHAR(8)   NOT NULL DEFAULT '',
        weight          NUMERIC(12, 6),
        valid_from      DATE         NOT NULL,
        valid_to        DATE,
        -- A3-b(i)：本表按设计只做 valid 轴（消除幸存者偏差只依赖
        -- valid_from/valid_to）；system_from 仅为入库存证，同样不取入库默认值。
        system_from     TIMESTAMPTZ  NOT NULL,
        source          VARCHAR(32),
        origin          VARCHAR(32),
        dataset_id      VARCHAR(64),
        license_id      VARCHAR(64),
        security_level  SMALLINT,
        created_at      TIMESTAMPTZ  DEFAULT now(),
        CONSTRAINT uq_ma_index_membership
            UNIQUE (index_code, symbol, valid_from)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_ma_index_membership_index
       ON ma_index_membership (index_code, valid_from, valid_to)""",
    # ── R5：质量报告落点 ──
    # 校验结果必须有地方写，否则规则集（P2）跑完只能丢进日志。append-only 流水，
    # 故不设唯一约束，只建按数据集 + 时间的检索索引。
    """
    CREATE TABLE IF NOT EXISTS ma_quality_report (
        id          BIGSERIAL PRIMARY KEY,
        dataset_id  VARCHAR(64),
        asset_type  VARCHAR(8),
        symbol      VARCHAR(16),
        rule_code   VARCHAR(48)  NOT NULL,
        severity    VARCHAR(8)   NOT NULL DEFAULT 'warn'
            CHECK (severity IN ('info', 'warn', 'block')),
        status      VARCHAR(8)   NOT NULL DEFAULT 'fail'
            CHECK (status IN ('pass', 'fail', 'skip')),
        detail      TEXT,
        sample      TEXT,
        trade_date  DATE,
        checked_at  TIMESTAMPTZ  DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS ix_ma_quality_report_dataset
       ON ma_quality_report (dataset_id, checked_at DESC)""",

    # ══ R 段补丁（2026-10-01）：收口 R 段评审发现的结构缺口 A1–A4 ════════
    # 全部为幂等升级语句，新库（上面的 CREATE 已是新形态）与存量库都安全。
    #
    # ── A1：ma_license 补「提供方所定级别」──
    # GB/T 42775-2023 §8.4：外部获取的数据级别不得低于提供方所定级别。
    # 没有左值就无法做比较；与消费侧自定级（行上的 security_level）分离。
    """
    ALTER TABLE ma_license
        ADD COLUMN IF NOT EXISTS level_as_provided SMALLINT
            CHECK (level_as_provided IS NULL OR level_as_provided BETWEEN 1 AND 4)
    """,
    # ── A3-a：存量库财报表补三表区分列（新库 CREATE 已含）──
    """
    ALTER TABLE ma_pit_fundamentals
        ADD COLUMN IF NOT EXISTS statement_type VARCHAR(16) NOT NULL DEFAULT ''
    """,
    # ── A3-a：唯一键迁移。仅当现存唯一键不含 statement_type 时才重建；
    # 新形态已在则整段零操作（避免每次 ensure_tables 都重建大表约束）。
    """
    DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conname = 'uq_ma_pit_fundamentals'
          AND NOT EXISTS (
            SELECT 1 FROM unnest(c.conkey) AS k(attnum)
            JOIN pg_attribute AS a
              ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE a.attname = 'statement_type'
          )
      ) THEN
        ALTER TABLE ma_pit_fundamentals DROP CONSTRAINT uq_ma_pit_fundamentals;
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_ma_pit_fundamentals'
      ) THEN
        ALTER TABLE ma_pit_fundamentals
          ADD CONSTRAINT uq_ma_pit_fundamentals
          UNIQUE (symbol, exchange, report_period, statement_type, metric,
                  valid_from, system_from);
      END IF;
    END $$;
    """,
    # ── A2：4 张治理表裸 security_level 补 1..4 CHECK（R 段仅 ma_bars 有）──
    # PG 没有 ADD CONSTRAINT IF NOT EXISTS，用固定约束名 + pg_constraint
    # 判存在实现幂等；骨架表无数据，不存在存量行校验成本。
    """
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                     WHERE conname = 'ck_ma_dataset_registry_level') THEN
        ALTER TABLE ma_dataset_registry
          ADD CONSTRAINT ck_ma_dataset_registry_level
          CHECK (security_level IS NULL OR security_level BETWEEN 1 AND 4);
      END IF;
    END $$;
    """,
    """
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                     WHERE conname = 'ck_ma_pit_fundamentals_level') THEN
        ALTER TABLE ma_pit_fundamentals
          ADD CONSTRAINT ck_ma_pit_fundamentals_level
          CHECK (security_level IS NULL OR security_level BETWEEN 1 AND 4);
      END IF;
    END $$;
    """,
    """
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                     WHERE conname = 'ck_ma_pit_consensus_level') THEN
        ALTER TABLE ma_pit_consensus
          ADD CONSTRAINT ck_ma_pit_consensus_level
          CHECK (security_level IS NULL OR security_level BETWEEN 1 AND 4);
      END IF;
    END $$;
    """,
    """
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint
                     WHERE conname = 'ck_ma_index_membership_level') THEN
        ALTER TABLE ma_index_membership
          ADD CONSTRAINT ck_ma_index_membership_level
          CHECK (security_level IS NULL OR security_level BETWEEN 1 AND 4);
      END IF;
    END $$;
    """,
    # ── A4：三张 PIT 表去掉 system_from 的入库时间默认值 ──
    # DROP DEFAULT 在列本无默认值时仅产生 NOTICE、不报错，天然幂等。
    """
    ALTER TABLE ma_pit_fundamentals ALTER COLUMN system_from DROP DEFAULT
    """,
    """
    ALTER TABLE ma_pit_consensus ALTER COLUMN system_from DROP DEFAULT
    """,
    """
    ALTER TABLE ma_index_membership ALTER COLUMN system_from DROP DEFAULT
    """,
]


@contextmanager
def get_db():
    """借池连接并切到插件 schema；with 块退出自动 commit/rollback + 归还池。"""
    with get_pooled_connection() as conn:
        conn.execute("SET search_path TO %s, public" % SCHEMA)
        yield conn


def ensure_tables():
    """建表 + 逐表 SELECT 验证；成功进程内记忆，失败 raise 由调用方降级。"""
    global _tables_ready, _ready_report
    if _tables_ready:
        return _ready_report
    with _ensure_lock:
        if _tables_ready:
            return _ready_report
        report = {}
        with get_db() as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS %s" % SCHEMA)
            for ddl in DDL:
                conn.execute(ddl)
            for table in TABLES:
                conn.execute("SELECT 1 FROM %s LIMIT 1" % table).fetchone()
                report[table] = True
        _tables_ready = True
        _ready_report = report
    _log.info("multi_asset ensure_tables OK: %s", sorted(report))
    return report


def drop_schema():
    """卸载清理：一次性连接，用完即关（借池例外条款）。"""
    from plugins._base.db import get_raw_connection
    conn = get_raw_connection()
    try:
        cur = conn.cursor()
        cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ── ma_bars 读写 ────────────────────────────────────────────────────────────

def _as_float(v):
    """Decimal/str -> float；None 原样返回（PG 数值列接受 None）。"""
    if v is None or isinstance(v, float):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v):
    """str -> int（CSV 读入的等级常是字符串）；None 原样，坏值转 None（未评估）。

    真正的越界值（如 9）以 int 进入时由表上的 CHECK 约束拒绝（fail-loud）。
    """
    if v is None or isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# tag_dataset_rows() 的「保持原列不动」哨兵：区别于显式传 None（=有意清空）。
_TAG_KEEP = object()


def upsert_bars(rows) -> int:
    """批量写行情/净值序列（幂等：唯一键冲突则更新）。

    rows: 可迭代的 dict，键至少含 asset_type/symbol/exchange/freq/trade_date；
    可选 bar_time/open/high/low/close/volume/amount/value/source。
    返回写入行数。
    """
    normalized = []
    for r in rows or []:
        if not r:
            continue
        normalized.append((
            str(r.get("asset_type") or "").upper(),
            str(r.get("symbol") or "").strip(),
            str(r.get("exchange") or "").upper(),
            str(r.get("freq") or "daily"),
            r.get("trade_date"),
            r.get("bar_time") or "00:00",
            _as_float(r.get("open")), _as_float(r.get("high")),
            _as_float(r.get("low")), _as_float(r.get("close")),
            _as_float(r.get("volume")), _as_float(r.get("amount")),
            _as_float(r.get("value")), r.get("source"),
            # A5：治理标签随行写入（免费源喂数时这 5 项为 None）。
            r.get("license_id"), _as_int(r.get("security_level")),
            r.get("origin"), r.get("dataset_id"), r.get("ingested_at"),
        ))
    if not normalized:
        return 0
    cols = ("asset_type, symbol, exchange, freq, trade_date, bar_time, "
            "open, high, low, close, volume, amount, value, source, "
            "license_id, security_level, origin, dataset_id, ingested_at")
    # 18 个 ? 对应前 18 列；ingested_at 缺省落 now()。
    row_ph = "(" + ",".join(["?"] * 18) + ",COALESCE(?, now()))"
    total = 0
    with get_db() as conn:
        for start in range(0, len(normalized), 500):
            chunk = normalized[start:start + 500]
            placeholders = ",".join([row_ph] * len(chunk))
            params = []
            for row in chunk:
                params.extend(row)
            cur = conn.execute(
                "INSERT INTO ma_bars (%s) VALUES %s "
                "ON CONFLICT (asset_type, symbol, exchange, freq, trade_date, bar_time) "
                "DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, "
                "low = EXCLUDED.low, close = EXCLUDED.close, "
                "volume = EXCLUDED.volume, amount = EXCLUDED.amount, "
                "value = EXCLUDED.value, source = EXCLUDED.source, "
                # A5：行情列随刷新覆盖；治理标签只在「新值非空」时补写，
                # 免费源无标签重刷不得抹掉后挂的许可/等级（COALESCE 保标签）。
                "license_id = COALESCE(EXCLUDED.license_id, ma_bars.license_id), "
                "security_level = COALESCE(EXCLUDED.security_level, ma_bars.security_level), "
                "origin = COALESCE(EXCLUDED.origin, ma_bars.origin), "
                "dataset_id = COALESCE(EXCLUDED.dataset_id, ma_bars.dataset_id), "
                # 首次入库时间不可变：冲突时保留原值。
                "ingested_at = ma_bars.ingested_at, "
                "updated_at = now()" % (cols, placeholders), params)
            # Report what the server actually wrote (inserts + conflict updates)
            # instead of the batch size, so the count is not an overstatement.
            affected = getattr(cur, "rowcount", None)
            total += affected if isinstance(affected, int) and affected >= 0 \
                else len(chunk)
    return total


def get_bars(asset_type: str, symbol: str, exchange: str = None, freq: str = "daily",
             start=None, end=None, limit: int = 500) -> list:
    """读 K 线/净值序列（按 trade_date, bar_time 升序）。"""
    sql = ("SELECT asset_type, symbol, exchange, freq, "
           "to_char(trade_date, 'YYYY-MM-DD') AS trade_date, "
           "to_char(bar_time, 'HH24:MI') AS bar_time, "
           "open, high, low, close, volume, amount, value, source, "
           # A5：每行必须答得出许可/等级/溯源/数据集/入库时间（§6.2）。
           "license_id, security_level, origin, dataset_id, "
           "to_char(ingested_at, 'YYYY-MM-DD HH24:MI:SS') AS ingested_at "
           "FROM ma_bars WHERE asset_type = ? AND symbol = ? AND freq = ?")
    params = [str(asset_type).upper(), str(symbol).strip(), str(freq or "daily")]
    if exchange:
        sql += " AND exchange = ?"
        params.append(str(exchange).upper())
    if start:
        sql += " AND trade_date >= ?"
        params.append(start)
    if end:
        sql += " AND trade_date <= ?"
        params.append(end)
    sql += " ORDER BY trade_date ASC, bar_time ASC LIMIT ?"
    params.append(max(1, min(int(limit or 500), 5000)))
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def tag_dataset_rows(*, dataset_id, license_id=_TAG_KEEP, security_level=_TAG_KEEP,
                     origin=_TAG_KEEP, asset_type=None, symbols=None,
                     start=None, end=None, all_rows=False) -> int:
    """为「已落库但无标签」的行情行批量后挂治理标签（A5）。

    真实顺序常是：免费源先入库 → 用户事后注册数据集/许可 → 按范围回填标签。
    upsert_bars 的冲突更新只能「保标签」，标签的首次补写走本函数。

    语义约定：
    - dataset_id 必写；license_id/security_level/origin 省略（默认哨兵）时
      保持原列不动，显式传 None 才是有意清空——部分更新不会把标签抹成 NULL。
    - 作用域必须显式给出（asset_type / symbols / start / end 至少一个），
      或 all_rows=True；禁止无条件全表更新。
    - 全部筛选值走 ? 参数化，不接受任何 SQL 片段拼接。
    返回受影响行数。
    """
    sets = ["dataset_id = ?"]
    params = [dataset_id]
    if license_id is not _TAG_KEEP:
        sets.append("license_id = ?")
        params.append(license_id)
    if security_level is not _TAG_KEEP:
        sets.append("security_level = ?")
        params.append(_as_int(security_level))
    if origin is not _TAG_KEEP:
        sets.append("origin = ?")
        params.append(origin)

    where = []
    if asset_type:
        where.append("asset_type = ?")
        params.append(str(asset_type).upper())
    if symbols:
        syms = [str(s).strip() for s in symbols if str(s).strip()]
        if syms:
            where.append("symbol IN (%s)" % ",".join(["?"] * len(syms)))
            params.extend(syms)
    if start:
        where.append("trade_date >= ?")
        params.append(start)
    if end:
        where.append("trade_date <= ?")
        params.append(end)
    if not where and not all_rows:
        # 无作用域的全表改标签属于高风险操作，必须显式 all_rows=True。
        raise ValueError("tag_dataset_rows requires a scope (asset_type/"
                         "symbols/start/end) or all_rows=True")

    sql = "UPDATE ma_bars SET " + ", ".join(sets)
    if where:
        sql += " WHERE " + " AND ".join(where)
    with get_db() as conn:
        cur = conn.execute(sql, params)
        affected = getattr(cur, "rowcount", None)
        return affected if isinstance(affected, int) and affected >= 0 else 0


# ── 参考表 upsert ───────────────────────────────────────────────────────────

def _upsert(table: str, key_cols, cols, rows) -> int:
    """通用批量 upsert（幂等：唯一键冲突则更新其余列）。"""
    rows = [r for r in (rows or []) if r]
    if not rows:
        return 0
    all_cols = list(key_cols) + [c for c in cols if c not in key_cols]
    update_cols = [c for c in all_cols if c not in key_cols]
    placeholders = ",".join(["(" + ",".join(["?"] * len(all_cols)) + ")"] * len(rows))
    params = []
    for row in rows:
        params.extend(row.get(c) for c in all_cols)
    if update_cols:
        conflict_clause = "DO UPDATE SET " + ", ".join(
            "%s = EXCLUDED.%s" % (c, c) for c in update_cols)
    else:
        conflict_clause = "DO NOTHING"
    sql = ("INSERT INTO %s (%s) VALUES %s ON CONFLICT (%s) %s"
           % (table, ", ".join(all_cols), placeholders, ", ".join(key_cols),
              conflict_clause))
    with get_db() as conn:
        conn.execute(sql, params)
    return len(rows)


def upsert_fund_ref(rows) -> int:
    return _upsert("ma_fund_ref", ("code", "exchange"),
                   ("name", "name_norm", "fund_type", "company", "manager",
                    "nav_date", "nav", "acc_nav", "source"), rows)


def upsert_bond_ref(rows) -> int:
    return _upsert("ma_bond_ref", ("code", "exchange"),
                   ("name", "name_norm", "bond_type", "coupon_rate", "issue_date",
                    "maturity_date", "issuer", "credit_rating", "convert_price",
                    "source"), rows)


def upsert_future_contracts(rows) -> int:
    return _upsert("ma_future_contracts", ("symbol", "exchange"),
                   ("product", "delivery_year", "delivery_month", "multiplier",
                    "tick_size", "last_trade_date", "is_main", "continuous_of",
                    "source"), rows)


def upsert_option_contracts(rows) -> int:
    return _upsert("ma_option_contracts", ("symbol", "exchange"),
                   ("underlying", "option_type", "strike", "expiry_date",
                    "multiplier", "source"), rows)


def upsert_option_greeks(rows) -> int:
    return _upsert("ma_option_greeks", ("symbol", "exchange", "trade_date"),
                   ("implied_vol", "delta", "gamma", "vega", "theta", "rho",
                    "source"), rows)


def record_fetch(asset_type: str, symbol: str, exchange: str = None,
                 freq: str = None, source: str = None, provenance_id: str = None,
                 as_of: str = None, delay_seconds: int = 0, ok: bool = True,
                 warning: str = None) -> None:
    """取数留痕（旁路：调用方应容忍失败，不得影响主链路）。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ma_fetch_log (asset_type, symbol, exchange, freq, source, "
            "provenance_id, as_of, delay_seconds, ok, warning) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(asset_type or "").upper() or None, symbol, exchange, freq, source,
             provenance_id, as_of, int(delay_seconds or 0), 1 if ok else 0, warning))


def list_fetch_log(limit: int = 50) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT asset_type, symbol, exchange, freq, source, provenance_id, "
            "as_of, delay_seconds, ok, warning, "
            "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
            "FROM ma_fetch_log ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit or 50), 500)),)).fetchall()
        return [dict(r) for r in rows]


# ── 标的检索（插件自辖，审查 P2-6 的插件侧兜底）────────────────────────────

def search_ref_instruments(query: str, asset_type: str = None, limit: int = 10) -> list:
    """在参考表内按 代码/名称 检索（基金/债券/期货合约）。"""
    q = str(query or "").strip()
    if not q:
        return []
    prefix = q.upper() + "%"
    contains = "%" + q + "%"
    limit = max(1, min(int(limit or 10), 50))
    out = []
    with get_db() as conn:
        if asset_type in (None, "FUND"):
            rows = conn.execute(
                "SELECT code, exchange, name FROM ma_fund_ref "
                "WHERE code LIKE ? OR name_norm LIKE ? ORDER BY code LIMIT ?",
                (prefix, contains, limit)).fetchall()
            out.extend({"asset_type": "FUND", "code": r["code"],
                        "exchange": r["exchange"], "name": r["name"]} for r in rows)
        if asset_type in (None, "BOND"):
            rows = conn.execute(
                "SELECT code, exchange, name FROM ma_bond_ref "
                "WHERE code LIKE ? OR name_norm LIKE ? ORDER BY code LIMIT ?",
                (prefix, contains, limit)).fetchall()
            out.extend({"asset_type": "BOND", "code": r["code"],
                        "exchange": r["exchange"], "name": r["name"]} for r in rows)
        if asset_type in (None, "FUTURE"):
            rows = conn.execute(
                "SELECT symbol, exchange, product FROM ma_future_contracts "
                "WHERE symbol LIKE ? ORDER BY symbol LIMIT ?",
                (prefix, limit)).fetchall()
            out.extend({"asset_type": "FUTURE", "code": r["symbol"],
                        "exchange": r["exchange"], "name": r["product"]} for r in rows)
    return out[:limit]


# ── 健康检查用 ──────────────────────────────────────────────────────────────

def storage_stats() -> dict:
    """表规模概览，供 health check / 管理页展示。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM ma_bars) AS bars, "
            "(SELECT COUNT(*) FROM ma_future_contracts) AS futures, "
            "(SELECT COUNT(*) FROM ma_fund_ref) AS funds, "
            "(SELECT COUNT(*) FROM ma_bond_ref) AS bonds, "
            "(SELECT COUNT(*) FROM ma_fetch_log) AS logs").fetchone()
    return {k: int(row[k] or 0) for k in ("bars", "futures", "funds", "bonds", "logs")}
