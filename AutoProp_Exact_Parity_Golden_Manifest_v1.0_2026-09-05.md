# AutoProp Exact Formula / Allocation Parity — Golden Manifest v1.0

**Date:** 2026-09-05 ET  
**Purpose:** Deterministic release specification for AutoProp ICT Fusion → AutoProp Router exact parity.  
**Status:** Verification specification only. **Not authorization to deploy or arm live execution.**  
**Controlling release candidates:** AutoProp ICT Fusion v1.1.4 Exact Formula Parity RC1 + AutoProp Router v1.2.0 Exact Formula Parity RC1.  
**Live contract gate:** `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false` until every mandatory gate passes.

---

## 1. Authority and evidence hierarchy

When two artifacts disagree, use this hierarchy until a newer release is explicitly validated and locked:

1. Exact locked five-engine TradingView benchmark and the exact v1.1.1 Universal Customer RC1 source lineage.
2. AutoProp Master Continuation Handoff v23 Exact Formula / Allocation Parity.
3. Directly re-audited v1.1.1 source formulas and lifecycle semantics.
4. Current CrossTrade/Tradovate API documentation for broker capabilities and reconciliation primitives.
5. Older research branches only as provenance. They must never override the locked production behavior.

Known v1.1.1 SHA-256:

`6bb380dde59866f455bc54d31176ad2c78f594ddfedfc20357506af117a3a35b`

The v1.1.4 Pine and v1.2.0 Router candidate files are not locally available in the continuation runtime as of this manifest. Their prior hashes/test results remain lineage claims until those exact artifacts are recovered and re-audited.

---

## 2. Frozen benchmark gate

TradingView must reproduce all of the following before Router deployment changes:

| Item | Required value |
|---|---:|
| Symbol | `CME_MINI:MNQ1!` |
| Host timeframe | 1 minute |
| Chart | Standard candles / full futures session |
| Backtest range | Sep 6 2023 – Sep 4 2026 |
| Trade legs | **680** |
| Net profit | **+$78,944.00** |
| Profit factor | **1.962** |
| Percent profitable | **42.50%** |
| Max intrabar drawdown | **$6,528.95** |
| Max contracts held | **20** |
| Margin calls | **0** |

Any mismatch is a STOP condition. Do not compensate in Router code for a Pine benchmark mismatch.

---

## 3. Architecture invariant

Pine/Fusion owns:

- signal qualification;
- engine eligibility;
- setup geometry;
- cross-engine arbitration;
- the single canonical opportunity stream.

Router owns, independently for each destination account:

- current closed cash balance and account rule state;
- MLL/failure-floor state;
- base risk budget;
- per-account quantity;
- per-account consistency quantity/target clipping;
- Core TP1/runner allocation;
- broker order creation/protection/readback;
- destination-specific management state;
- destination-specific ORG re-entry eligibility based on that destination's proven broker outcome.

**Forbidden:** Router discovers an alternate setup, replaces the canonical trade, copies one account's quantity or target split to another, or changes engine thresholds to simplify routing.

If an account cannot safely take the canonical trade, that destination **skips** it.

---

## 4. Numeric conventions that must match Pine

### 4.1 MNQ constants

- Point value: `$2.00` per point per MNQ.
- Minimum tick: `0.25` point.
- Tick value: `$0.50` per MNQ.

### 4.2 Contract rounding

All frozen risk-based quantity calculations use Pine-equivalent nearest-whole-contract rounding.

For nonnegative quantity inputs, the Router oracle must implement:

`pine_round_positive(x) = floor(x + 0.5)`

This is required because Pine `math.round()` rounds ties upward. Do **not** use Python's built-in `round()` for sizing because Python uses ties-to-even.

Required regression cases:

| Raw qty | Pine expected |
|---:|---:|
| 2.49 | 2 |
| 2.50 | 3 |
| 2.51 | 3 |
| 0.49 | 0 |
| 0.50 | 1 |

A post-formula Router cap is not allowed unless it is part of the frozen account/engine formula. `LIVE_MAX_QTY_PER_ACCOUNT=0` remains intentional.

### 4.3 Tick clipping

Where Pine uses `floor()` to convert remaining profit room to ticks, Router must also floor. Do not nearest-round clipped targets.

---

## 5. Challenge risk engine

### 5.1 Current-cushion risk percentage

