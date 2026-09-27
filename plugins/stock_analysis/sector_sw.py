# sector_sw.py — 申万（SW）行业分类免费源（akshare，无需任何 key）
#
# 背景：行业分类原本只能走 Tushare `index_classify` + `index_member`（需 2000 积分），
# 120 积分账号必失败 —— `sa_classification` 恒空 → `/api/sectors` 恒 404 →
# 总览页「板块热力」永久空态。本模块用 akshare 免费接口补齐申万一级 31 个行业。
#
# ★ 实测基线（2026-09-19，akshare 1.18.94，本机）：
#   sw_index_first_info()            31 行 2.2s
#     列：行业代码('801010.SI') / 行业名称 / 成份个数 / 静态市盈率 /
#         TTM(滚动)市盈率 / 市净率 / 静态股息率
#   index_component_sw(symbol=裸码)  0.8s/行业，31 行业合计 **10.5s**、5218 条、
#     去重后证券数同为 5218 ⇒ 申万一级行业互斥（一只票只属一个一级行业）
#     列：序号 / 证券代码 / 证券名称 / 最新权重 / 计入日期(date)
#     ★ 必须传**裸码** '801010'：带 '.SI' 后缀会 KeyError（源内部按裸码取数）。
#   ★ 申万**指数**行情无免费源：sw_index_daily / sw_index_quote 在该版本不存在；
#     index_zh_a_hist('801010') 走东财 push2，**本机代理不通**（ProxyError）。
#     新浪指数快照 stock_zh_index_spot_sina 含 562 个指数但**不含**申万 801xxx。
#     ⇒ 行业涨跌幅只能由成分股行情聚合（见 routes.sectors），不能指望指数直取。
#
# 红线（沿用 S1/S3）：
#   - 源没给的字段留空，不填假值；取不到就明确报错，不用 mock 顶替。
#   - 确定性失败（akshare 缺失接口 / 列名变了）抛 SectorSourceUnavailable
#     且 retryable=False —— 与 akshare_macro 同款理由：不能让"用户切换上下文"
#     把本源误冷却。
from __future__ import annotations

import logging
import time
from datetime import date

_log = logging.getLogger("stock_analysis.sector_sw")

STANDARD = "sw"                 # 与 classification._STANDARDS["SW2021"] 同值
SOURCE = "akshare_sw"

# 行业清单缓存：估值每日变 ⇒ 当日有效（gateway._disk_get 默认分支）
_KEY_INFO = "sector:sw:info"
# 成分股缓存：申万调整半年一次 ⇒ 7 天足够（避免每次灌库付 10s 网络）
_KEY_MEMBERS = "sector:sw:members:{code}"
_MEMBER_TTL_DAYS = 7


class SectorSourceUnavailable(RuntimeError):
    """本源确定性不可用（接口缺失/列名变了/非中国口径）。"""

    retryable = False


def _ak():
    try:
        import akshare as ak
        return ak
    except ImportError as err:                                # pragma: no cover
        raise SectorSourceUnavailable(f"akshare not installed: {err}") from err


def _disk():
    """延迟导入 gateway：避免本模块被 routes 早期 import 时的循环依赖。"""
    from . import gateway
    return gateway


def _to_float(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f                          # NaN → None
    except (TypeError, ValueError):
        return None


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10] if s else None


