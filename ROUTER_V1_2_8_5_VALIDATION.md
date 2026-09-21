# AutoProp Router v1.2.8.5 — Low-Latency Entry Hotfix Validation

Date: 2026-09-21

## Release identity

- Version: `1.2.8.5-exact-parity-low-latency-fanout-rc1`
- Execution mode: `live`
- Management mode: `exact_formula_parity`
- Execution symbol: `MNQ1!`
- Standard ENTRY path: one all-account snapshot followed by one concurrent wave of up to eight account submissions
- ASW path: durable, reconciled, and intentionally sequential; ASW working limits are not part of the concurrent market ENTRY wave

## Incident addressed

The prior release could serialize account work and then use a position view that may lag a recent fill. That combination widened the time between the first and last account submission and could treat an accepted entry as flat while broker state was still converging.

This hotfix removes synchronous post-submit readback from the latency-critical market ENTRY wave. Submission acknowledgements are persisted first; fill, position, and protection verification continue asynchronously without resending an ambiguous order.

## Corrected behavior

### Low-latency fanout

- One pooled HTTP client is reused for broker traffic.
- One all-account snapshot supplies the entry preflight and account state inputs.
- Durable per-account entry attempts are prepared before any broker mutation.
- Eligible standard ENTRY orders are released as one bounded concurrent wave, with a maximum concurrency of eight.
- Broker admission control and a shared rate budget still apply; latency reduction does not bypass broker limits.
- The all-account snapshot uses the same fast pacing class as the following mutation wave while still consuming the shared broker token budget.
- PREPARED and PREPARED-to-SUBMITTING persistence are each committed once for the complete wave, rather than once per destination.
- Worker wakeups cannot be lost between an empty queue read and the wait, removing a possible one-second polling delay.
- Dispatch results expose `ingress_to_completion_ms` and `acceptance_spread_ms` for live verification.

### Fill and flat proof

- Position decisions use the plural, fill-reconciled positions view rather than the lag-prone singular position read.
- Accepted submissions enter asynchronous reconciliation for fill, quantity, side, and owned protection.
- A flatten acknowledgement is not treated as proof of flat.
- An attempt or managed trade is not cleared until the fill-reconciled position is zero and every Router-owned order is terminal or absent.
- If a PLACE response lost the order identities, recovery enumerates and cancels every post-preflight working order, requires a second empty working-order read, and only then re-proves the plural position flat.
- Ambiguous PLACE outcomes remain durably quarantined, are reconciled from broker position and working-order state, and are never blindly resent.

### Durable safety boundaries

- Entry attempts survive restarts and retain ownership while their broker outcome is unresolved.
- Durable control fences prevent a delayed submit or promotion from crossing a relevant EXIT, STOP, or hard-flat control.
- Unresolved attempts or an open entry circuit block new risk.
- Startup warms the broker-backed account-state cache before the regular ENTRY worker starts. A failed warmup leaves the circuit open and the Router fail-closed.
- Safety controls and reconciliation remain available while `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`.

### Deployment boundary

- Production requires exactly one Railway replica.
- `railway.toml` starts exactly one Uvicorn worker.
- Uvicorn access logging is disabled because authenticated webhook and admin paths contain the token.
- The accounts must be dedicated to Router execution while armed. Manual positions or orders can invalidate ownership and flat-proof assumptions, especially for Core child-order discovery.

## Required automated validation

Run from the flat project root and require both commands to complete without errors:

```bash
python -m compileall -q .
pytest -q
```

Validated release result: `266 passed, 6 deprecation warnings, 0 failed`.

The regression suite must cover at least:

1. a single all-account snapshot and concurrent standard ENTRY submission wave;
2. no duplicate mutation after an ambiguous PLACE;
3. plural fill-reconciled position proof;
4. durable PREPARED, SUBMITTING, ACCEPTED, and FLATTENING recovery;
5. control-fence behavior before, during, and after a submission wave;
6. owned-order cancellation plus a second flat proof before ownership is deleted;
7. startup cache warmup failure opening the entry circuit;
8. control processing remaining available while new risk is disarmed;
9. one-worker deployment configuration; and
10. existing allocation, exact-formula parity, Core, Silver/SB35, ASW, and account-risk regressions.

## Disarmed production acceptance

Keep `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`. With all eight accounts already drained, require:

- `/health` reports the exact version above, `configuration_ready=true`, `broker_mutation_armed=false`, `entry_circuit_open=false`, `unresolved_entry_attempts=0`, `active_trades=0`, and `asw_pending_limits=0`;
- `/admin/live-readiness/<token>` reports `problems=[]`, eight enabled account states, and `net_position=0` for every account;
- the readiness snapshot reports no open positions or working orders for any configured destination;
- `/admin/entry-attempts/<token>` reports `unresolved=0`; and
- Railway shows exactly one replica, one running Uvicorn worker, and no previous deployment still serving traffic.

The expected staging warning is that the TradingView alert contract gate is false.

## First armed acceptance

After the token rotation and the final armed checks in `DEPLOY_V1_2_8_5.md`, observe the first natural standard ENTRY end-to-end:

- all intended destinations receive the same signal without multi-minute serialization;
- no destination receives a duplicate PLACE;
- `acceptance_spread_ms` and `ingress_to_completion_ms` are recorded in the webhook result;
- accepted attempts may remain pending briefly while asynchronous reconciliation proves fills and protection;
- every attempt becomes a managed active trade or a safe terminal state;
- `entry_circuit_open=false` and `unresolved_entry_attempts=0` after reconciliation; and
- closing the trade leaves every account flat with no Router-owned working order.

Any mismatch, orphan order, unresolved attempt, unexpected circuit, or duplicate mutation is a stop condition: disarm immediately and follow the recovery procedure in the deployment runbook.
