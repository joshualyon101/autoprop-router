# AutoProp Router v1.2.2 Exact Parity Production RC1 — Validation Report

Date: 2026-09-07 ET

## Release status

**Production-wired, staged-first RC.** Broker mutation is controlled by `TRADINGVIEW_ALERT_CONTRACT_VERIFIED` and defaults false.

## Automated validation

```text
python -m compileall -q .                    PASS
pytest -q                                     150 passed
FastAPI/settings startup smoke                PASS
Independent exact-parity Golden Oracle        35 passed
```

## New production hardening validated in this promotion

- production webhook reaches `LiveRouter` only after final gate is true;
- final gate false permits staging/readiness while preventing mutation;
- parent Tradovate order ID is retained from accepted/reconciled PLACE;
- destination entry fill is read from fills scoped to that parent order;
- weighted average fill is used when an entry fills in multiple executions;
- duplicate fill rows are deduped;
- exact routed fill quantity must be proven;
- accepted entry with unprovable fill state is flattened rather than managed from planned Pine price;
- Core target tier ownership is identified from absolute target ordering by side, so entry slippage cannot swap TP1/runner tiers;
- Core child ownership cannot be satisfied by unrelated pre-existing orders;
- ambiguous PLACE is never resent blindly;
- linked CrossTrade accounts are compared with configured registry;
- versioned SQLite database avoids legacy schema collision;
- dedupe rows are pruned by retention policy.

## Pine gate consumed by this release

AutoProp ICT Fusion v1.1.5 DUAL ROUTE RC3 compiled with zero errors and reproduced the locked system on TradingView's currently available rolling history: 677 trades, +$79,017.20, PF 1.967, max intrabar DD $6,528.95, max contracts 20. The three-trade difference versus the frozen 680-trade benchmark is attributable to the earlier Sep 6–9 2023 1-minute history rolling out; DD and max-contract exposure match exactly and headline metrics reconcile to the frozen reference.

## Still required before final gate=true

1. Preserve/export the currently deployed account registry/risk state.
2. Deploy this package with one Railway replica and `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`.
3. Confirm all enabled account rules are verified.
4. Confirm each configured CrossTrade account is linked/discovered.
5. Confirm fresh closed cash and zero/expected open position.
6. Confirm current prop MLL/failure floor is verified.
7. Confirm durable-fill ledger coverage has been independently verified.
8. Confirm `/admin/live-readiness/<token>` has `problems=[]`.
9. Create a fresh TradingView RC3 Router alert.
10. Only then set final gate true.
11. Watch the first accepted live trade end-to-end before unattended use.
