# AutoProp Router RC6 Symbol Hotfix — Validation Report

**Date:** 2026-09-10 ET  
**Source:** user-downloaded production GitHub ZIP `autoprop-router-main.zip`  
**Source ZIP SHA-256:** `1b49f63196c31d062ad60fcfb74e950c67292d2e44235d3b27ff1a28283b246d`  
**Source version:** `1.2.7-exact-parity-production-rc6`  
**Hotfix version:** `1.2.7.1-exact-parity-production-rc6-symbol-hotfix`

## Incident reproduced from production evidence

The 2026-09-10 ORG entry reached the durable RC6 webhook inbox and was processed once. All eight destinations returned the same CrossTrade HTTP 400 because RC6 submitted the bare root `MNQ` as the broker instrument. CrossTrade requires a routable futures symbol such as `MNQ1!` or a dated Tradovate contract.

The defect was found directly in active production source:

- `execution.py::Executor.place_single()` hard-coded `instrument: "MNQ"`.
- `execution.py::Executor.place_core()` hard-coded `instrument: "MNQ"`.
- multiple protection/failsafe flatten calls used bare `MNQ`.
- `settings.DEFAULT_EXECUTION_SYMBOL` was already `MNQ1!`, but the active Executor ignored it.

This means the defect was common to ORG, Silver, TGIF, DWC, and Core entry transport, not an ORG signal-logic defect.

## Hotfix scope

No Pine code is changed. No engine qualification, arbitration, sizing math, targets, stops, consistency math, MLL math, daily accounting, Core management formulas, Silver management formulas, or ORG re-entry proof logic is changed.

### 1. Central MNQ broker symbol normalization

`crosstrade.py` now defines a fail-closed `normalize_tradovate_symbol()` transport contract:

- `MNQ` -> `MNQ1!`
- `MNQ1!` -> `MNQ1!`
- explicit dated MNQ Tradovate symbols such as `MNQU6` remain valid
- non-MNQ symbols fail closed with `InstrumentContractError`

The CrossTrade boundary normalizes PLACE, single-position read, and FLATTEN operations. This is defense-in-depth even if an upstream caller supplies the root symbol.

### 2. Executor and live router use one normalized execution symbol

`execution.py` now carries one normalized `execution_symbol` and uses it for:

- all four single-target engine entry payloads: ORG / SILVER / TGIF / DWC
- Core entry payloads
- emergency protection-failure flatten operations

`live.py` initializes the symbol from `DEFAULT_EXECUTION_SYMBOL`, passes it to the Executor, and uses the same value for position reads and all live/failsafe flatten operations.

### 3. Global symbol-contract failure short-circuit

A definitive CrossTrade instrument-translation error is classified as `InstrumentContractError`. On an ENTRY fanout, the first such error stops further broker mutation attempts for the remaining enabled accounts. Remaining destinations are marked `ERROR` with a global-instrument-contract abort reason.

There is **no automatic trade replay**. Existing per-account dedupe and ambiguous-PLACE protections remain unchanged.

### 4. Honest webhook execution telemetry without changing retry semantics

The durable inbox processing status remains `DONE` when the worker completed normally. This is intentionally separate from broker execution success so the hotfix does not accidentally convert a terminal event into an automatically replayable event.

ENTRY results now include an `execution_summary`:

- `ROUTED`
- `PARTIAL_DESTINATION_FAILURE`
- `ALL_DESTINATIONS_FAILED`
- `NO_ROUTES_WITH_EXECUTION_FAILURES`
- `NO_DESTINATIONS_ELIGIBLE`

The `/admin/webhook-inbox/<token>` endpoint also annotates already-stored RC6 results at read time, so the 2026-09-10 eight-account HTTP-400 incident is surfaced as `ALL_DESTINATIONS_FAILED` without rewriting history.

Railway application logs now emit ERROR/WARNING/INFO lines for ENTRY execution outcomes and stack traces for worker exceptions. Raw webhook bodies and secrets are not logged by this patch.

### 5. Readiness/health contract

`readiness.py` validates `DEFAULT_EXECUTION_SYMBOL` before reporting configuration ready. `/health` now includes the normalized non-secret `execution_symbol`, expected to be `MNQ1!`.

## Files intentionally changed

Production code:

- `crosstrade.py`
- `execution.py`
- `live.py`
- `main.py`
- `readiness.py`
- `version.py`

Regression test added:

- `test_rc6_symbol_hotfix.py`

No database schema changes were made. `store.py` is byte-identical to RC6.

## Critical unchanged files — byte identity versus uploaded RC6

