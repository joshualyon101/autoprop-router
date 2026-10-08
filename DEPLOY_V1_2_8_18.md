# AutoProp Router v1.2.8.18

This release preserves the v1.2.8.17 DWC SB25/05 behavior and adds compatibility
with the current CrossTrade Tradovate account-detail balance schema.

## Change

- Zero-touch onboarding now reads `balance.totalCashValue` as closed cash.
- `balance.cashUSD` is accepted as a fallback.
- `balance.netLiq` remains intentionally excluded because it can include open P&L.

## Expected verification

After deployment, run `/admin/discovery/<token>`. The new account should move from
`quarantined` to `onboarded`, then `/health` should report six registered accounts.
Verify the inferred prop rules before leaving broker mutation armed.
