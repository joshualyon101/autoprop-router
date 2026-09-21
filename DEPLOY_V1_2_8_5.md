# AutoProp Router v1.2.8.5 — Drained Production Deployment

This is a disarmed, no-overlap rollout. Do not accept new risk until the old deployment is stopped, the new deployment is the only active instance, and all readiness checks pass.

## 1. Disarm and drain

1. Keep `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false` in Railway. It is already set correctly for this rollout.
2. Pause or disable the TradingView alert while changing the webhook token.
3. Confirm all eight broker accounts are flat and have zero working orders.
4. Require `/health` to show `broker_mutation_armed=false`, `active_trades=0`, `asw_pending_limits=0`, and `unresolved_entry_attempts=0`.
5. Require `/admin/entry-attempts/<token>` to show `unresolved=0`.
6. Save the current read-only `/health`, `/admin/live-readiness/<token>`, and `/admin/entry-attempts/<token>` responses for comparison.

Do not deploy over an active position, pending ASW limit, working owned order, unresolved attempt, or active entry wave. Do not delete or replace the persistent `/data` volume or its SQLite database.

## 2. Replace the source and lock concurrency

1. Extract the release ZIP locally, then replace the GitHub project files with the extracted flat-root contents. Do not upload the ZIP itself as application source.
2. In Railway, set the service to **exactly one replica**.
3. Keep the included `railway.toml`; its start command fixes Uvicorn at `--workers 1`.
4. Keep `--no-access-log` in the included start command. The webhook/admin token is carried in the URL path and must not be echoed into application access logs.
5. Do not start a second service or canary instance against the same accounts or SQLite volume.
6. Deploy while the TradingView contract gate remains false.
7. Wait until Railway shows the old deployment stopped and only the v1.2.8.5 deployment active.

New ENTRY events remain rejected while disarmed, but EXIT, STOP, hard-flat controls, and reconciliation remain available.

## 3. Rotate the exposed webhook token

The prior `AUTOPROP_WEBHOOK_TOKEN` appeared in a screenshot and must be considered compromised.

1. Generate a new strong, unique token.
2. Update `AUTOPROP_WEBHOOK_TOKEN` in Railway while the Router remains disarmed.
3. Let the resulting deployment finish and again confirm only one active replica.
4. Replace the token segment in the TradingView webhook URL with the new token.
5. Do not post the new URL or token in screenshots, logs, GitHub, or chat.
6. Keep the TradingView alert paused until the disarmed validation below is complete.

The old token must no longer authenticate either webhook or admin routes after the rotation.

## 4. Validate while disarmed

Check `/health` and require:

- `version=1.2.8.5-exact-parity-low-latency-fanout-rc1`;
- `execution_mode=live`;
- `management_mode=exact_formula_parity`;
- `execution_symbol=MNQ1!`;
- `configuration_ready=true`;
- `tradingview_alert_contract_verified=false`;
- `broker_mutation_armed=false`;
- `registered_accounts=8`;
- `active_trades=0` and `asw_pending_limits=0`;
- `entry_circuit_open=false`; and
- `unresolved_entry_attempts=0`.

Then call `/admin/live-readiness/<new-token>` and require:

- `problems=[]`;
- all eight enabled accounts are present;
- every `net_position=0`;
- the all-account snapshot contains no open position or working order; and
- the only expected warning is the false TradingView contract gate.

Finally, call `/admin/entry-attempts/<new-token>` and require `unresolved=0`. A `409` saying an entry wave is active is not a pass; remain disarmed and retry after the wave is fully resolved.

## 5. Arm once

Only after all disarmed checks pass:

1. Leave the TradingView alert paused, with its URL already updated to the new token.
2. Set `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true` in Railway.
3. Wait for deployment completion and confirm the previous instance is stopped.
4. Verify exactly one replica and one Uvicorn worker.
5. Recheck `/health`: `tradingview_alert_contract_verified=true`, `broker_mutation_armed=true`, `entry_circuit_open=false`, and `unresolved_entry_attempts=0`.
6. Recheck `/admin/live-readiness/<new-token>` and require `problems=[]` and `broker_mutation_armed=true`.
7. Only now resume the TradingView alert. This prevents a valid signal from being rejected and lost during the arming deployment.

Do not manually trade, place orders, or flatten these eight dedicated accounts while Router is armed. The standard market ENTRY path now fans out concurrently. The ASW working-limit path remains sequential by design and is monitored by its durable reconciler.

## 6. Observe the first natural trade

Watch `/admin/webhook-inbox/<new-token>` and `/admin/entry-attempts/<new-token>` through the first standard ENTRY and exit.

Require:

- one accepted attempt per intended account and no duplicate PLACE;
- a tight `acceptance_spread_ms`, subject to broker admission and rate limits;
- no multi-minute account-by-account submission sequence;
- asynchronous promotion only after fill, side, quantity, and owned protection are proved;
- no open entry circuit or unresolved attempt after reconciliation; and
- zero positions and zero Router-owned working orders after exit.

## Emergency disarm and recovery

If any account is wrong, an attempt remains unresolved, an owned order is orphaned, or the entry circuit opens:

1. Immediately set `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false` and pause the TradingView alert.
2. Do not resend the signal and do not restart repeatedly. Ambiguous PLACE outcomes must remain quarantined while the Router reconciles broker positions and working orders.
3. Inspect `/admin/entry-attempts/<token>`, `/admin/webhook-inbox/<token>`, `/admin/live-readiness/<token>`, and the broker's positions and working orders.
4. Allow the control worker/reconciler to complete. If exposure is immediately unsafe, use the broker's emergency flatten, then cancel all remaining orders for the affected dedicated account and verify flat directly at the broker.
5. Require all accounts flat, no working orders, `active_trades=0`, `asw_pending_limits=0`, and `unresolved=0` before any reset or redeploy.
6. If the circuit remains open after those conditions are true, keep the gate false and call `POST /admin/entry-circuit/reset/<token>`. The reset must return success; it will refuse to clear against non-flat broker state.
7. Run the complete disarmed validation again before considering re-arm.

## Rollback

Rollback is permitted only from a drained state:

1. Keep the gate false and TradingView paused.
2. Prove every account flat and clear, with no managed trade, ASW pending limit, active wave, or unresolved attempt.
3. Deploy the previously known-good source with exactly one replica and one worker. Preserve the `/data` volume; do not erase the durable audit state.
4. Confirm the v1.2.8.5 instance is fully stopped before allowing the rollback instance to serve alone.
5. Repeat that release's disarmed health and live-readiness checks.

Do not re-arm a rolled-back release merely to restore trading quickly. Re-arm only after its safety checks pass and the incident that caused rollback is understood.