| Drawdown family | Standard | Aggressive |
|---|---:|---:|
| Static | 15.00% | 32.50% |
| EOD trail | 18.75% | 19.25% |
| Live trail | 18.75% | 19.75% |

`rawRisk = currentUsableCushion × basePct`

`riskCap = originalMaxLoss × basePct × 1.30`

`baseRisk = min(rawRisk, riskCap)`

At progress `>= 80%` of the **effective target**:

- Static Standard: `baseRisk × 0.75`
- every other frozen profile: `baseRisk × 0.85`

### 5.2 Effective target and consistency planning

Actual challenge target with consistency:

`effectiveTarget = max(originalProfitTarget, largestWinningDay / actualConsistencyFraction)`

Standard profile planning ceiling = actual rule.

Aggressive planning ceiling:

- 30% rule → 40%
- 40% rule → 50%
- any rule >=50% → actual rule; never intentionally above 50% from this transfer rule.

### 5.3 Challenge golden vectors

Assume original Max Loss = `$3,000`, usable cushion = `$3,000`:

| Profile | Raw | Cap | Base | Near-pass expected |
|---|---:|---:|---:|---:|
| Static Standard | 450.000 | 585.000 | 450.000 | 337.500 |
| EOD Standard | 562.500 | 731.250 | 562.500 | 478.125 |
| Live Standard | 562.500 | 731.250 | 562.500 | 478.125 |
| Static Aggressive | 975.000 | 1,267.500 | 975.000 | 828.750 |
| EOD Aggressive | 577.500 | 750.750 | 577.500 | 490.875 |
| Live Aggressive | 592.500 | 770.250 | 592.500 | 503.625 |

Cap-binding vector: EOD Standard, cushion `$6,000`, Max Loss `$3,000`:

- raw = `$1,125.00`
- cap = `$731.25`
- base = `$731.25`
- near-pass = `$621.5625`

Effective-target vector:

- original target = `$6,000`
- largest winning day = `$3,000`
- actual consistency = `40%`
- expected effective target = `max(6000, 3000/.40) = $7,500`

---

## 6. Funded risk engine

### 6.1 Pre-lock participation risk

Percentages are of **original Max Loss**, not current cushion:

| Current cushion as % Max Loss | Standard | Aggressive |
|---|---:|---:|
| >=75% | 15.00% | 16.50% |
| >=50% and <75% | 13.75% | 15.125% |
| <50% | 12.50% | 13.75% |

Aggressive is Standard × `1.10`.

For Max Loss `$3,000`:

| Tier | Standard risk | Aggressive risk |
|---|---:|---:|
| >=75% | $450.00 | $495.00 |
| >=50% | $412.50 | $453.75 |
| <50% | $375.00 | $412.50 |

### 6.2 Post-lock base risk

`healthyFloor = originalMaxLoss × 15%` Standard, or that floor ×1.10 Aggressive.

`scaledRisk = retainedCushion × 12.5%` Standard, `×15%` Aggressive.

`postLockBaseRisk = max(healthyFloor, scaledRisk)`

Vector: Max Loss `$3,000`, retained cushion `$4,000`:

- Standard = `max(450, 500) = $500`
- Aggressive = `max(495, 600) = $600`

### 6.3 Low-cushion survival mode

Before lock, if cushion `<30%` of Max Loss:

`oneContractMaxRisk = cushion × 35%`

Allow exactly one MNQ only when structural one-contract risk is `<= oneContractMaxRisk`.

Vector: Max Loss `$3,000`, cushion `$600`:

- threshold = `$210`
- structural risk `$210` → qty 1
- structural risk `$210.01` → qty 0

### 6.4 Core funded minimum-loss-buffer safety

`safeQty = pine_round_positive((cushion / 12) / riskPerContract)`

`finalCoreQty = min(normalCoreQty, safeQty)`

Vector: cushion `$1,800`, risk per contract `$50` → `(1800/12)/50 = 3.0` → safe qty `3`.

Tie vector: cushion `$1,500`, risk per contract `$50` → `2.5` → Pine expected safe qty `3`.

---

## 7. Daily loss and consistency semantics

### 7.1 Daily boundary

Daily-loss/consistency state resets on the **New York calendar day**.

Do not use:

- the 18:00 futures trading-day boundary;
- CrossTrade/Tradovate `tradeDate` as a substitute for the AutoProp NY calendar-day key.

