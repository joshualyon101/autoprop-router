# AutoProp Six-Engine Coordinated Deployment Checklist — RC1

## 1. TradingView gate first
Compile `AutoProp_ICT_Fusion_v1.2.3_SIX_ENGINE_PRODUCTION_RC2_ASW_ROUTER_ARMED.pine` on CME_MINI:MNQ1!, 1-minute standard candles.

Required before Router code promotion:
- zero Pine compile errors;
- quick Strategy Tester sanity reproduces the validated six-engine economics for the same test configuration;
- no unexpected trade-count/order-geometry difference from the RC used for the final Challenge/Funded validation.

If Pine differs, STOP. Do not compensate in Router code.

## 2. Preserve production state
Before replacing Router code, save the current read-only responses from:
- `/admin/accounts/<token>`
- `/admin/live-readiness/<token>`
- `/admin/webhook-inbox/<token>`

Do not wipe or replace the Railway volume.
This release keeps the current default SQLite path and adds only the new `asw_pending` table automatically.

## 3. Disarm before deployment
Set:
`TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`

Keep one Railway replica.
Keep the existing CrossTrade/webhook secrets in Railway variables; do not commit them to GitHub.

## 4. Deploy Router v1.2.8 RC1
Upload the flat-root Router package contents.
Do not merge the separate v1.2.9 lifecycle-shadow branch into this release.

Expected `/health` after deploy:
- version = `1.2.8-exact-parity-six-engine-asw-rc1`
- execution_mode = `live`
- management_mode = `exact_formula_parity`
- execution_symbol = `MNQ1!`
- asw_limit_contract = `ASW_LIMIT_V1`
- configuration_ready = true
- broker_mutation_armed = false
- asw_pending_limits = 0 before the first ASW setup

## 5. Staging checks
Run:
- `/health`
- `/admin/discovery/<token>`
- `/admin/live-readiness/<token>`
- `/admin/storage/<token>`
- `/admin/webhook-inbox/<token>`

Do not proceed with any readiness problem.

## 6. Recreate TradingView alert
Use the new Pine RC2.

Condition:
`AutoProp Fusion — Order fills and alert() function calls`

Message:
`{{strategy.order.alert_message}}`

Webhook:
current production Router webhook URL.

Do not add wrapper JSON.

## 7. Optional safest ASW acceptance
If a linked demo/sim Tradovate destination is available, validate one ASW_LIMIT_V1 candidate there before prop accounts are armed:
- LIMIT is resting before fill;
- exact midpoint price;
- exact account-specific qty;
- exact stop and 1.50R target;
- cancelAfter present;
- CANCEL_PENDING removes an unfilled entry;
- full fill produces exact owned stop/target protection;
- partial-at-expiry is flattened;
- no duplicate market entry is generated.

## 8. Arm
Only after staging is clean:
`TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true`

Re-check `/health` and `/admin/live-readiness/<token>`.

## 9. First natural six-engine acceptance
Watch the first broker-mutating trade end-to-end.
For ASW specifically confirm:
- intended accounts only;
- Challenge qty = exact native Q0 or skip;
- Funded qty = whole Q0 multiple only;
- exact LIMIT entry;
- exact stop/target;
- no duplicate entry;
- pending count clears after fill/cancel;
- no orphan orders after target/stop/cancel/time-flat.

Any failure: set the TradingView gate false and diagnose. Do not relax the parity rules.
