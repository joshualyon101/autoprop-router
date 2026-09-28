# Router v1.2.8.11 validation

This candidate pairs with `AutoProp_ICT_Fusion_v1.2.4A_DIRECT_SHADOW_ORG_BREADTH_RC4.pine`.

## Automated checks

- Live-role regression suite: **315 passed** with `AUTOPROP_EXECUTION_MODE=live`, excluding the four shadow-only test modules.
- Shadow-role suite: **27 passed** with `AUTOPROP_EXECUTION_MODE=shadow`, `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`, and `SHADOW_BROKER_READS_ENABLED=false`.
- Nine new shadow breadth cases cover even/odd and minimum-one quantity, the strict +0.50pp boundary, disabled and missing readings, inconsistent fields, and a legacy alert.

The test runner emits existing FastAPI/Starlette deprecation warnings. No failed tests remain in either role-specific run.

## Manual checks before using a canary

The Pine candidate has not been compiled or backtested in TradingView. Compile it and compare Off versus On under the intended account inputs and Strategy Properties. Confirm ORG entries at breadth readings above +0.50pp, including deferred and reclaim cases, preserve the final risk cap and produce the expected actual alert quantity. Inspect a sample Router-format alert for `RG_VER`, `RG_EN`, `B5`, `RG_HALF`, and `Q_BASE` alongside `QTY`; check `regime_sizing.verification` in the shadow inbox. A Direct alert carries the already-sized `qty` to CrossTrade, so use a separate canary account and monitor actual fills and stops. Recreate affected TradingView alerts after any script or input change.

This validation does not establish a live yield-curve improvement. The prior 100K research result used a different test configuration from this candidate's default 50K strategy capital.
