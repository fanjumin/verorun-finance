# providers/akshare_fundamental.py — 财报四表免费备源（新浪三表 + 东财财报摘要）
#
# 背景：tushare 财报类接口需 2000 积分（付费），120 积分账号调用必失败。
# 本 provider 用 akshare 免费源补齐 FUNDAMENTAL 类别，使财报页不依赖付费积分。
#
# 红线（方案 §3）：
#   - 只新增 provider，不删改 tushare 路径；优先级 tushare > 本源。
#   - 源没给的字段留空，不填假值。
#   - 避开 push2.eastmoney.com 系接口（本机不通）。
#
# 实测基线（2026-09-19，600519）：
#   stock_financial_report_sina 资产负债表 103 行 / 利润表 103 行
#   stock_financial_abstract   80 个指标 × 82 个报告期
import json
import time

from .base import DataCategory, ProviderError
from .base_v2 import BaseProviderV2, FetchResult

import logging

_log = logging.getLogger("stock_analysis.akshare_fundamental")

# 新浪科目名 → 内核/下游期望字段名（与 tushare 口径对齐，evidence.py 读这些键）
_INCOME_MAP = {
    "营业总收入": "revenue",
    "营业收入": "revenue_operating",
    "营业成本": "operate_cost",
    "营业利润": "operate_profit",
    "利润总额": "total_profit",
    "所得税费用": "income_tax",
    "净利润": "n_income",
    "归属于母公司所有者的净利润": "n_income_attrp",
    "少数股东损益": "minority_gain",
    "基本每股收益": "basic_eps",
    "稀释每股收益": "diluted_eps",
    "销售费用": "sell_exp",
    "管理费用": "admin_exp",
    "研发费用": "rd_exp",
    "财务费用": "fin_exp",
}
_BALANCE_MAP = {
    "资产总计": "total_assets",
    "负债合计": "total_liab",
    "所有者权益(或股东权益)合计": "total_equity",
    "归属于母公司股东权益合计": "total_hldr_eqy_exc_min_int",
    "流动资产合计": "total_cur_assets",
    "流动负债合计": "total_cur_liab",
    "货币资金": "money_cap",
    "存货": "inventories",
    "应收账款": "accounts_receiv",
    "应收票据及应收账款": "accounts_receiv_bn",
    "商誉": "goodwill",
    "短期借款": "st_borr",
    "长期借款": "lt_borr",
}
# 现金流量表科目：用「包含」匹配，规避不同年份表头细微差异
_CASHFLOW_MAP = [
    ("经营活动产生的现金流量净额", "n_cashflow_act"),
    ("投资活动产生的现金流量净额", "n_cashflow_inv"),
    ("筹资活动产生的现金流量净额", "n_cashflow_fin"),
    ("销售商品、提供劳务收到的现金", "c_fr_sale_sg"),
    ("经营活动现金流入小计", "c_inf_fr_operate_a"),
    ("经营活动现金流出小计", "c_paid_fr_operate_a"),
]


def _plain(symbol: str) -> str:
    """sh600519 → 600519"""
    s = (symbol or "").strip().lower()
    return s[2:] if len(s) > 6 and s[:2] in ("sh", "sz", "bj") else s


def _sina(symbol: str) -> str:
    """600519 / SH600519 → sh600519"""
    s = (symbol or "").strip().lower()
    if s[:2] in ("sh", "sz", "bj"):
        return s
    return ("sh" if s[0] == "6" else "sz") + s


def _norm_date(v) -> str:
    """新浪报告日 → YYYYMMDD（与 tushare end_date 同格式）。"""
    txt = str(v or "").strip()
    if txt.isdigit() and len(txt) == 8:
        return txt
    for sep in ("-", "/"):
        if sep in txt:
            return txt.split(" ")[0].replace(sep, "")
    return txt


