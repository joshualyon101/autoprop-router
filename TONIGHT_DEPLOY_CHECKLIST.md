# AutoProp — Tonight's Railway Deployment Checklist

## 0. Do not wipe the volume

Railway volume usage is currently high. Increase the volume capacity with **Live Resize** first. Keep the existing volume and data intact.

## 1. Before replacing the old Router, save its state

Open these on the currently deployed service and save the JSON responses:

```text
https://autoprop-router-production.up.railway.app/admin/accounts/<WEBHOOK_TOKEN>
https://autoprop-router-production.up.railway.app/admin/live-readiness/<WEBHOOK_TOKEN>
```

This is the fastest way to preserve current account classification/rules/MLL information before code replacement.

## 2. Deploy v1.2.2 RC1 with the final gate FALSE

Use one Railway replica. Keep the existing CrossTrade token and webhook token.

Set/confirm:

```text
CROSSTRADE_BASE_URL=https://app.crosstrade.io
CROSSTRADE_TOKEN=<existing secret>
AUTOPROP_WEBHOOK_TOKEN=<existing secret>
SQLITE_PATH=/data/autoprop_router_v122.sqlite3
ACCOUNT_CONFIG_JSON=<verified registry JSON>
RISK_STATE_JSON=<verified current risk-state JSON, or write states after deploy>
MAX_STATE_AGE_SECONDS=20
REQUEST_TIMEOUT_SECONDS=4
DEFAULT_EXECUTION_SYMBOL=MNQ1!
AUTOPROP_EXECUTION_MODE=live
AUTOPROP_MANAGEMENT_MODE=exact_formula_parity
LIVE_MAX_QTY_PER_ACCOUNT=0
NATIVE_ATM_BREAKEVEN_AFTER_TP1=false
EXECUTION_REQUIRE_PROTECTIVE_STOP=true
AUTOPROP_LIVE_ARM=I_UNDERSTAND_LIVE_ORDERS
AUTOPROP_FULL_SCALE_ARM=I_UNDERSTAND_FULL_SCALE
TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false
```

## 3. Check staging endpoints

```text
/health
/admin/discovery/<token>
/admin/live-readiness/<token>
/admin/storage/<token>
/admin/dry-run/<token>
```

Do not continue if `problems` is non-empty.

## 4. Create the new TradingView alert

RC3 Pine on MNQ1! 1-minute, with:

```text
Execution Alert Route = Railway Router
Condition = AutoProp Fusion / Order fills and alert() function calls
Message = {{strategy.order.alert_message}}
Webhook = https://autoprop-router-production.up.railway.app/webhook/tradingview/<WEBHOOK_TOKEN>
```

Do not add wrapper JSON.

## 5. Arm only after readiness is clean

Change only:

```text
TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true
```

Re-check `/health` and `/admin/live-readiness/<token>`.

## 6. First trade must be watched

Do not make the first broker-mutating trade an unattended overnight acceptance test. Confirm account participation, size, exact protective orders, Core split/management or Silver stop lock, and no duplicate orders. After that acceptance passes, unattended operation is a separate decision.
