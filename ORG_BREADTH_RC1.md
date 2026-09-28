# ORG breadth sizing canary

This release extends shadow Router v1.2.8.10 and pairs with
`AutoProp_ICT_Fusion_v1.2.4A_DIRECT_SHADOW_ORG_BREADTH_RC4.pine`.
Its release version is `1.2.8.11-shadow-observer-org-breadth-rc1`.

## Pine rule

The new **Half ORG on high breadth** input is off by default. When enabled,
the last completed five-session `NASDAQ:NDXE` return minus the matching
`NASDAQ:NDX` return must exceed +0.50 percentage points. Then the final ORG
entry quantity is floored to half, with a one-contract minimum. It applies
to primary, deferred, fresh-FVG re-entry, and reclaim ORG entries after the
existing consistency cap. Missing index observations keep normal sizing.
Silver and all other engines remain unchanged. No regime-based trade halt
or risk transfer is added.

The original Pine account, balance-source, route, and alert inputs remain
available. The strategy declaration still defaults to $50,000. To reproduce
the prior RG7 100K Challenge comparison, match the account inputs and
Strategy Properties to that test. The production candidate has not been
compiled or backtested inside TradingView as part of this release.

## Shadow observation

Router-format ORG ENTRY messages now include `RG_VER=EW5_B50`, `RG_EN`,
`B5`, `RG_HALF`, and `Q_BASE` alongside the actual `QTY`. The shadow observer
records a `regime_sizing` diagnostic with `MATCH`, `MISMATCH`, or
`UNVERIFIABLE`, comparing the source quantity with the Pine-supplied base
quantity and breadth flag. An inconsistent alert remains observational;
it never places an order or changes account state. Older ORG messages are
still accepted without this diagnostic.

The Direct CrossTrade message format remains unchanged. Pine sends its
already-sized ORG quantity directly; the shadow Router does **not** halve
it again and does not simulate broker/account quantities. Differences
between separate Direct and Shadow chart account inputs are not resolved
by this diagnostic.

## Deployment boundary

Keep `AUTOPROP_EXECUTION_MODE=shadow`,
`TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`, and
`SHADOW_BROKER_READS_ENABLED=false`. The mutation firewall and separate
shadow database remain required. This package was prepared, not deployed.

If proceeding with a limited canary, enable the Pine input only on that
account's Direct alert snapshot and the one canonical Shadow alert when
comparing the same profile. Recreate each affected TradingView alert after
compiling and changing its settings; retire the old snapshot before the new
one runs. Preserve one Shadow feed rather than cloning it per Direct account.
Follow `DEPLOY_V1_2_8_11.md` for the route and account-state
constraints. In particular, the Direct route's live/manual account-state
limitation has not been changed by this work.
