# AutoProp Router v1.2.8.19

## Reliability change

When the live router is intentionally armed, temporary account-state and fleet-position
read failures no longer discard an otherwise valid entry signal.

- Sizing first uses the durable last verified account state, including its last confirmed
  closed-cash balance, MLL data, daily ledger, and consistency state.
- If an account has never produced a verified state, the existing conservative
  configuration-only account-type fallback remains available.
- A failed fleet snapshot proceeds through CrossTrade's atomic
  `requireMarketPosition=flat` and `maxPositions=1` order guards.
- The former five-wave/one-hour state-read fallback cutoff is unbounded while the router
  is deliberately armed.
- Read-only state circuits are reported as degraded but no longer disarm entries.

## Safety boundaries retained

The router still blocks new risk for unresolved or ambiguous PLACE attempts, persistence
failures, pending protection management, confirmed pre-existing positions/orders, and
protection/normalization failures. Idempotent event keys and durable attempt ownership are
unchanged, so a timed-out mutation is reconciled and never blindly resent.

## Configuration

`ARMED_LAST_KNOWN_STATE_EXECUTION=true` is the new default. No Railway variable is required.
Set it to `false` only to restore the prior bounded fail-closed behavior.

After deployment, `/health` should show:

- `version`: `1.2.8.19-armed-last-known-execution-rc1`
- `armed_last_known_state_execution`: `true`
- `broker_mutation_armed`: `true` when the independent live arm gates are satisfied
- `entry_circuit_blocks_new_entries`: `false` for a read-only degraded circuit