class AkshareFundamentalProvider(BaseProviderV2):
    name = "akshare_fundamental"
    authorized = False
    market = "CN"
    categories = frozenset({DataCategory.FUNDAMENTAL})
    rate_per_min = 20
    burst = 2

    def _do_fetch(self, cat, *, symbol=None, **kw):
        if cat is not DataCategory.FUNDAMENTAL:
            raise NotImplementedError(cat)
        periods = int(kw.get("periods", 8) or 8)
        data = self._fetch_fundamental(symbol, periods=periods)
        if not data:
            raise ProviderError(self.name, "fundamental", "empty", retryable=False)
        return FetchResult(category=cat, data=data, source=self.name,
                           as_of=time.strftime("%Y-%m-%dT%H:%M:%S"))

    # ── 内部实现 ────────────────────────────────────────
    @staticmethod
    def _import_ak():
        try:
            import akshare as ak
            return ak
        except ImportError as err:
            raise ProviderError("akshare_fundamental", "fundamental",
                                f"akshare not installed: {err}", retryable=False)

    @staticmethod
    def _rows(df, mapping, periods, date_col="报告日"):
        """新浪宽表 → list[dict]，按报告日降序取 periods 期。"""
        if df is None or df.empty or date_col not in df.columns:
            return []
        df = df.copy()
        df[date_col] = df[date_col].map(_norm_date)
        df = df[df[date_col].str.len() == 8]
        df = df.sort_values(date_col, ascending=False).head(periods)
        out = []
        for _, row in df.iterrows():
            rec = {"end_date": str(row[date_col])}
            for cn, en in mapping.items():
                if cn in df.columns:
                    val = row[cn]
                    rec[en] = None if val is None or str(val) == "nan" else val
            out.append(rec)
        return out

    @staticmethod
    def _cashflow_rows(df, periods):
        if df is None or df.empty or "报告日" not in df.columns:
            return []
        df = df.copy()
        df["报告日"] = df["报告日"].map(_norm_date)
        df = df[df["报告日"].str.len() == 8]
        df = df.sort_values("报告日", ascending=False).head(periods)
        pairs = []
        for col in df.columns:
            for cn, en in _CASHFLOW_MAP:
                if cn == col or cn in col:
                    pairs.append((col, en))
                    break
        out = []
        for _, row in df.iterrows():
            rec = {"end_date": str(row["报告日"])}
            for col, en in pairs:
                val = row[col]
                rec[en] = None if val is None or str(val) == "nan" else val
            out.append(rec)
        return out

    @staticmethod
    def _abstract_rows(df, periods):
        """财报摘要（指标 × 期 宽表）→ list[dict]，期末在先。

        原表列：选项/指标/20260630/20260331/...（倒序）。
        转置为 [{end_date, 指标名: 值}, ...]，指标名保留中文原样（下游展示用）。
        """
        if df is None or df.empty or "指标" not in df.columns:
            return []
        periods_cols = [c for c in df.columns
                        if str(c).isdigit() and len(str(c)) == 8]
        periods_cols = sorted(periods_cols, reverse=True)[:periods]
        out = []
        for pc in periods_cols:
            rec = {"end_date": str(pc)}
            for _, row in df.iterrows():
                name = str(row.get("指标") or "").strip()
                if not name:
                    continue
                val = row.get(pc)
                rec[name] = None if val is None or str(val) == "nan" else val
            out.append(rec)
        return out

    def _fetch_fundamental(self, symbol: str, periods: int = 8) -> dict:
        ak = self._import_ak()
        sina_code = _sina(symbol)
        plain = _plain(symbol)
        out = {}
        # 三表：新浪（免费、无需积分）
        for key, sheet in (("balance", "资产负债表"),
                           ("income", "利润表"),
                           ("cashflow", "现金流量表")):
            try:
                df = ak.stock_financial_report_sina(stock=sina_code, symbol=sheet)
            except Exception as err:
                _log.warning("akshare %s failed for %s: %s", sheet, sina_code, err)
                out[key] = []
                continue
            if key == "cashflow":
                out[key] = self._cashflow_rows(df, periods)
            else:
                out[key] = self._rows(df, _BALANCE_MAP if key == "balance"
                                      else _INCOME_MAP, periods)
        # 金融/银行股没有「营业总收入」科目（实测 000001.SZ 该列为空），
        # 其主营收入口径是「营业收入」。这里只做同义字段回退，不编数值；
        # 两者都缺失时仍留 None（下游显示 '—'）。
        for rec in out.get("income") or []:
            if rec.get("revenue") is None:
                rec["revenue"] = rec.get("revenue_operating")
        # 指标：东财财报摘要（不走 push2，实测可用）
        try:
            ab = ak.stock_financial_abstract(symbol=plain)
        except Exception as err:
            _log.warning("akshare abstract failed for %s: %s", plain, err)
            ab = None
        out["fina_indicator"] = self._abstract_rows(ab, periods)

        if not any(out.get(k) for k in ("income", "balance", "cashflow")):
            raise ProviderError(self.name, "fundamental",
                                f"all sheets empty for {sina_code}", retryable=False)
        return json.loads(json.dumps(out, default=str))
