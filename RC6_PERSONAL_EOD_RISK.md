# Router v1.2.7 RC6 — Personal EOD Percent-Risk Scaling

## What changed
Only Personal-account percent-risk basis changed.

Previous behavior:
`percent risk = starting_balance × personal_risk_value`

RC6 behavior:
`actual EOD basis = closed_cash_balance - realized_today`

Then:
- `actual_eod` mode: risk basis = actual EOD basis
- `virtual_eod` mode:
  `risk basis = virtual_start + (actual EOD basis - broker_anchor)`

The daily realized P&L ledger is already fail-closed and New York calendar-day based, so this freezes Personal sizing during the day and updates the basis after the day rolls.

Challenge/Funded risk formulas, engine allocation, Core management, execution, durable fast-ACK, CrossTrade rate limiting and all broker mutation logic are unchanged.

## Example: $12K broker account acting like a $20K account
Configure:
- account_type = personal
- profile = standard
- personal_risk_method = percent
- personal_risk_value = 0.75
- personal_balance_mode = virtual_eod
- personal_virtual_start_balance = 20000
- personal_broker_anchor_balance = the actual confirmed EOD cash balance when enabled (for example, 12000)
- max_contracts = 20

At a $12,000 anchor:
- virtual EOD risk basis = $20,000
- base risk at 0.75% = $150

If trading gains move actual EOD cash to $12,500:
- virtual EOD basis = $20,500
- base risk = $153.75

If you externally deposit or withdraw money, update the broker anchor so the virtual bridge does not mistake the cash flow for trading P&L.
