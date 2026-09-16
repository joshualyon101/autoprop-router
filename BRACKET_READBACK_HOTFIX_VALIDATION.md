# AutoProp Router v1.2.8.1 — Tradovate Bracket Readback Hotfix

Date: 2026-09-16
Version: `1.2.8.1-exact-parity-six-engine-bracket-readback-hotfix`

## Trigger / live evidence
The first live Silver Bullet event on 2026-09-16 showed:

- `execution_summary.outcome = NO_ROUTES_WITH_EXECUTION_FAILURES`
- `routed = 0`
- Personal account `CT_2181614`: `single-target protective bracket failed owned exact readback; flattened`
- two destinations skipped because two pre-existing working orders were already present
- five destinations returned ERROR with an empty legacy reason string

The Personal broker history simultaneously showed the Silver entry plus the expected stop and target, followed six seconds later by the Router market flatten. This proved the bracket existed but Router verification could not read its exact geometry.

## Root cause
CrossTrade's Tradovate per-account working-orders endpoint returns raw Tradovate Order entities only. It does **not** enrich those rows with OrderVersion quantity, order type, or prices.

The Router v1.2.8 exact-readback code incorrectly assumed those fields were present when calling:
- `verify_single_bracket()`
- `verify_core_bracket()`
- `owned_roles()`
- `change_stop_orders()`
- exact read-after-change verification

Therefore a perfectly valid live broker bracket could fail Router exact verification and trigger a safety flatten.

This is an execution/readback defect, not a Silver signal, sizing, stop, target, or MNQ-symbol defect.

## Hotfix
The Router now uses the documented Tradovate order-lifecycle endpoint for exact broker readback:

`GET /v1/api/tv/accounts/{account}/orders/{id}/lifecycle`

That endpoint returns:
- current Order status;
- highest-id OrderVersion with `orderQty`, `orderType`, `price`, `stopPrice`;
- related commands including the original `clOrdId`;
- command reports.

The Router builds an enriched owned-order snapshot from those fields before exact verification.

Affected paths fixed:
1. single-target bracket verification — ORG / Silver / TGIF / DWC;
2. Core child discovery and exact normalization;
3. target-vs-stop child role ownership;
4. Silver/Core stop-change exact readback;
5. ASW filled-limit protective bracket verification;
6. ambiguous PLACE custom-id reconciliation across current-session Tradovate orders.

The raw working-orders endpoint remains appropriate only for:
- determining whether any working orders exist;
- obtaining working order IDs;
- pre-entry fail-closed working-order gate.

## Additional observability fix
CrossTrade network errors now include the HTTP method/path and exception type when the underlying library supplies an empty exception message. This prevents future Router inbox rows with a useless blank `reason`.

## Unchanged strategy / risk behavior
No changes to:
- Pine;
- six engine signals;
- arbitration;
- Challenge/Funded/Personal formulas;
- ASW whole-plan scaling;
- stops or targets;
- MNQ symbol normalization;
- daily accounting;
- ORG re-entry semantics;
- Core management formulas;
- Silver +3R stop logic.

## MNQ regression
The existing symbol-hotfix suite remains present and passed. The broker boundary still normalizes:
- `MNQ` -> `MNQ1!`;
- `MNQ1!` -> `MNQ1!`;
- valid dated MNQ contracts unchanged;
- non-MNQ instruments fail closed.

## Validation
- `python -m compileall -q .` — PASS
- full Router pytest suite — **168 passed**
- FastAPI import/startup health smoke — PASS
- smoke health execution symbol — `MNQ1!`
- staged broker mutation — false while TradingView contract gate false

New live-shape regression coverage uses the documented Tradovate behavior:
- working-orders rows contain only ID/status;
- lifecycle supplies OrderVersion type/qty/prices;
- Silver exact bracket verification succeeds without flatten;
- owned target/stop roles are recovered;
- Silver stop modification reads back exactly;
- Core normalization succeeds from lifecycle-enriched children;
- all-orders identity envelopes flatten correctly for ambiguous custom-id reconciliation.

## Changed production files vs v1.2.8
- `execution.py`
- `crosstrade.py`
- `version.py`

New regression test:
- `test_tradovate_bracket_readback_hotfix.py`

## Deployment sequence
1. Immediately set `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`.
2. Confirm all accounts are flat and remove/understand any orphan working orders.
3. Deploy v1.2.8.1 with one Railway replica.
4. Verify `/health` reports the v1.2.8.1 version, `execution_symbol=MNQ1!`, configuration ready, broker mutation disarmed.
5. Verify `/admin/live-readiness/<token>` shows all intended accounts flat and no problems.
6. Keep the existing RC4 TradingView Pine/alert; Pine contract did not change.
7. Re-arm by setting only `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true`.
8. Watch the next natural live trade as another acceptance test.

## Separate operational follow-up
The two destinations skipped on the Silver event because each had two pre-existing working orders. Before re-arming, identify those orders in CrossTrade/Tradovate. If CrossTrade Trade Copier is still copying a Router-routed leader into Router-managed destinations, disable that overlap; Router already fans out independently.