Broker `tradeDate` may be stored for reconciliation metadata, but the AutoProp risk-day key must follow the locked Pine New York date semantics.

### 7.2 Daily loss limit

`dailyLossLimit = activeBaseRiskBudget × 1.8`

Vector: active base risk `$562.50` → daily loss limit `$1,012.50`.

### 7.3 Consistency room

`room = max(0, baseTarget × planningCeiling - realizedPnlToday)`

Negative realized P&L is **not clamped to zero**.

For base target `$6,000`, ceiling `40%`:

| Realized today | Room | Entry allowed by room floor? |
|---:|---:|---|
| -$500 | $2,900 | Yes |
| $2,300 | $100 | Yes |
| $2,350 | $50 | **No** (`<= $50`) |
| $2,400 | $0 | No |

### 7.4 Single-target consistency quantity

If room `<= $50`, qty = 0.

Otherwise:

`fitQty = floor(room / projectedProfitPerContract)`

- if `fitQty >=1`, final qty = `min(preQty, fitQty)`;
- if none fits but room is above `$50`, frozen logic can force qty `1`, then clip target geometry.

### 7.5 Single-target forced-one target vector

Assume room `$60`, preQty >0, natural projected profit `$100` per contract, one MNQ, point value `$2`, tick `0.25`.

No whole contract naturally fits, so qty = `1`.

Target ticks allowed:

`floor(60 / (1 × 2 × .25)) = 120 ticks`

Allowed price distance = `120 × .25 = 30 points`, producing exactly `$60` projected profit for one MNQ.

---

## 8. Liquidity Core risk allocation

### 8.1 Score multiplier

| Score | Multiplier |
|---:|---:|
| >=4 | 1.50 |
| 3 | 1.25 |
| 2 | 1.00 |
| <=1 | 0.75 |

### 8.2 Moderate source multiplier

| Source | Multiplier |
|---|---:|
| PDL / LOH | 1.50 |
| ASH / NYH | 1.00 |
| HPL / PDH | 0.50 |
| other | 1.00 |

Default Core multiplier:

`min(1.50, sourceMultiplier × scoreMultiplier)`

Primary Core exceptions use **score multiplier only**:

- LONG: HPL when enabled, ORL, IBL
- SHORT: ORH, IBH

SC_FVG uses default Core multiplier.

Add-ons:

- LOL_ADD risk multiplier = `1.00`
- PDL_ADD risk multiplier = `0.75`, but production cap = `0`

### 8.3 Multiplier vectors

- PDL score 4 default → `min(1.5, 1.5×1.5) = 1.50`
- HPL score 4 default arithmetic would be `0.75`, but primary LONG HPL exception → `1.50`
- ORH short score 3 primary exception → `1.25`
- PDH score 2 default → `0.50`
- LOL_ADD → `1.00`
- PDL_ADD → qty must remain `0` due module cap

### 8.4 Core module contract caps for prop Challenge / pre-lock Funded

Personal / Real Money Core follows the locked personal path and uses the destination account cap rather than these prop score caps.

| Module / score | Cap |
|---|---:|
| CORE / SC_FVG score <=1 | 3 |
| CORE / SC_FVG score 2 | 4 |
| CORE / SC_FVG score 3 | 5 |
| CORE / SC_FVG score >=4 | 6 |
| LOL_ADD | 4 |
| PDL_ADD | 0 |

After funded lock, applicable CORE/LOL modules may use the destination account's frozen firm maximum as defined by production logic.

---

## 9. Destination-specific Core allocation

After **that destination's** final quantity is known:

`runnerQty = floor(totalQty / 2)` when totalQty >=2, else `0`

`tp1Qty = totalQty - runnerQty`

Required vectors:

| Total | TP1 | Runner |
|---:|---:|---:|
| 1 | 1 | 0 |
| 2 | 1 | 1 |
| 3 | 2 | 1 |
| 4 | 2 | 2 |
| 5 | 3 | 2 |
| 6 | 3 | 3 |
| 11 | 6 | 5 |
| 25 | 13 | 12 |
| 50 | 25 | 25 |

Never proportionally copy the canonical source account's split.

---

## 10. Core consistency size-first pipeline

For each candidate quantity from `1..preConsistencyQty`:

