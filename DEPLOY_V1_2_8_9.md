# AutoProp Router v1.2.8.9 deployment

Release: `1.2.8.9-exact-parity-priority-management-rc1`

## Scope

This release fixes the live Silver-management failure observed on 2026-09-23 and retains
the verified-cohort automatic challenge onboarding from v1.2.8.8.

The observed failure had two coupled causes:

1. background balance, fill-history, readiness, and discovery reads could occupy the same
   broker-read lane needed to promote accepted entries to managed `ACTIVE` trades;
2. a Silver stop event was requeued until every Silver destination had promoted, so no
   already-ready destination could advance and the webhook attempt count grew repeatedly.

## Changed behavior

- Every valid Silver stop alert is committed once as a durable management intent.
- Stage 2 supersedes stage 1 atomically; a late stage 1 can never loosen stage 2.
- The webhook inbox completes after recording intent and does not create a retry storm.
- Entry reconciliation and Silver management share one serialized safety-critical lane.
- An `ACTIVE` destination advances even while a sibling remains in `ACCEPTED` readback.
- Intent remains durable across retries/restarts until every live Silver destination has
  reached the requested stage or is proven flat.
- Balance/history refresh, automatic discovery, and live-readiness fanout yield while an
  entry is unresolved, a trade is active, a working limit exists, or management is pending.
- Background account-state refresh is serial, so it never queues an eight-account GET wave.
- Health reports `silver_management_pending` and `silver_management_stage`.
- New risk stays disarmed while Silver management intent is pending.

## Deploy safely

1. Pause the TradingView alert.
2. Set `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false` in Railway.
3. Confirm every account is flat and has no working orders.
4. Replace the GitHub repository files with this release and allow Railway to deploy.
5. Keep one Railway replica and one Uvicorn worker.
6. Do not override the included `railway.toml` start command. If Railway has a manual
   Start Command, use:

   `sh -c 'exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1 --no-access-log'`

7. Verify `/health` reports:
   - `version="1.2.8.9-exact-parity-priority-management-rc1"`;
   - `configuration_ready=true`;
   - `broker_mutation_armed=false`;
   - `entry_circuit_open=false`;
   - `unresolved_entry_attempts=0`;
   - `silver_management_pending=false`;
   - `silver_management_stage=0`;
   - `auto_discovery_enabled=true`;
   - the expected `registered_accounts` count.
8. Run `/admin/live-readiness/<token>` and require `problems=[]`, every account
   `net_position=0`, and no snapshot working-order problem.
9. Set `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true`.
10. Confirm `/health` reports `broker_mutation_armed=true`, then resume the alert.

## Railway variables

No new variable is required; the release has safe defaults. Optional explicit values are:

```text
SILVER_MANAGEMENT_LOOP_SECONDS=0.25
SILVER_MANAGEMENT_RETRY_BASE_SECONDS=0.50
SILVER_MANAGEMENT_RETRY_MAX_SECONDS=5
```

Retain the v1.2.8.8 onboarding values:

```text
AUTO_DISCOVERY=true
AUTO_ONBOARD_VERIFIED_CHALLENGE_COHORTS=true
FUNDEDNEXT_DEFAULT_MODEL=Legacy
AUTO_ONBOARD_FUNDEDNEXT_LEGACY_CHALLENGES=true
AUTO_DISCOVERY_INTERVAL_SECONDS=10
AUTO_ONBOARD_BALANCE_TOLERANCE=1
AUTO_ONBOARD_FILL_LOOKBACK_DAYS=35
```

## Expected live behavior

During an open trade, `/admin/live-readiness/<token>` may return HTTP 409 stating that
safety-critical work is active. This is intentional: the endpoint no longer competes with
live reconciliation and stop management. `/health` remains available throughout the trade.

After a Silver stop alert, `/health` may briefly show:

```text
silver_management_pending=true
silver_management_stage=1
```

It returns to `false`/`0` only after all destinations have applied the stage or are proven
flat. Do not manually clear this state.