# ── 取数 ────────────────────────────────────────────────────────────────
def industry_info(use_cache: bool = True) -> list[dict]:
    """申万一级行业清单 + 官方估值。无缓存时一次网络约 2.2s。

    返回 [{code, name, constituents, pe_ttm, pb, dividend_yield}]，按行业代码升序。
    """
    gw = _disk()
    if use_cache:
        hit = gw._disk_get(_KEY_INFO)
        if hit:
            return list(hit[0])

    ak = _ak()
    fn = getattr(ak, "sw_index_first_info", None)
    if fn is None:
        raise SectorSourceUnavailable("akshare missing sw_index_first_info")
    try:
        raw = fn()
    except Exception as err:                                   # noqa: BLE001
        raise SectorSourceUnavailable(
            f"sw_index_first_info failed: {type(err).__name__}: {err}") from err
    if raw is None or raw.empty:
        raise SectorSourceUnavailable("sw_index_first_info returned empty")

    need = ("行业代码", "行业名称")
    for col in need:
        if col not in raw.columns:
            raise SectorSourceUnavailable(
                f"sw_index_first_info column '{col}' missing; got {list(raw.columns)[:8]}")

    rows = []
    for _, r in raw.iterrows():
        code = str(r.get("行业代码") or "").strip().replace(".SI", "")
        name = str(r.get("行业名称") or "").strip()
        if not code or not name:
            continue
        rows.append({
            "code": code,
            "name": name,
            "constituents": int(_to_float(r.get("成份个数")) or 0),
            "pe_ttm": _to_float(r.get("TTM(滚动)市盈率")),
            "pb": _to_float(r.get("市净率")),
            "dividend_yield": _to_float(r.get("静态股息率")),
        })
    rows.sort(key=lambda x: x["code"])
    if not rows:
        raise SectorSourceUnavailable("no usable industry rows")

    gw._disk_put(_KEY_INFO, rows, SOURCE)
    _log.info("sector_sw industry_info: %d rows", len(rows))
    return rows


def members(code: str, use_cache: bool = True) -> list[dict]:
    """某申万一级行业成分股。无缓存时一次网络约 0.8s。

    返回 [{symbol(CN:xxxxxx), code, name, weight, in_date}]，按权重降序。
    """
    code = str(code).strip().replace(".SI", "")
    gw = _disk()
    key = _KEY_MEMBERS.format(code=code)
    if use_cache:
        hit = gw._disk_get(key, max_age_days=_MEMBER_TTL_DAYS)
        if hit:
            return list(hit[0])

    ak = _ak()
    fn = getattr(ak, "index_component_sw", None)
    if fn is None:
        raise SectorSourceUnavailable("akshare missing index_component_sw")
    try:
        raw = fn(symbol=code)                                  # ★ 裸码，带 .SI 会 KeyError
    except Exception as err:                                   # noqa: BLE001
        raise SectorSourceUnavailable(
            f"index_component_sw({code}) failed: {type(err).__name__}: {err}") from err
    if raw is None or raw.empty:
        raise SectorSourceUnavailable(f"index_component_sw({code}) returned empty")
    if "证券代码" not in raw.columns:
        raise SectorSourceUnavailable(
            f"index_component_sw column '证券代码' missing; got {list(raw.columns)[:8]}")

    out = []
    for _, r in raw.iterrows():
        sec = str(r.get("证券代码") or "").strip()
        if not sec:
            continue
        out.append({
            "symbol": f"CN:{sec}",
            "code": sec,
            "name": str(r.get("证券名称") or "").strip() or None,
            "weight": _to_float(r.get("最新权重")),
            "in_date": _to_date(r.get("计入日期")),
        })
    if not out:
        raise SectorSourceUnavailable(f"no usable members for {code}")
    out.sort(key=lambda x: (x["weight"] is None, -(x["weight"] or 0)))
    gw._disk_put(key, out, SOURCE)
    return out


# 全市场快照缓存：当日有效（gw._disk_get 默认分支按 day 判新鲜）
_KEY_SPOT = "sector:cn:spot"
# 新浪全量快照实测 36.5s / 5564 行（2026-09-19）。一次性代价换来**全成分股**精确聚合；
# 命中当日缓存后为毫秒级，故策略是"当日只付一次"，而不是每次采样几百只票。
_SPOT_FN = "stock_zh_a_spot"


