# AutoProp Router v0.3.0 — Account Manager + Live-Ready Canary

Skip v0.2.0 and deploy this version directly.

## What is new

- Auto-discovers new CrossTrade/Tradovate accounts.
- Automatically classifies known FundedNext and MFFU naming patterns.
- Adds a protected web Account Manager for manual account-type/rule overrides.
- Manual overrides persist on Railway's `/data` volume and win over auto-detection.
- CrossTrade remains the manual account ON/OFF control through Block Signals / Closing Only.
- Adds `canary` execution mode: exactly one selected account, capped at 1 contract by default.
- Uses Tradovate-native ATM brackets for entry protection and multi-target scale-out.
- Supports full EXIT / FLATTEN events through CrossTrade's account/instrument flatten endpoint.
- Uses continuous `MNQ1!` by default so CrossTrade can resolve/pin the current Tradovate contract.

## Protected Account Manager

`https://YOUR-DOMAIN/admin/accounts/YOUR_WEBHOOK_TOKEN`

Use Edit when:
- an account is misclassified,
- a challenge becomes a funded account without a new recognizable ID/name,
- you buy a new prop model the Router does not know yet,
- an existing account needs its one-time current MLL bootstrap.

Do NOT use this page as the trading ON/OFF switch.

Trading ON/OFF:
CrossTrade -> Tradovate Account Manager -> Block Signals / Closing Only.

## Live readiness

`https://YOUR-DOMAIN/admin/live-readiness/YOUR_WEBHOOK_TOKEN`

Do not arm live until this reports `"ready": true`.

## First live rollout

Railway variables:

AUTOPROP_EXECUTION_MODE=canary
AUTOPROP_LIVE_ARM=I_UNDERSTAND_LIVE_ORDERS
CANARY_ACCOUNT_ID=CT_xxxxxxxx
CANARY_MAX_QTY=1
EXECUTION_REQUIRE_BRACKET=true
USE_NATIVE_ATM=true
USE_CROSSTRADE_POSITION_GATE=true

The canary account is the only account the Router will attempt. CrossTrade Block Signals can still block it.

After one real entry is verified in CrossTrade Alert History and Tradovate:
- correct account
- correct direction
- exactly 1 MNQ
- native stop exists
- target(s) exist
- OCO/ATM relationship is correct
- flatten/exit behavior is correct

then change:
AUTOPROP_EXECUTION_MODE=live

and leave:
AUTOPROP_LIVE_ARM=I_UNDERSTAND_LIVE_ORDERS

`LIVE_MAX_QTY_PER_ACCOUNT=0` means use AutoProp's calculated quantities.
Set a positive number to add a hard global cap during rollout.

## Latency

The webhook path still does not fetch balances. It uses the hot cache.

CrossTrade's `requireMarketPosition=flat` remains ON by default. CrossTrade documents that a genuinely flat Tradovate reading is rechecked once, about one second later. This is the largest deliberate entry delay in the first live rollout. Keep it enabled for the first canary order; benchmark it before considering removal.