1. Compute that candidate's TP1/runner split.
2. `projected = tp1Qty × max(0,tp1ProfitPerContract) + runnerQty × max(0,tp2ProfitPerContract)`.
3. Keep the largest candidate where `projected <= room + 0.0001`.
4. If none fits and room > `$50`, result can still be `1`.

Integration vectors using TP1 profit `$20/contract`, TP2 profit `$40/contract`, preQty 4:

| Room | q1 projected | q2 | q3 | q4 | Expected final qty | Split |
|---:|---:|---:|---:|---:|---:|---|
| $121 | 20 | 60 | 80 | 120 | 4 | 2/2 |
| $100 | 20 | 60 | 80 | 120 | 3 | 2/1 |
| $70 | 20 | 60 | 80 | 120 | 2 | 1/1 |
| $50 | — | — | — | — | 0 | blocked |

### 10.1 Forced-one Core target clip

To test the otherwise narrow forced-one path, use:

- room = `$55`
- preQty >=1
- natural TP1 profit = `$60/contract`
- natural TP2 profit >= `$60/contract`

No candidate naturally fits but room is > `$50`, so final qty = `1`, runner = `0`.

Common target ticks:

`floor(55 / (1 × $2 × .25)) = 110 ticks`

Price distance = `27.5 points`, projected profit = `$55`.

### 10.2 Isolated target-function regression

The target-clipping function itself must also be unit-tested independently even if normal size-first integration usually reduces quantity first.

Example: entry `100`, natural TP1 `110`, natural TP2 `120`, qty `4`, split `2/2`, room `$100`:

- natural TP1 per contract = `$20`
- natural TP2 per contract = `$40`
- projected natural = `$120`
- `room >= qty × TP1Pc` → `$100 >= $80`, so TP1 stays `110`
- runner room = `$100 - (2×$20) = $60`
- runner ticks = `floor(60 / (2×2×.25)) = 60`
- clipped TP2 = `100 + 60×.25 = 115`
- projected blended profit = `$100`

This is a unit test for formula parity, not an assertion that the full size-first pipeline would retain qty 4 at room $100.

---

## 11. Engine-specific quantity caps / semantics

- ORG pre-lock/non-funded-lock cap: `8 MNQ`.
- Silver pre-lock/non-funded-lock cap: `30 MNQ`.
- Funded after lock: ORG and Silver may use the destination account's true max according to frozen production logic.
- TGIF/DWC: common account-risk sizing then account maximum; TGIF's canonical `q==1` computation is a setup-quality classifier, **not** the final live quantity.

---

## 12. Exact live management state machines

### 12.1 Silver

At favorable progress `+3.0R`, stop tightens to approximately `+0.50R`.

A Router update may tighten a stop; it must never loosen an already safer live stop.

### 12.2 Core — observed market clock

Management decisions occur on completed **native 5-minute pulses**, not arbitrary Router wall-clock intervals and not every 1-minute host update.

Pine telemetry must therefore emit a generic, trade-independent `MARKET_PULSE` every completed native 5-minute pulse containing the observed market fields needed by Core management. The pulse must continue even when the canonical TradingView Core position is no longer active.

At minimum the pulse contract must carry enough immutable market state for Router to evaluate every active destination Core allocation without using the canonical source trade's lifecycle, including the completed native 5m bar identity/time, high/low as needed for stage tests, ATR, and runner swing references used by the frozen formula.

### 12.3 Core pre-TP1 stop stages

Only after the destination's native entry bar:

1. Check 75% threshold **first**.
2. If reached, stop → breakeven (frozen offset 0).
3. Else if 50% reached, stop → halfway between entry and initial stop.

A single pulse that crosses both thresholds must end at the 75%/breakeven state, not the 50% state.

### 12.4 Core TP1 transition

For each destination independently:

If:

`previousBrokerPositionAbs > runnerQty` and `currentBrokerPositionAbs <= runnerQty`

then mark TP1 filled and move runner stop to:

- LONG: `entry + 1 tick`
- SHORT: `entry - 1 tick`

Do not infer TP1 from the canonical source account's position or alert lifecycle.

### 12.5 Core runner trail

On each completed native 5m pulse after TP1:

LONG:

`newStop = max(currentStop, completed5mRunnerSwingLow - 0.10 × completed5mATR)`

SHORT:

`newStop = min(currentStop, completed5mRunnerSwingHigh + 0.10 × completed5mATR)`

The Router must only tighten, never loosen.

---

