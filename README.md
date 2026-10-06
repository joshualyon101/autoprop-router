# AutoProp Router v1.2.8.14 — Bounded State Fallback RC1

This release prevents a known temporary account-state read failure from immediately
disabling every new entry. It adds a durable, conservative fallback window while keeping
authentication, configuration, account-status, execution, bracket, and reconciliation
failures fully fail-closed.

No Pine change is required for this Router release. It accepts the same alert contract as
v1.2.8.13 and retains the v1.2.8.13 removal of the portfolio daily realized-loss lock.

## Exact behavior

When a live account-state read fails for a recognized temporary reason (for example a
CrossTrade timeout, HTTP 429 `snapshot_refresh_pending`, or a stale/uninitialized entry
cache), the Router may size from verified account configuration instead of inventing a
cash balance, MLL floor, drawdown cushion, or daily ledger.

The fallback window:

- counts one TradingView entry signal as one entry wave, regardless of how many accounts
  receive it;
- allows at most five fallback entry waves;
- lasts at most 3,600 seconds (one hour);
- is durable across process restarts and Railway redeployments;
- blocks the sixth fallback wave, or the first wave after the time limit, before any order
  is sent;
- automatically resets after one complete successful live refresh of every enabled
  account; and
- automatically clears only a circuit owned by this state-fallback condition. It never
  clears a circuit caused by ambiguous orders, bracket/readback failures, unresolved
  attempts, protection failures, or other broker uncertainty.

Authentication errors (including HTTP 401/403), invalid tokens, missing verified prop
rules/MLL configuration, closed or breached accounts, malformed responses, and unknown
errors do not qualify. They remain fail-closed immediately.

## Conservative fallback budgets

Fallback quantities are floored so initial stop risk cannot exceed the configured budget.
If even one MNQ contract exceeds the budget, that destination is skipped.

| Account type | Default fallback budget |
| --- | ---: |
| Challenge | 10% of configured maximum loss |
| Funded | 7.5% of configured maximum loss |
| Personal | 50% of configured normal personal risk |

Core confidence/add-on multipliers may reduce a fallback budget but can never increase it.
Challenge and Personal ASW entries remain one exact native plan; Funded ASW may use only a
whole native-plan multiple within its fallback budget.

This is deliberately smaller than normal Standard sizing. No nonzero fallback can prove
current prop-firm drawdown compliance while account state is unavailable; the lower cap,
five-wave limit, one-hour limit, and one-contract floor check bound that uncertainty.

## Railway variables

No new variable is required. The defaults above activate automatically. Add variables only
if you intentionally want to override them:

```text
STATE_FALLBACK_ENABLED=true
STATE_FALLBACK_MAX_ENTRY_WAVES=5
STATE_FALLBACK_MAX_DURATION_SECONDS=3600
STATE_FALLBACK_CHALLENGE_MAX_LOSS_PCT=10
STATE_FALLBACK_FUNDED_MAX_LOSS_PCT=7.5
STATE_FALLBACK_PERSONAL_RISK_MULTIPLIER=0.5
```

Keep the existing live/shadow, alert-contract, CrossTrade, account configuration, risk
state, and arm variables unchanged.

## Monitoring

`/health` now reports:

- `state_fallback_active`
- `state_fallback_entry_waves_used`
- `state_fallback_max_entry_waves`
- `state_fallback_max_duration_seconds`
- `state_fallback_failing_accounts`

Entry results identify each destination using fallback sizing and include the underlying
state-read reason. `/admin/live-readiness/{token}` treats recognized temporary state reads
as warnings while the bounded fallback is available; hard failures remain problems.

Read `RELEASE_AUDIT.md` for the implementation and regression-test record. The package
contains no credentials, account configuration, account state, or Railway volume data.
