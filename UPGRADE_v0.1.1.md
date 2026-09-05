# v0.1.1 ID-Safe Upgrade

Why: linked Tradovate display names can be ambiguous in the UI. The Router now matches account state by the unique CrossTrade/Tradovate `accountId` first.

Known account IDs loaded for discovery:
- FundedNext: 64016206, 61365404, 64016199, 59230927
- My Funded Futures: 61867057, 61867137, 63599504

All seven remain:
- enabled=false
- rules_verified=false

So this build can discover/cache balances but cannot route trades.

Protected account discovery:
`/admin/discover/<AUTOPROP_WEBHOOK_TOKEN>`

Railway Root Directory remains blank.
Persistent volume remains mounted at `/data`.
Keep `AUTOPROP_EXECUTION_MODE=shadow`.
