# Release audit — Router v1.2.8.14 bounded state fallback RC1

## Scope

This release changes only live Router behavior for temporary account-state read failures.
Signal selection, Pine alerts, normal verified-state formulas, broker order construction,
bracket ownership, reconciliation, stop management, control fences, deduplication, and the
v1.2.8.13 no-daily-loss-lock behavior are unchanged.

## Safety design

- The unit of allowance is one signal/entry wave, not one account order.
- The durable SQLite episode counter survives restart and redeployment.
- Five fallback waves are allowed; the sixth is rejected before durable attempt creation
  or broker mutation.
- A separate one-hour limit rejects a later wave even when fewer than five were used.
- Duplicate processing of the same event key does not consume another allowance.
- A complete successful live refresh of every enabled account resets the episode.
- Automatic circuit reset is limited to state-fallback, startup-state, and recognizable
  legacy transient state-preparation circuits. Execution uncertainty circuits are retained.
- Only recognized timeouts, connection failures, HTTP 429/5xx conditions, snapshot-refresh
  contention, and stale/uninitialized state cache errors qualify.
- HTTP 400/401/403, invalid bearer, missing verified prop risk/MLL data, disabled/unverified
  rules, closed/breached accounts, and unknown errors fail closed.
- Fallback allocation never fabricates balance, MLL, cushion, lock, or daily-ledger values.
- Quantity uses floor rather than nearest-contract rounding, ensuring initial stop risk is
  no greater than the conservative fallback budget.
- Normal and ASW entry paths make the fleet-wide fallback decision before broker mutation.

## Default risk budgets

| Account type | Formula |
| --- | --- |
| Challenge | `configured max_loss × 10%` |
| Funded | `configured max_loss × 7.5%` |
| Personal | `configured normal personal risk × 50%` |

The values are optional environment overrides. Defaults require no Railway variable change.

## Executed validation

| Check | Result |
| --- | --- |
| New bounded-fallback unit/integration suite | **14 passed** |
| Shadow observer regression suite | **54 passed** |
| Live-role regression suite (includes new tests) | **331 passed** |
| Python syntax compilation | **Passed** |

The new tests cover account-type budgets, floor sizing, transient/hard classification,
five-wave admission, sixth-wave blocking before PLACE, one-hour expiry, duplicate-event
idempotency, persistence across Store restart, targeted automatic recovery, and protection
of unrelated execution circuits.

The existing FastAPI lifecycle/TestClient deprecation warnings remain. They are dependency
warnings and not test failures.

## Operational limit

Trading while live balance/MLL/daily-ledger state is unavailable is inherently less certain
than verified-state sizing. A conservative nonzero budget cannot prove the account's current
distance from a prop-firm failure threshold. This release bounds that uncertainty; it does
not claim to eliminate it. Operators who require strict fail-closed behavior can set
`STATE_FALLBACK_ENABLED=false`.

No deployment, Railway variable mutation, webhook change, or broker order was performed by
this build/audit.
