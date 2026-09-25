# AutoProp Router v1.2.8.10 validation

Version: `1.2.8.10-shadow-observer-core-readback-resilience-rc1`

## Incident replay conclusion

The 2026-09-24 Core entry reached all eight destinations successfully. Acceptance spread was
147.8 ms. The Router then closed the positions before TradingView's modeled exits because four
CrossTrade lifecycle reads timed out and four CHANGE commands lacked an immediate final
`Replaced` report. Those observations were uncertainty, not proof that the accepted native ATM
had no protective stop.

## Corrected live behavior

- Lifecycle read timeout or missing/delayed final CHANGE report:
  - retain the accepted native ATM;
  - keep the attempt pending;
  - open the new-entry circuit;
  - retry with backoff;
  - never resend an unresolved CHANGE;
  - do not market-flatten solely because readback is unavailable.
- Explicitly rejected or affirmatively inconsistent protection keeps the fail-safe path.
- Intentional EXIT and HARD_FLAT controls are unchanged.

## Shadow safety

- Separate `/webhook/tradingview-shadow/{token}` intake.
- Live webhook rejected while the process role is shadow.
- Separate shadow SQLite database.
- Only one shadow observation worker starts.
- Live reconciliation, management, discovery, state refresh, and control workers do not start.
- CrossTradeClient blocks every non-GET request before rate limiting or HTTP transport in shadow.
- Broker reads are disabled by default.
- Shadow results never write live trade, attempt, ASW, Silver-intent, control-fence, or circuit state.

## Automated validation

Mode-correct test commands were run against the packaged source:

- Live contract and regression suite: **315 passed**.
- Shadow safety/observer/integration suites: **18 passed**.
- Total: **333 passed**.

The only emitted messages were existing FastAPI/Starlette deprecation warnings.

## Pine compatibility audit

The uploaded Pine v1.2.4 build's current Router pipe messages parse under this release, and its
Direct commands include `destination=tradovate`. Ordinary modeled TP/SL exit alerts are suppressed
in Direct mode so broker brackets remain authoritative. A Direct and Shadow feed require two
separate TradingView alert snapshots using otherwise identical inputs and the combined condition
`Order fills and alert() function calls`.

The separately delivered companion `AutoProp_ICT_Fusion_v1.2.4A_DIRECT_SHADOW_AUDIT_RC1.pine` applies only alert-route
hardening discovered in this audit: ASW safety cancels now emit in both routes, Router/Shadow can
observe modeled ASW TP/SL closures, dead ASW TIME_FLAT messages carry their required contract, and
the unknown-exit fallback is parser-valid. Silver rebracketing now requests the Pine-owned quantity
instead of the account-wide contract cap. Signal, entry, allocation, stop, and target formulas are
unchanged from the uploaded v1.2.4 source. Pine compilation must still be performed in TradingView.

Operational limitations are documented in `DEPLOY_V1_2_8_10.md`, including fixed-account Direct
routing, static manual balance/MLL inputs, broad Direct FLATTEN/CANCELANDBRACKET scope, and the
temporary loss of Router zero-touch onboarding while this service is shadow-only.
