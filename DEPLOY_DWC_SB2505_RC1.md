# AutoProp Fusion — DWC SB25/05 coordinated release

**Pine:** v1.2.7 DWC SB25/05 Protection RC1  
**Router:** v1.2.8.17-dwc-sb2505-rc1  
**Status:** implementation and offline regression complete; TradingView compilation, current-baseline historical validation, and broker acceptance remain pending. Not deployed by this package.

## What to install

- `AutoProp_Fusion_v1.2.7_DWC_SB2505_PRODUCTION_RC1.pine` is the complete customer-facing strategy. The `.txt` file contains identical code for easier copying.
- `AutoProp_Router_v1.2.8.17_DWC_SB2505_RC1_UPLOAD.zip` is a **changed-files upload**, not a new repository. Extract it and upload its contents to the existing router repository root. Do not upload the ZIP itself as the application. Do not delete unrelated repository files.
- `validation/AutoProp_Fusion_v1.2.6D_UPLOADED_CONTROL.pine` is the user's latest uploaded script with only CRLF-to-LF line-ending normalization. It is the control for the merge, not an older research baseline.
- `validation/` contains the source diff, static audit, test logs, and checksums. `tools/apply_router_update.py` is an optional checksum-guarded local installer; it defaults to dry-run.

## Exact rule

DWC remains a short-only specialist with its existing initial 1.5 × completed 15-minute ATR(14) stop and unchanged executable 3R-based target. Account consistency may already shorten that target; this release does not change the target calculation.

On the first confirmed one-minute close at which maximum favorable excursion has reached **+2.50R**, DWC requests a **one-time static stop at +0.50R**. R is frozen from the modeled fill to the original absolute stop. MFE is the lowest price observed since that fill. The stop cannot loosen.

There is no 09:25 time exit, no liquidity target, no partial profit-taking, and no continuous trailing stop. The existing afternoon required-flat behavior remains.

Example, excluding costs: short fill 100, original stop 110, R = 10. A low of 75 activates the rule; the stop becomes 95. A later low of 73 does not move that stop again. The original target remains in force.

**An intended +0.50R stop is not a guaranteed +0.50R realized result.** Bar-close calculation, gaps, broker rejection, delivery delay, tick rounding, fees, and slippage can reduce or reverse the profit. This corrects the earlier overly strong statement that a protected trade cannot become a loser.

## What was preserved

The source is the exact uploaded `Pasted text(20261007-005720).txt`, identified as v1.2.6D. It retains:

- The confirmed final-child Core five-minute packet and one-shot native-close guard.
- Core's complete sweep-to-entry extreme, absolute structural stop after fill, and fill-rebased target logic.
- Always-on ORG breadth sizing and the removal of portfolio daily-loss/count entry locks.
- Existing account drawdown, consistency, contract caps, session gates, position arbitration, entry calculations, and sizing.
- Silver's staged management and guarded Direct transport, the TGIF conditional `ta.lowest` call, Asia Sweep, and chart visuals.

The audit reverses every authorized Pine substitution and recovers the entire uploaded source. Of 105 existing Pine functions, only the DWC entry-message builder changes; one DWC stop-message builder is added. Strategy declaration behavior and the 45 plot/plotshape calls are retained.

## Router behavior

The new wire contract is `DWC_SB25_05_V1`. Each new DWC entry and stop update carries `DWC_ID`, the immutable Pine entry-signal timestamp. A movement can apply only to an owned SHORT DWC trade with that same identity and contract. Legacy untagged DWC trades do not receive the new management automatically. Install only while flat.

Pine owns the canonical +2.5R trigger. On that event the Router calculates each matching destination's protected stop from its **verified actual broker fill** and stored original stop. The source alert quantity and SL are telemetry, not permission to overwrite destination position size or price geometry. Prices are mapped to MNQ ticks; targets are not changed. Different fills can therefore produce slightly different protected prices and realized R. The Router does not independently monitor intrabar MFE at every destination.

The Router uses in-place modification of its stored owned stop IDs, not account-wide cancel-and-rebracket. It checks side, exact position/stop quantity, ownership, lifecycle/readback, and the generation again before saving state. A durable intent and per-account reservation prevent blind duplicate changes following replay or restart. A later hard-flat fence takes priority.

Temporary position/lifecycle read failures keep the existing orders and block new risk while read confirmation is retried with bounded backoff. A reserved change is not resent. Missing or unsafe ownership/topology, an explicit unsafe modification outcome, or expiry of the existing reconciliation timeout can invoke the owned safety-exit path and quarantine new entries. An ambiguous safety exit is not blindly repeated; ownership remains until flat/cleanup is proven. Persistent errors require review.

The service is idle without DWC intents: there is no new continuous market-data feed or idle webhook stream. Shadow mode records the event without broker mutation.

Existing account configuration, automatic-discovery/onboarding policy, environment settings, risk/allocation modules, transport pacing, state fallback, recovery policy, and database records are not replaced. A small new SQLite table stores DWC intents; old serialized trades/attempts load with safe defaults.

## CrossTrade Direct behavior

