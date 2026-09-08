# AutoProp Router v1.2.2 — Exact Parity Production RC1

Production-wired release candidate for AutoProp ICT Fusion v1.1.5 DUAL ROUTE RC3 in **Railway Router** mode.

## Safety state

The service is designed to be deployed in a fully staged/disarmed state first:

```text
TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false
```

With that gate false, webhook payloads are parsed but broker mutation is blocked. Health, account discovery, storage, cash/MLL/ledger state, and dry-run endpoints remain available.

Only set the final gate true after `/admin/live-readiness/<token>` reports no problems for every enabled account and the new TradingView alert has been rebuilt from the parity-validated RC3 Pine.

## Production guarantees implemented

- Pine owns all five-engine setup qualification and portfolio arbitration.
- Router independently sizes every destination account from exact Challenge/Funded/Personal formulas.
- Pine-equivalent nearest-contract rounding, including `.5` ties upward.
- Personal Aggressive `1.20x`; Funded pre-lock Aggressive `1.10x`.
- ORG-only modeled round-trip execution cost in sizing.
- Destination-specific Core TP1/runner split and consistency target clipping.
- Funded Core 12R minimum-loss-buffer safety.
- Closed cash required; net liquidation is not substituted.
- Prop MLL/failure floor must be separately verified.
- New York calendar-day realized P&L from durable fills.
- ORG re-entry requires destination-specific prior stop proof.
- Broker position/order gates and per-account event dedupe.
- Single-target absolute bracket readback.
- Core native multi-tier protection followed by exact absolute-price child normalization/readback.
- Core 75%-before-50% pre-TP1 management, destination TP1 detection, runner BE +1 tick, completed-5m swing trail.
- Silver +3R / +0.5R stop-lock from the destination's actual broker fill.
- Actual destination entry fill is mandatory for live management; unprovable accepted fills are flattened.
- Ambiguous PLACE is reconciled by stable custom ID and never blindly resent.
- Stop changes are monotonic and owned-child only.
- Global and engine-scoped hard-flat handling.
- Linked CrossTrade accounts are discovered/read back; unknown linked accounts remain unconfigured and cannot receive trades.
- Versioned SQLite path avoids collision with legacy Router database schemas.
- Dedupe retention prevents unbounded growth of the new SQLite database.

## Validation

See `PRODUCTION_VALIDATION_REPORT.md`.

Current offline release validation:

```text
python -m compileall -q .       PASS
pytest -q                        150 passed
FastAPI/settings startup         PASS
Independent Golden Oracle        35 passed
```

## Railway staging endpoints

```text
GET /health
GET /admin/accounts/<WEBHOOK_TOKEN>
GET /admin/discovery/<WEBHOOK_TOKEN>
GET /admin/live-readiness/<WEBHOOK_TOKEN>
GET /admin/dry-run/<WEBHOOK_TOKEN>
GET /admin/storage/<WEBHOOK_TOKEN>
POST /admin/risk-state/<WEBHOOK_TOKEN>
POST /webhook/tradingview/<WEBHOOK_TOKEN>
```

## Required Railway variables

Use `.env.example` as the canonical list. Secrets and live account registry/risk state belong in Railway variables, not GitHub.

For migration safety the default database is:

```text
SQLITE_PATH=/data/autoprop_router_v122.sqlite3
```

Do not point this RC at the legacy `/data/autoprop_router.sqlite3` unless a separate migration has been performed.

## Account state

`ACCOUNT_CONFIG_JSON` is the authoritative rule registry when supplied. Each enabled account must have `rules_verified=true`.

`RISK_STATE_JSON` or `POST /admin/risk-state/<token>` supplies facts CrossTrade cannot infer from a normal Tradovate account snapshot, including current prop MLL/failure floor and funded-lock state. For prop accounts, unverified MLL or unverified durable-ledger coverage fails closed.

## TradingView alert

Pine: `AutoProp_ICT_Fusion_v1.1.5_DUAL_ROUTE_RC3_ROUTER_CONTRACT_FIX.pine`

Strategy input:

```text
Execution Alert Route = Railway Router
```

TradingView alert:

```text
Condition: AutoProp Fusion — Order fills and alert() function calls
Message: {{strategy.order.alert_message}}
Webhook: https://autoprop-router-production.up.railway.app/webhook/tradingview/<WEBHOOK_TOKEN>
```

Do not wrap the message in extra JSON. Recreate the alert after any Pine version or route change.

## First-live acceptance

The first broker-mutating trade remains an acceptance test. It should be watched while awake. Confirm exact participating accounts, quantities, targets, Core split, protective orders, no duplicates, and management transitions before treating the system as unattended production.