## 13. ORG re-entry — destination outcome proof

A canonical ORG re-entry is not a blanket command to every destination.

Destination account may participate only if Router can prove all of:

1. It participated in the immediately preceding canonical ORG attempt.
2. The specific prior allocation/order identity is unambiguous.
3. Durable/current fill and order evidence proves that destination's prior attempt ended by stop, consistent with the frozen ORG stop/re-entry path.
4. No evidence shows the destination instead completed by its own clipped target/other non-stop outcome.
5. Broker-history coverage for the relevant interval is known sufficient.

If proof is missing, stale, contradictory, or history coverage is suspect, **skip re-entry for that destination**. Do not guess.

Durable broker fill history is preferred because current-session fill state can reset around session boundaries/weekends.

---

## 14. Closed cash balance and MLL state

### 14.1 Balance source

Exact risk calculations must use confirmed **closed cash balance**, not net liquidation and not a balance value contaminated by open P&L.

Current CrossTrade Tradovate cash-balance snapshots expose fields including `amount`, `realizedPnL`, `tradeDate`, and `timestamp`. Router must select the exact account's cash-balance row, validate identity, and enforce freshness.

If confirmed cash balance is absent, stale, duplicated/ambiguous, or cannot be tied to the correct account, new entry fails closed.

### 14.2 MLL / failure floor

Cash balance does not itself replace the AutoProp rule engine's MLL/failure-floor state. Router must maintain the exact firm/account rule model and manual override semantics defined for that account.

For live trailing/EOD/static models, risk cushion is based on the confirmed closed balance and the confirmed/current modeled MLL floor as appropriate.

### 14.3 Daily realized P&L boundary

Do not silently treat broker `tradeDate` as AutoProp's NY calendar date. Reconstruct or maintain the NY-day realized state using a verified NY-date baseline and broker fills/cash-state as required.

---

## 15. Broker protection and order normalization

### 15.1 Single-target engines — ORG / Silver / TGIF / DWC

Use broker-hosted absolute protective prices:

- `takeProfit=<absolute price>`
- `stopLoss=<absolute price>`

After placement, Router must read back and prove:

- correct destination account;
- correct symbol;
- correct side;
- full-position protective stop coverage;
- exact routed stop price at tick precision;
- exact routed target price at tick precision;
- expected working state/order identity.

If exact protective state cannot be confirmed after bounded reconciliation, flatten the destination.

### 15.2 Core multi-tier bracket

Required sequence:

1. Place immediate native multi-tier ATM protection so the filled position is not intentionally naked.
2. Discover the actual broker child target/stop orders tied to the accepted parent/allocation.
3. Normalize child quantities/prices to the exact destination-specific absolute TP1, TP2, and stop.
4. Read working orders back.
5. Verify exact tick prices, quantities, account, symbol, sides, OCO/protective relationship, and total stop coverage.
6. Retry only within bounded, deterministic reconciliation logic.
7. If exact state cannot be proven, flatten that destination.

The production call graph must prove that `_normalize_core_multibracket()` or its final successor is actually invoked on every applicable accepted Core allocation. A defined-but-unused helper is a release blocker.

### 15.3 Ambiguous mutation rule

An ambiguous PLACE/CHANGE/CANCEL response must never trigger a blind resend that could duplicate exposure.

Reconcile using stable custom order identity, live order/lifecycle state, and fills. If acceptance is proven, reconstruct/protect. If safe reconstruction cannot be proven, flatten exposure rather than duplicate it.

---

## 16. State freshness and fail-closed matrix

| Condition | New entry behavior | Existing exposure behavior |
|---|---|---|
| Closed cash balance stale/missing | Skip | Maintain/verify protection; escalate if state affects safety |
| MLL/floor state stale/ambiguous | Skip | Fail toward less exposure |
| Canonical signal malformed | Skip | No new mutation unless explicit safe flatten |
| Duplicate webhook/event | Dedupe | No duplicate order |
| Broker PLACE ambiguous | Do not resend blindly | Reconcile; protect or flatten |
| Protective child not found | No further risk | Bounded reconcile; flatten if unresolved |
| Protective price/qty mismatch | No promotion | Correct + verify; flatten if unresolved |
| Core market pulse stale/missing | No new Core management transition based on guess | Preserve safer current stop; fail closed |
| ORG prior-outcome proof absent | Skip ORG re-entry for that destination | N/A |
| Fill-history coverage gap | Skip outcome-dependent action | N/A |
| Account disabled/Closing Only | Honor CrossTrade gate | No new opening exposure |