- `allocation.py` — `2548f2de6a02b5c085a5671bb71f77f68913968e2b7ebec56fac3f4c09edf20b`
- `parity.py` — `68440c7f09931cd7a25e46c74522547b3f306dc048f52056c76349ccaf403566`
- `management.py` — `7de3f59464f385d1a12c41b54c9b72ccdb1e4e86e8b1cc7a53d69eb43db681f7`
- `events.py` — `923b56e9ce8b798cd7e1a02c8f2f1381fbd1a84a7f62d5a22f5d27f2af2564b3`
- `state.py` — `d2c7025128423df838fc4b584668171d0afb813c296f820c3c8acead81ac0c6c`
- `state_refresh.py` — `708ea93513469293fc317dba13c65b5d2b728370890f3743beb4aecddacfde76`
- `models.py` — `bd71d684bbfbf8c3eba5ef2641ec9ca38732421df05c6174ea96ffc5011069c3`
- `config.py` — `a9033285dc896a1bd3a638bc6c31451aa39d6064852a12a42ae708a4f472ebfa`
- `service.py` — `461f0edf413087e2b10207a58e7a04f1c352fbfb4f4f70ba727104b4d4cf6ba3`
- `org_reentry.py` — `5404065fcf367d2e053a28924037a7f4deaa1eecebbe2b8b1708e9862be52ef6`
- `store.py` — `7bcb8ef7e9ed113bd714b54c47f5659675fb14852e959ac1a33f105601ffadbd`

This preserves the locked account formulas and lifecycle/management behavior outside the transport seam.

## Validation completed

### Compile

`python -m compileall -q .` — **PASS**

### New hotfix regression suite

`pytest -q test_rc6_symbol_hotfix.py` — **14 passed**

Coverage includes:

- root/continuous/dated MNQ normalization
- non-MNQ fail-closed behavior
- CrossTrade PLACE/position/flatten boundary never emitting bare MNQ
- remote CrossTrade translation HTTP 400 classification
- ORG/Silver/TGIF/DWC entry payloads = `MNQ1!`
- Core entry payload = `MNQ1!`
- emergency flatten = `MNQ1!`
- exact eight-account global-symbol failure stops after one definitive broker attempt
- old RC6 eight-account HTTP-400 inbox result is displayed as `ALL_DESTINATIONS_FAILED`
- intentional all-SKIP result remains `NO_DESTINATIONS_ELIGIBLE`
- readiness accepts `MNQ`/`MNQ1!` normalization and blocks an invalid non-MNQ symbol
- admin webhook-inbox endpoint backfills the legacy incident summary

### Existing active exact-parity/regression subset

The downloaded repository contains a pre-existing test packaging mismatch: many tests import `app.*` even though the production GitHub ZIP is a flat-module deployment, and several older legacy tests target superseded APIs. The unmodified uploaded RC6 therefore does not cleanly collect under a raw `pytest -q` from this ZIP.

To avoid changing unrelated production/test architecture, the active exact-parity suites were run in a temporary validation harness using the same flat imports as Railway. Results:

- uploaded RC6 baseline: **134 passed**
- patched RC6 hotfix: **134 passed**
- `test_formula_parity.py` within that set: **102 passed** against the bundled independent golden oracle

No pre-existing active regression changed outcome.

### Startup smoke

Local Uvicorn startup — **PASS**  
`GET /health` — **PASS**  
With `DEFAULT_EXECUTION_SYMBOL=MNQ`, health reports normalized `execution_symbol="MNQ1!"` and keeps broker mutation disarmed while the TradingView contract gate is false.

## Deployment safety sequence

1. Keep/set Railway `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false` before code deployment.
2. Deploy this exact hotfix source.
3. Verify `/health` reports:
   - version `1.2.7.1-exact-parity-production-rc6-symbol-hotfix`
   - `execution_symbol: "MNQ1!"`
   - `configuration_ready: true`
   - `broker_mutation_armed: false` while staged
   - expected registered account count
4. Verify `/admin/live-readiness/<token>` has no problems and all expected account state remains verified.
5. Verify `/admin/webhook-inbox/<token>` now labels the Sep-10 ORG incident `ALL_DESTINATIONS_FAILED` in its execution summary.
6. Do **not** replay the old ORG entry.
7. Re-arm only after all staging checks are clean by restoring `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true`.
8. The first new market entry remains the acceptance test. Confirm CrossTrade Alert History shows `MNQ1!`-resolved Tradovate orders for the intended accounts, correct per-account quantities, and correct broker protection.

## Rollback

Rollback target is the exact uploaded RC6 source/version `1.2.7-exact-parity-production-rc6`. Keep the TradingView contract gate false during rollback. Do not replay an old queued ENTRY as part of rollback.

## Promotion status

**Offline hotfix validation: PASS.**  
**Production promotion: NOT performed in this chat.**  
This hotfix is a surgical transport/observability branch from RC6. It does not merge or supersede the separately staged RC7B Auto-Onboarding or Prop Risk Lifecycle work.
