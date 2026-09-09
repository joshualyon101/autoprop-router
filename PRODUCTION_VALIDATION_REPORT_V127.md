# AutoProp Router v1.2.7 Exact Parity Production RC6 — Validation

## Version
`1.2.7-exact-parity-production-rc6`

## Intentional logic change
Personal-account percentage risk now uses a completed New York EOD balance rather than the configured starting balance.

### Actual EOD mode
`basis = closed_cash_balance - realized_today`

### Virtual EOD mode
`basis = virtual_start + (actual_eod - broker_anchor)`

Then:
`base_risk = basis × personal_risk_value / 100`

Standard/Aggressive behavior remains unchanged after base risk; Aggressive still applies the existing 1.20 multiplier.

## Focused tests
7/7 focused personal EOD risk tests PASS

## Critical files byte-identical to v1.2.6 RC5
- parity.py: **True**
- execution.py: **True**
- management.py: **True**
- events.py: **True**
- live.py: **True**
- org_reentry.py: **True**
- state.py: **True**
- state_refresh.py: **True**
- store.py: **True**
- crosstrade.py: **True**

`models.py` and `allocation.py` are intentionally changed.
`version.py` is intentionally changed.

## Operational note
When using `virtual_eod`, external deposits/withdrawals must be accompanied by a broker-anchor update or they will be interpreted as account performance.

## External production gate
Deploy with `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`, verify `/health` and `/admin/live-readiness`, add/verify the personal CrossTrade Live account config, then re-arm.