---

## 17. Router golden regression suites

The exact candidate must pass, at minimum:

### Formula suite

- Challenge six-profile matrix.
- Challenge 1.30× cap binding.
- >=80% near-pass boundary at just below / equal / above 80%.
- Effective consistency target.
- Standard vs Aggressive planning ceilings.
- Funded 75% and 50% tier boundaries.
- Aggressive ×1.10.
- Post-lock floor-vs-scaled max.
- <30% one-contract survival boundary.
- 12R Core safety quantity.
- Pine `.5` tie-up rounding.
- NY daily reset semantics.
- negative-day consistency room.
- exact `$50` no-new-trade boundary.

### Allocation suite

- ORG cap 8 pre-lock.
- Silver cap 30 pre-lock.
- Core source/score exceptions.
- CORE/SC_FVG/LOL_ADD/PDL_ADD caps.
- funded post-lock account maximum.
- destination Core TP1/runner split for odd/even quantities.
- single-target consistency force-one target clip.
- Core size-first quantity matrix and forced-one path.

### Management suite

- Silver +3R → +0.5R.
- Core 75%-before-50% on same pulse.
- no pre-TP1 stop stage on native entry bar.
- per-destination TP1 detection from broker position reduction.
- runner BE ±1 tick.
- completed-5m-only runner trail.
- no stop loosening.
- generic MARKET_PULSE continues after canonical source Core exit.
- two destinations at different lifecycle stages process same market pulse independently.

### Broker/reconciliation suite

- exact single-target bracket readback.
- Core multibracket normalization call-site coverage.
- child-order discovery with deterministic identity.
- wrong child quantity/price detected.
- partial target filled while runner remains.
- ambiguous PLACE accepted vs rejected recovery.
- idempotent webhook retry/dedupe.
- durable fill-history ORG stop proof.
- destination target outcome correctly blocks ORG re-entry.
- history coverage gap blocks ORG re-entry.
- stale cash snapshot blocks new entry.
- cash `tradeDate` does not reset NY daily state incorrectly.

---

## 18. Release gates

### Gate 1 — Pine compile

`0 errors`

### Gate 2 — exact TradingView benchmark

`680 legs / +$78,944.00 / PF 1.962`, with win rate, drawdown, and max contracts matching the locked benchmark.

### Gate 3 — Router exact regression

Every suite in Section 17 passes against the recovered exact v1.2.0 candidate. Compile/import/startup pass is necessary but not sufficient.

### Gate 4 — deployment/readiness while still disarmed

- expected Router version and management mode;
- all seven accounts cached/registered;
- state polling healthy;
- no readiness problems;
- `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false` remains false.

### Gate 5 — rebuild TradingView alert from exact validated Pine

Condition: `AutoProp Fusion` → `Order fills and alert() function calls`  
Message: `{{strategy.order.alert_message}}`  
Webhook: production Router endpoint  
No wrapper JSON.

### Gate 6 — final contract arm

Only after all offline/configuration gates pass may `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true` be restored.

### Gate 7 — first market-open acceptance

Verify on the first accepted live trade:

- exact intended destination set;
- exact per-account quantity;
- exact per-account targets/runner split;
- exact protective stop/target working orders;
- no duplicate entry;
- correct fill reconciliation;
- correct Silver/Core management transitions;
- any outcome-dependent ORG re-entry behaves per destination.

A failure at Gate 7 requires re-disarm and diagnosis; it is not a reason to relax the parity rules.

---

## 19. Current blocker and next action

**Current blocker:** the exact v1.1.4 Pine and v1.2.0 Router package referenced by the controlling handoff are not available in the active continuation runtime/File Library search.

Therefore:

- do not deploy;
- do not arm;
- do not reconstruct production candidates from memory;
- do not claim the prior 376-test result is revalidated here.

Once the exact artifacts are recovered, run this manifest as a literal source-code and test audit. The Router candidate should be changed only where it demonstrably violates the frozen formula/lifecycle contract.

---

## 20. Change-control rule

This manifest is a parity specification, not a strategy-improvement proposal. No new engine, threshold, entry condition, target concept, risk profile, or convenience behavior may be added during exact-parity release work.