The initial Direct entry still submits the original stop and target. When DWC protection activates, the script reuses the existing guarded short-position transport used by Silver, with DWC-specific notes. It requests `CANCELANDBRACKET` with the new stop and unchanged target. If the chart close has already crossed the requested stop or target, the existing side-scoped FLATTEN fallback is used instead.

This command cancels all working orders for the specified account/instrument. Use a dedicated AutoProp account/instrument and do not mix manual trades or unrelated strategies there. Pine cannot read broker acknowledgements, establish external ownership, or guarantee exact broker-fill-relative R. The Direct route is not equivalent to Router readback/ownership enforcement.

## Installation and validation

1. **Prepare while all destinations are flat**, with no pending entries, ASW limits, unresolved entry attempts, or pending protection. Save the current Pine inputs, alert settings, and current source. Back up the router database and repository using the normal operational process; do not copy a live SQLite main file without its WAL-consistent backup.
2. Disarm new Router entries with `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`. Do not disable safety exits or remove protective orders from an open position. Do not leave both old and new entry alerts active.
3. Confirm the existing router is v1.2.8.16 at repository commit `81cbaacba330fe8e5c434d329bed66eb8efe12a5`, or verify the per-file baseline hashes in the upload manifest. **Do not overwrite newer router changes.** The optional local installer refuses conflicting file contents.
4. Extract the Router upload ZIP and upload those files into the existing repository root. Keep all other files, environment values, persistent volume, account configuration, and secrets. Keep the deployment at one active process/replica against this SQLite state, as in the current architecture.
5. Verify the service starts and `/health` reports version `1.2.8.17-dwc-sb2505-rc1`, `dwc_stop_contract: DWC_SB25_05_V1`, no pending DWC intent, and no unresolved exposure/circuit issue. While the TradingView gate is false, entry-armed status should remain false; do not mistake that for a failed deployment.
6. Paste the full new Pine into TradingView on **MNQ1!, standard 1-minute candles**. Restore your actual account inputs and matching Strategy Properties. Webhook messages stay OFF during historical validation. The TGIF CW10003 advisory is intentionally not rewritten; a real compile error still blocks promotion.
7. Run the uploaded v1.2.6D control and the merged version on **the same fixed date range, data/session settings, inputs, and Strategy Properties**. This is a current-baseline integration check, not another parameter search. Account for expected downstream equity/sizing/arbitration effects following changed DWC exits. Check unchanged DWC entry/initial-stop/target formulas, the first protection transition, 3R target preservation, one-way stop, and no new 09:25 exits.
8. Do **not** demand the old X5 totals of 726 trades / $81,904.20 from this version. That research used a different older whole-Fusion baseline. Today's Core fixes and ORG changes can change portfolio results. No new whole-system performance claim is made before this current-base comparison.
9. After compilation and historical checks, validate transport on an isolated non-live/paper destination or through the existing nonmutating test workflow. Verify one intended DWC stop event, the unchanged broker target, exact broker stop quantity/price, and no replay mutation. Do not send the example fixtures below to live execution.
10. Recreate the production TradingView alert from the new chart instance: **Order fills and alert() function calls**, message **`{{strategy.order.alert_message}}`**, with the existing correct receiver URL. Router mode retains its configured balance-source behavior; Direct mode still requires Live / Manual Account State. Recheck all account and routing inputs before enabling messages. Only then restore the Router entry gate. Existing alert snapshots do not automatically adopt edited source/settings.

No remote deployment, GitHub commit, broker order, or live-account setting was changed while preparing this package.

## Acceptance limits

Offline result: **546 pytest cases passed**, split across the live-mode suite (545) and the one test requiring default shadow configuration (1). This includes 70 new DWC tests and 476 pre-existing tests. Four existing FastAPI deprecation warnings remain in the live-mode run. Python compileall passed.

Synthetic arithmetic checking also matched the selected X5.C static rule across 10,000 generated paths / 320,000 bar decisions. This is not a market-data backtest, TradingView compile, or live-fill test. The actual compiled Pine token count is unavailable here.

The original research modified four DWC exit outcomes. That is a small set of affected historical outcomes, not evidence that future drawdown will fall by 40%, and not the total number of trades that can activate protection while still reaching TP. Final selection is subject to the current-base and execution checks above.

## Rollback

Rollback only as a coordinated **flat-account** change. Disarm new risk, wait for or explicitly resolve owned exposure and pending DWC intent, then restore both the prior Pine/alert and prior Router runtime files. Do not downgrade a live DWC trade midway through a protection request. Retaining the additive DWC SQLite table is harmless to the older build; do not delete the database to roll back.

## Source documentation consulted

- CrossTrade Cancel and Bracket: https://crosstrade.io/docs/webhooks/commands/cancel-and-bracket
- CrossTrade API Change Order: https://crosstrade.io/docs/api/orders/put-change-order
- TradingView alerts and alert snapshots: https://www.tradingview.com/pine-script-docs/concepts/alerts/
- TradingView broker emulator and strategy timing: https://www.tradingview.com/pine-script-docs/concepts/strategies/

External documentation describes platform behavior. AutoProp-specific behavior and safeguards above are derived from the source and offline tests included in this package.
