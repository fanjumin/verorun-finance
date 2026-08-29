# Changelog

## v1.4.0-pre — 2026-08-29

### Changes

- refactor(stock_analysis): provider abstraction, zero behavior change (P0-a)
- 新增 `providers/` 包（base / commons / sina / tencent）与 `gateway.py` 数据网关
- `stock_skill.py` 5 处行情/新闻调用点全部改走 gateway（类别路由 + 源前缀缓存）
- 删除 `_get_price_data` / `_get_latest_price`；`_market_symbol` / `_index_symbol` 标记 deprecated
- 回归基线锚点：`git tag v1.3.0-baseline` + `tests/fixtures/baseline_v130.json` + `tools/` 录制/对比脚本
- 13 用例回归对比 OK（逐字段 / 数值容差 ±0.5%），无效 symbol 稳定 error

## v1.3.0 — 2026-08-28

### Changes

- Version bump from v1.2.0
- fix(stock_analysis): harden NaN handling, symbol normalization and field parsing
- fix(plugins): attach edition roles to dedicated sub-agents
