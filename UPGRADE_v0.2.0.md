
# AutoProp Router v0.2.0 — Auto-Discovery + CrossTrade Account Switches

## New behavior

The Router no longer depends on a hard-coded account list.

Every background CrossTrade/Tradovate snapshot is scanned for accountId + account name.
A previously unseen account is registered automatically.

Recognized profiles currently supported:
- FundedNext challenge names beginning FNFTCH -> configured default FundedNext model (Legacy by default)
- FundedNext funded names beginning FNFTFA -> configured default FundedNext model
- MyFundedFutures Pro evaluation names beginning MFFUEVPRO

Unknown names are still discovered but are quarantined instead of guessed.

## Manual account ON/OFF

Do not use TradingView or edit Router files.

In CrossTrade:
Tradovate -> Account Manager -> account -> Block Signals

Block Signals ON = no new AutoProp CrossTrade signal may open that account.
Block Signals OFF = account is allowed again, subject to Router risk rules.

Closing Only is useful when you want existing positions/exits to continue while preventing new openings.

CrossTrade's account-level signal locks are the authoritative manual switch. The Router does not add a second user-facing enable toggle.

## Latency

Discovery occurs only in the background account refresh loop.
The webhook execution path still uses the hot in-memory cache and does not perform account discovery or balance reads.

## Existing accounts

Accounts first discovered after they have already been trading are marked risk_ready=false because current balance alone cannot prove their historical prop MLL.

They require one one-time MLL bootstrap.

A brand-new recognized challenge/funded account first seen at its starting balance becomes risk-ready automatically.

## Railway

Keep:
AUTO_DISCOVERY=true
FUNDEDNEXT_DEFAULT_MODEL=Legacy
AUTOPROP_EXECUTION_MODE=shadow
SQLITE_PATH=/data/autoprop_router.sqlite3

Root Directory remains blank.
