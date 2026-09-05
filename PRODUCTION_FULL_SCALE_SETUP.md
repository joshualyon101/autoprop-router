# AutoProp Router v1.0.0 RC1 — Full-Scale Production Setup

This build supersedes v0.2 and v0.3. There is no mandatory 1-contract canary cap.

## Why RC1 rather than claiming live-final

The architecture and normal-scale sizing path are complete. The exchange is closed during this build, so actual CrossTrade -> Tradovate acceptance/fill behavior cannot be truthfully certified until the first market-open acceptance order. No code change is expected merely to remove the RC label.

## Default execution architecture

`AUTOPROP_MANAGEMENT_MODE=native_atm`

- AutoProp Router determines each account's quantity from live balance / MLL cushion.
- Entry orders fan out concurrently.
- Tradovate owns the fixed protective stop, TP1/TP2 scale-out, OCO links, and target-fill runner breakeven.
- TradingView STOP_MOVE / ordinary EXIT messages are ignored in this mode to prevent double exits.
- FLATTEN / SESSION_EXIT still routes to every enabled AutoProp account.

The alternative `tv_managed` mode is implemented but should not be selected until the current Fusion Pine alert payload is audited. It attaches a broker catastrophic stop, supports partial closes, STOP_MOVE through CancelAndBracket, and full exits.

## Full-scale Railway variables

AUTO_DISCOVERY=true
FUNDEDNEXT_DEFAULT_MODEL=Legacy
AUTOPROP_EXECUTION_MODE=live
AUTOPROP_MANAGEMENT_MODE=native_atm
LIVE_MAX_QTY_PER_ACCOUNT=0
NATIVE_ATM_BREAKEVEN_AFTER_TP1=true
EXECUTION_REQUIRE_PROTECTIVE_STOP=true
USE_CROSSTRADE_POSITION_GATE=true

Three independent firing gates:

AUTOPROP_LIVE_ARM=I_UNDERSTAND_LIVE_ORDERS
AUTOPROP_FULL_SCALE_ARM=I_UNDERSTAND_FULL_SCALE
TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true

Until all three are present, `/health` reports live_armed false and the Router remains non-mutating even if EXECUTION_MODE=live.

## Existing account bootstrap

Every existing trailing-drawdown account needs its real current MLL floor entered once in Account Manager before risk_ready should be enabled. CrossTrade max-loss protection is an excellent redundant hard stop, but account balance alone does not reveal the historical trailing floor needed for AutoProp sizing.

Account Manager:
`https://YOUR-DOMAIN/admin/accounts/YOUR_WEBHOOK_TOKEN`

Readiness:
`https://YOUR-DOMAIN/admin/live-readiness/YOUR_WEBHOOK_TOKEN`

Weekend sizing test (never sends a broker order):
POST a Router-native TradeSignal to
`https://YOUR-DOMAIN/admin/dry-run/YOUR_WEBHOOK_TOKEN`

Alert parser test (never sends an order):
POST the exact TradingView body to
`https://YOUR-DOMAIN/admin/normalize/YOUR_WEBHOOK_TOKEN`

## CrossTrade account ON/OFF

- Closing Only: use when the account has an open AutoProp position but you want no new entries; exit/protection commands remain allowed.
- Block Signals: complete automation off. Use it after the account is flat.

## Current auto profiles

- FundedNext Legacy Challenge: 25K/50K/100K, MLL 1K/2K/3K, targets 1.25K/3K/6K, max micros 20/30/50, 40% consistency, EOD trail-to-lock.
- FundedNext Legacy Funded: max micros 30/50/70, no consistency, EOD trail-to-lock.
- MFFU Pro Evaluation: 50K/100K/150K, MLL 2K/3K/4.5K, targets 3K/6K/9K, max micros 30/60/90, 50% consistency, EOD trail locks at starting balance + $100.
- Unknown account names are discovered but quarantined until manually verified.

MFFU Pro Sim-Funded rules differ materially (including 5/10/15 micro limits), so a newly funded MFFU account should be manually verified in Account Manager unless its naming pattern is explicitly added later.
