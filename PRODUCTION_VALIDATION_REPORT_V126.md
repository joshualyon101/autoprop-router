# AutoProp Router v1.2.6 RC5 — Validation Report

Candidate: `1.2.6-exact-parity-production-rc5`
Purpose: durable fast acknowledgement for TradingView webhooks after the first live acceptance alert timed out before CrossTrade received a route.

## Focused RC5 tests
- durable SQLite persistence occurs before HTTP acknowledgement
- HTTP acknowledgement remains fast while mocked downstream routing sleeps 0.6 seconds
- same-day identical TradingView retry is acknowledged but not queued twice
- restart recovery requeues stranded PROCESSING inbox work
- RC3 state-refresh regression tests retained
- RC4 CrossTrade rate-limit hardening regression tests retained

Result: **9/9 PASS** across RC5 + RC3 + RC4 focused regression set.

## Frozen formula/allocation tests
`test_formula_parity.py` + `test_allocation.py`: **108/108 PASS**.

## Build/startup
- `python -m py_compile *.py`: PASS
- Uvicorn startup: PASS
- `/health`: PASS
- observed version: `1.2.6-exact-parity-production-rc5`

## Byte identity versus v1.2.5 RC4
The following production logic files are byte-identical:
`parity.py`, `allocation.py`, `execution.py`, `management.py`, `events.py`, `models.py`, `live.py`, `org_reentry.py`, `state.py`, `state_refresh.py`, `crosstrade.py`, `config.py`, `readiness.py`, `service.py`, `settings.py`.

Changed files:
- `main.py`: durable webhook ingress + worker + inbox diagnostics
- `store.py`: durable webhook inbox persistence/claim/recovery/status
- `version.py`: RC5 version bump

## Deployment safety
Deploy with `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`. After health and live-readiness pass, set it true and redeploy. The first new live signal remains an acceptance test and should be watched.
