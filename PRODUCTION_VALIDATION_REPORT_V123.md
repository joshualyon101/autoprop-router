# AutoProp Router v1.2.3 Exact Parity Production RC2 — Validation

Purpose: production-wired exact-parity Router plus read-only legacy SQLite state inspector for migration from the currently deployed Router.

Validation:
- python -m compileall -q . — PASS
- pytest -q — 152 passed
- New endpoint: GET /admin/legacy-inspect/<token>
- Legacy database opened read-only at /data/autoprop_router.sqlite3
- Secret-like columns are redacted by name
- New state database remains isolated at /data/autoprop_router_v123.sqlite3
- TradingView alert contract gate defaults false
- No change to trading formulas, allocation logic, execution logic, management logic, or broker mutation gates relative to v1.2.2 Production RC1
