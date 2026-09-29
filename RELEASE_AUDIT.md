# Release audit — Pine v1.2.5 and Router v1.2.8.12

Audit completed for this delivered pair on September 28, 2026 (New York).

## Pine changes and review

- Removed the optional ORG breadth input. The strict +0.50pp rule now applies to every account mode and size.
- Updated the customer strategy title, release identity, account-state help, and ORG TODAY status row.
- Added `RG_POLICY=ALWAYS_ON` to Router-format ORG alerts. The existing Direct CrossTrade command format and quantity routing are unchanged.
- Compared the entire final source with the RC4 source used in the supplied backtests. After accounting for the fixed On condition, identity, alert metadata, tooltip, and table changes, the remaining source is identical.
- Checked balanced delimiters, all six ORG alert call sites, all four quantity-adjustment sites, and the preserved decision for deferred setups.
- Confirmed the half-size helper leaves zero quantity at zero and never increases a positive executable quantity. Reduction occurs after the existing caps. Reclaim and fresh-FVG entries use the same rule. Deferred quantities are recalculated from the original risk calculation before applying the stored rule, preventing a double reduction.
- Confirmed both index requests use completed daily observations, `close[1]` and `close[6]`, with `lookahead_on`. No unconfirmed daily close was introduced.
- Confirmed the primary entry, deferred, and reclaim alert paths carry actual final QTY and the corresponding pre-regime Q_BASE. Broker-native modeled TP/SL exits retain their existing Direct alert suppression.

The final Pine has **not** been compiled inside TradingView by this audit. The RC4 base compiled and ran in the user-supplied exports. Local source equivalence and delimiter checks are not a replacement for TradingView's compiler, runtime limits, or visual checks. The added table row uses the existing table style; visual verification awaits chart loading. Historical results from RC4 On remain evidence for the selected trading logic, not a fresh run of the final file or proof of live profitability.

## Shadow audit fixes

- Required breadth fields no longer silently pass as a normal-size match when missing.
- Unknown policies, malformed flags, invalid quantities, and invalid/infinite readings are handled as mismatched or unverifiable evidence without raising an execution action.
- NaN/index-unavailable messages report their fallback quantity separately from the unavailable breadth check.
- Rounded +0.50pp values report an unverifiable condition instead of a false mismatch. A quantity mismatch still remains a mismatch.
- The final ALWAYS_ON policy is checked, while older optional and legacy alert formats remain accepted.

## Executed tests

| Check | Result |
| --- | --- |
| Shadow observer, intake/worker, mutation firewall, and breadth contract suite | **54 passed** |
| Existing live-role regression suite, run separately in a local test environment | **315 passed** |
| Python AST syntax check | **50 files passed** |
| Pine source equivalence and targeted structural checks | **Passed** |

Tests covered primary and re-entry alerts in both directions, even/odd/minimum-one contract counts, policy contradictions, missing fields, malformed values, boundary precision, backward compatibility, JSON-safe output, and the final alert passing through the actual shadow endpoint and worker. Existing tests verify no broker mutations, separate shadow storage, and no live runtime/ownership access in shadow mode. Running live-role regression tests does not deploy or arm the Router.

Existing dependency deprecation warnings remain for FastAPI lifecycle handlers and test-client interfaces. They are warnings under the pinned dependencies, not test failures. Dependency migration was not included in this release.

## Deployment scope and remaining limits

The Router defaults to shadow, alert-contract verification false, and broker reads disabled. Actual Railway variables must retain those settings. No deployed health check, broker order, or live alert was changed or verified during this file audit.

The ORG breadth check uses Pine-supplied fields; it does not fetch market breadth independently or check actual broker fills. The dormant live Router sizing path has not been extended to apply the ORG rule to recalculated destination quantities. Continue Direct production with passive Router observation until a separate live-routing release is validated.

## Technical references

- Confirmed higher-timeframe requests: https://www.tradingview.com/pine-script-docs/faq/other-data-and-timeframes/
- Pine runtime limits: https://www.tradingview.com/pine-script-docs/writing/limitations/
- Strategy alert snapshots: https://www.tradingview.com/support/solutions/43000481368-strategy-alerts/
