# AutoProp Router v1.2.8.12 — Shadow Observer

Companion to **AutoProp Fusion - Trade Navigator v1.2.5**.

The Pine strategy permanently applies the ORG breadth rule to Challenge, Funded, and Personal accounts of every size. This Router records the plan and checks its reported ORG sizing. Production execution remains TradingView directly to CrossTrade.

## ORG sizing rule

Use the last completed five-session percentage return of NASDAQ:NDXE minus NASDAQ:NDX. When the difference is **strictly greater than +0.50 percentage points**, halve ORG's executable contract count after the existing quantity limits and consistency calculation. Round down, with a minimum of one for an otherwise valid entry. A zero quantity remains zero. For example: 8 becomes 4, 7 becomes 3, and 1 remains 1.

The rule applies to primary entries, fresh-FVG re-entries, deferred entries, and reclaim entries. Deferred entries retain the breadth condition from setup qualification and apply it once to the freshly recalculated executable quantity. Missing index values retain normal size. The Pine status table's ORG TODAY row reports the current condition. This release has no account-size split and no setting to turn the rule off.

## Router compatibility

The previous v1.2.8.11 shadow Router already accepts the final Pine alert format. Updating is recommended for the improved audit, matching release documentation, and new contract tests; it is not required to halve orders. Pine performs the reduction. The Router does not halve the quantity again.

The final alert includes `RG_VER=EW5_B50`, `RG_POLICY=ALWAYS_ON`, `RG_EN=1`, `B5`, `RG_HALF`, and `Q_BASE` alongside `QTY`. Direct CrossTrade alerts continue to carry the executable quantity in `qty` without these observer fields. Older Pine alerts, including messages without any breadth fields and RC4 with its optional rule disabled, remain accepted by shadow mode.

For new ORG messages, the shadow inbox and last-event summary contain `regime_sizing`:

- `quantity_verification`: whether source QTY matches the supplied base and half flag.
- `condition_verification`: whether the supplied breadth, enabled flag, and policy are consistent.
- `verification`: combined MATCH, MISMATCH, or UNVERIFIABLE.
- `data_status` and, when needed, `reason`: explain missing/invalid readings or a rounded threshold boundary.

The source reports B5 to eight decimal places. If it rounds to the +0.50 boundary, the audit cannot reconstruct the exact comparison and reports UNVERIFIABLE instead of a false mismatch. Missing market data also prevents full verification, even when quantity arithmetic matches. These results never place, change, or cancel orders. They audit source fields, not an independent market feed or broker fill.

## Install and review

Read `DEPLOY_SHADOW.md` for the deployment settings and alert setup. Read `RELEASE_AUDIT.md` for tests, fixes, and remaining validation limits. The package includes no credentials or account state.
