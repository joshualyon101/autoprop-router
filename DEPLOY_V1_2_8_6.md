# AutoProp Router v1.2.8.6 — Core Normalization Race Hotfix

This release must be deployed while new risk is disarmed. Keep
`TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false` and keep the TradingView alert paused until
every disarmed check below passes.

## 1. Preconditions

1. Confirm all eight destination accounts are flat and have no working orders.
2. Confirm `/health` reports `active_trades=0`, `asw_pending_limits=0`, and
   `unresolved_entry_attempts=0`.
3. Preserve the existing Railway `/data` volume and SQLite database. Do not delete them.
4. Keep exactly one Railway replica. Do not run an overlapping canary or second service
   against the same accounts.

The currently persisted entry circuit may still be open from the September 22 incident.
That is expected. Do not reset it before this release is deployed and verified disarmed.

## 2. Replace the GitHub source

1. Extract the release ZIP on your computer.
2. In the GitHub repository, remove obsolete project files that are not present in the
   extracted release, then upload the extracted files at the repository root. Do not upload
   the ZIP itself as the application source and do not create an extra nested folder.
3. Commit the replacement and let Railway deploy it.
4. Keep the included `railway.toml` and `Dockerfile`. Both require one Uvicorn worker and
   disable access logging so token-bearing URL paths are not written to logs.
5. Wait until Railway shows only the new deployment active.

## 3. Verify the disarmed deployment

Check `/health` and require:

- `version=1.2.8.6-exact-parity-core-normalization-race-hotfix-rc1`;
- `execution_mode=live`;
- `management_mode=exact_formula_parity`;
- `execution_symbol=MNQ1!`;
- `tradingview_alert_contract_verified=false`;
- `broker_mutation_armed=false`;
- `registered_accounts=8`;
- `active_trades=0` and `asw_pending_limits=0`; and
- `unresolved_entry_attempts=0`.

Then check `/admin/entry-attempts/<token>`. Historical attempts may remain in the response,
but `unresolved` must be `0`.

## 4. Clear the incident circuit safely

Only after the new version is running, the gate is false, `unresolved=0`, and the broker
shows all accounts flat with no working orders:

1. Call `POST /admin/entry-circuit/reset/<token>`.
2. Require `reset=true` and `entry_circuit.open=false`.
3. Recheck `/health`; it must show `entry_circuit_open=false` while
   `broker_mutation_armed=false` remains false because the TradingView gate is still false.
4. Call `/admin/live-readiness/<token>` and require `problems=[]`, eight enabled accounts,
   and `net_position=0` for every account. The false contract-gate warning is expected.

The reset endpoint independently checks the all-account snapshot and refuses to reset if a
configured destination has a position or working order.

## 5. Arm once

1. Leave the TradingView alert paused.
2. Set `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true` in Railway.
3. Wait for deployment completion and again confirm exactly one replica and one worker.
4. Require `/health` to show `broker_mutation_armed=true`, `entry_circuit_open=false`, and
   `unresolved_entry_attempts=0`.
5. Require `/admin/live-readiness/<token>` to show `problems=[]` and
   `broker_mutation_armed=true`.
6. Resume the TradingView alert only after those checks pass.

## 6. First-trade acceptance

For the next natural Core trade, monitor `/admin/webhook-inbox/<token>` and
`/admin/entry-attempts/<token>` and require:

- eight intended account submissions with no duplicate PLACE;
- a tight `acceptance_spread_ms` in the ENTRY result;
- Core child discovery and exact bracket normalization progressing across accounts without
  a multi-minute serial sequence;
- attempts promoting to `ACTIVE` after fill, side, quantity, and protection proof;
- an ordinary EXIT closing already-flat attempts without reopening the entry circuit; and
- `unresolved=0`, `entry_circuit_open=false`, zero positions, and zero Router-owned working
  orders after the trade is complete.

Broker pacing and network latency still apply. The release removes avoidable Router
serialization; it does not bypass CrossTrade's request budget.

## Emergency stop

If an account is wrong, a bracket CHANGE is unconfirmed, an attempt remains unresolved, or
the circuit opens:

1. Set `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false` and pause TradingView immediately.
2. Do not resend the signal and do not repeatedly restart Railway.
3. Inspect broker positions and working orders plus `/admin/entry-attempts/<token>`,
   `/admin/webhook-inbox/<token>`, and `/admin/live-readiness/<token>`.
4. If exposure is unsafe, flatten and clear it at the broker, then prove every destination
   flat with no working orders.
5. Reset the circuit only while disarmed and only after `unresolved=0`.

## Rollback

Rollback only from a fully drained state. Keep the gate false, preserve `/data`, deploy one
known-good instance with one worker, and repeat that release's complete disarmed validation.
Do not re-arm merely to restore trading quickly.
