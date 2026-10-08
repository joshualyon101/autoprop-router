# Upload instructions

Upload all six files in this folder to the root of the existing
`autoprop-router` GitHub repository and overwrite files with the same names.
Do not delete unrelated repository files, and do not upload the ZIP itself as
the application source.

Railway should deploy automatically after the GitHub commit.

## Verification

1. Confirm `/health` reports version
   `1.2.8.18-crosstrade-cash-schema-hotfix-rc1`.
2. Temporarily set `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false` if needed while
   verifying onboarding.
3. Run `/admin/discovery/<token>`.
4. Verify `FNFTCHJOSHUALYON75290` is onboarded rather than quarantined.
5. Verify `/health` reports `registered_accounts: 6` and inspect the new
   account's inferred rules before rearming live trading.

The patch preserves v1.2.8.17 DWC SB25/05 behavior. It adds support for
CrossTrade's `balance.totalCashValue` and `balance.cashUSD` fields while
deliberately rejecting `netLiq` as a closed-cash substitute.