def market_snapshot(use_cache: bool = True) -> dict:
    """A 股全市场实时快照 → {6位代码: {"name","price","change_pct","amount"}}。

    用新浪 `stock_zh_a_spot`（东财 `stock_zh_a_spot_em` 走 push2，**本机代理不通**）。
    实测 5564 行 36.5s，列：代码('sh600519'/'bj920000') / 名称 / 最新价 /
    涨跌额 / 涨跌幅(百分数) / 昨收 / … / 成交额 / 时间戳。

    ★ 一次拿全市场，行业涨跌幅才能按**全部成分股**聚合（而不是采样几只），
      这是"申万指数没有免费行情源"前提下最接近指数真实涨跌的做法。
    """
    gw = _disk()
    if use_cache:
        hit = gw._disk_get(_KEY_SPOT)
        if hit:
            return dict(hit[0])

    ak = _ak()
    fn = getattr(ak, _SPOT_FN, None)
    if fn is None:
        raise SectorSourceUnavailable(f"akshare missing {_SPOT_FN}")
    t0 = time.time()
    try:
        raw = fn()
    except Exception as err:                                   # noqa: BLE001
        raise SectorSourceUnavailable(
            f"{_SPOT_FN} failed: {type(err).__name__}: {err}") from err
    if raw is None or raw.empty or "代码" not in raw.columns:
        raise SectorSourceUnavailable(
            f"{_SPOT_FN} bad frame: cols={list(getattr(raw, 'columns', []))[:8]}")

    out: dict = {}
    for _, r in raw.iterrows():
        full = str(r.get("代码") or "").strip().lower()
        code = full[2:] if len(full) > 6 else full             # 'sh600519' → '600519'
        if not code:
            continue
        pct = _to_float(r.get("涨跌幅"))
        if pct is None:
            continue
        out[code] = {
            "name": str(r.get("名称") or "").strip() or None,
            "price": _to_float(r.get("最新价")),
            "change_pct": pct,
            "amount": _to_float(r.get("成交额")),
        }
    if not out:
        raise SectorSourceUnavailable("no usable snapshot rows")
    _log.info("sector_sw market_snapshot: %d rows in %.1fs", len(out), time.time() - t0)
    gw._disk_put(_KEY_SPOT, out, SOURCE)
    return out


# ── 灌库 ────────────────────────────────────────────────────────────────
def ingest(industries: list[str] | None = None, *, use_cache: bool = True,
           sleep_between: float = 0.0) -> dict:
    """把申万一级行业分类灌进 sa_classification（+ 权重明细表）。

    - `sa_classification`：point-in-time 分类本体（standard='sw'），
      effective_from 取成分股「计入日期」，effective_to 留空（当前仍在职）。
    - `sa_sector_constituent`：另存**权重**与成分名，供板块热力按权重聚合
      （分类表无权重列，不宜为了热力去改分类表语义）。

    幂等：分类按 (symbol, standard, effective_from) UPSERT；成分表按
    (standard, industry_code, symbol, as_of) UPSERT。

    返回 {"industries": N, "members": N, "errors": N, "elapsed": s, "as_of": date}。
    """
    from .models_sa import (upsert_classification, replace_sector_constituents)

    t0 = time.time()
    as_of = date.today().isoformat()
    info = industry_info(use_cache=use_cache)
    if industries:
        wanted = {str(c).strip().replace(".SI", "") for c in industries}
        info = [r for r in info if r["code"] in wanted]

    stats = {"industries": 0, "members": 0, "errors": 0,
             "elapsed": 0.0, "as_of": as_of}
    for r in info:
        try:
            ms = members(r["code"], use_cache=use_cache)
        except SectorSourceUnavailable as err:
            stats["errors"] += 1
            _log.warning("sector_sw members failed %s %s: %s", r["code"], r["name"], err)
            continue
        if not ms:
            continue

        det = []
        for m in ms:
            eff = m["in_date"] or as_of
            try:
                upsert_classification(
                    symbol=m["symbol"], standard=STANDARD,
                    industry_l1=r["name"], industry_l2=None, industry_l3=None,
                    effective_from=eff, effective_to=None, source=SOURCE)
                stats["members"] += 1
            except Exception as err:                          # noqa: BLE001
                stats["errors"] += 1
                _log.warning("upsert_classification failed %s/%s: %s",
                             m["symbol"], r["code"], err)
                continue
            det.append({"symbol": m["symbol"], "name": m["name"],
                        "weight": m["weight"], "in_date": m["in_date"]})
        try:
            replace_sector_constituents(
                standard=STANDARD, industry_code=r["code"],
                industry_name=r["name"], rows=det, as_of=as_of, source=SOURCE)
        except Exception as err:                              # noqa: BLE001
            stats["errors"] += 1
            _log.warning("replace_sector_constituents failed %s: %s", r["code"], err)

        stats["industries"] += 1
        if sleep_between:
            time.sleep(sleep_between)

    stats["elapsed"] = round(time.time() - t0, 1)
    _log.info("sector_sw ingest done: %s", stats)
    return stats
