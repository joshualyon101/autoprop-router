# AutoProp Router v1.2.8 — Six-Engine ASW_LIMIT_V1 RC1 Validation
Date: 2026-09-15/16 ET

## Release identity
Router: `1.2.8-exact-parity-six-engine-asw-rc1`
Pine: `AutoProp ICT Fusion v1.2.3 SIX-ENGINE PRODUCTION RC2`
New contract: `ASW_LIMIT_V1`

## Exact production base
This release was built from the uploaded v1.2.7.1 exact-parity production RC6 symbol-hotfix source.
The following uploaded base files matched the published v1.2.7.1 hotfix SHA-256 values exactly before modification:
- allocation.py
- parity.py
- management.py
- events.py
- state.py
- state_refresh.py
- models.py
- config.py
- service.py
- org_reentry.py
- store.py

The existing five-engine execution/allocation methods were source-hash compared before/after the ASW merge. Preserved exactly:
- allocation.allocate / personal_eod_risk_basis / _base_risk
- execution.place_single / place_core / Core normalization / stop management / existing bracket verification
- live.route_entry / market_pulse / silver_stop / ordinary_exit / existing fill proof
- CrossTrade symbol normalization, rate limiting, request transport, existing place/change/flatten methods
- webhook durable worker and webhook ingress

Intentional existing-function extensions are limited to health/readiness ASW telemetry and hard-flat cleanup for ASW-owned orders.

## New ASW event contract
### WORKING_LIMIT
`AUTOPROP_ICT_FUSION|ASW|WORKING_LIMIT|<SIDE>|CONTRACT=ASW_LIMIT_V1|ORDER_TYPE=LIMIT|TIF=DAY|QTY=...|Q0=...|ENTRY=...|SL=...|TP=...|CR=...|T5=...|EXP=...`

### CANCEL_PENDING
`AUTOPROP_ICT_FUSION|ASW|CANCEL_PENDING|<SIDE>|CONTRACT=ASW_LIMIT_V1|T5=...`

### TIME_FLAT
`AUTOPROP_ICT_FUSION|ASW|TIME_FLAT|<SIDE>|CONTRACT=ASW_LIMIT_V1`

## Destination allocation
Challenge:
- exact Q0 native plan or skip;
- full native risk must fit current Challenge risk budget;
- full 1.50R projected profit must fit consistency room;
- no quantity shrink and no target compression.

Funded:
- quantity = Q0 × integer multiple;
- largest safe whole multiple permitted by current funded base risk, contract cap and enabled daily-profit/consistency room;
- existing low-cushion survival rule remains authoritative;
- target remains exact 1.50R.

Personal:
- exact Q0 native plan in this release.

## Broker execution
ASW is not converted to a market entry.
Each accepted destination receives:
- Tradovate `orderType=limit`;
- exact absolute `limitPrice`;
- exact absolute `takeProfit` and `stopLoss` OSO bracket;
- `tif=day`;
- `cancelAfter` equal to remaining source validity, rounded up to minutes and capped to CrossTrade's 1–180 minute contract;
- `requireMarketPosition=flat`;
- `maxPositions=1`;
- stable caller order ID.

The Router persists each pending ASW order in SQLite with the broker parent and owned child order IDs.

## Pending lifecycle / failure behavior
- durable pending state survives Router process restarts;
- a 10-second reconciliation loop is enabled only when the TradingView contract gate is armed;
- unfilled orders are canceled at EXP even if the Pine cancel alert is missed;
- Pine CANCEL_PENDING cancels the parent and defensively flattens scoped ASW exposure so a broker fill cannot silently diverge from a Pine-unfilled setup;
- a partial fill at expiry/cancel is flattened rather than carried at a non-native quantity;
- a full fill must prove exact owned stop/target coverage before promotion to ActiveTrade;
- an unprotected or side-mismatched fill is flattened;
- ambiguous ASW PLACE is never resent blindly; if the parent can be reconciled but OSO child ownership cannot, it is canceled/flattened;
- ASW TIME_FLAT cancels only ASW-owned orders and flattens the scoped MNQ position;
- observed-flat ASW records explicitly clear any remaining owned OCO child IDs to avoid orphan orders.

## CrossTrade API verification
Current CrossTrade documentation was checked during this release. The Tradovate REST surface supports:
- `orderType=limit`
- absolute `limitPrice`
- absolute `takeProfit` / `stopLoss`
- caller `orderId`
- `cancelAfter` 1–180 minutes
- cancel specific order by ID
- order status and lifecycle reads

## Automated validation
`python -m compileall -q .` — PASS

Combined regression harness:
`164 passed`

Coverage includes:
- frozen formula parity grid;
- existing allocation tests;
- existing single-target/Core execution tests;
- fill-proof tests;
- state/management/ORG re-entry tests;
- v1.2.7.1 symbol-hotfix regression;
- RC3 state-refresh regression;
- 14 ASW_LIMIT_V1 tests.

ASW tests cover:
- parser contract enforcement;
- Challenge exact-plan behavior;
- no Challenge downsizing;
- Funded whole-plan scaling;
- cap flooring to a whole multiple;
- Funded consistency without target clipping;
- CR geometry mismatch fail-closed;
- actual LIMIT payload + OSO + cancelAfter;
- expired candidate no-mutation behavior;
- durable pending persistence;
- full-fill exact-bracket promotion;
- expired partial-fill flatten;
- canonical cancel flattening broker exposure.

Startup/health smoke — PASS.
Staged health with a valid test registry returned:
- configuration_ready=true
- broker_mutation_armed=false
- execution_symbol=MNQ1!
- asw_limit_contract=ASW_LIMIT_V1
- asw_pending_limits=0

The only test warnings are FastAPI deprecation warnings for `@app.on_event`; they are inherited architecture warnings, not test failures.

## Pine coordination
The Router-armed Pine RC2 differs from Pine RC1 only by:
- release label/config identity;
- `aswRouterContractVerified = true`;
- comment reflecting the coordinated Router contract.

No signal, sizing, stop, target, timing or arbitration rule was changed in that RC1→RC2 step.

Pine still requires the external TradingView compile and quick Strategy Tester sanity gate before deployment.

## Not included
This release does NOT merge the separately researched Prop Lifecycle shadow v1.2.9 branch.
It is a surgical promotion of ASW onto the current v1.2.7.1 exact-parity production lineage.
