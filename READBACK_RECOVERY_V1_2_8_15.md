# Accepted-entry readback recovery — v1.2.8.15 RC1

An accepted entry can remain unconfirmed when CrossTrade position reads exhaust their
transport retries. The Router blocks new entries because acceptance alone does not prove
the resulting position or protective orders. Previously that circuit could remain open
after an EXIT positively closed the originating attempt.

## Automatic recovery

The existing state refresh loop now checks eligible transport-read circuits. Defaults:

```text
READBACK_AUTO_RECOVERY_ENABLED=true
READBACK_AUTO_RECOVERY_INTERVAL_SECONDS=10
READBACK_AUTO_RECOVERY_CONFIRMATIONS=2
```

No weekly reset is required for a qualifying, resolved incident. Recovery requires:

- Live mode and an unchanged READBACK_UNCONFIRMED circuit with a recognized transport
  error in its accepted-entry readback reason.
- The original durable attempt positively CLOSED or FLAT on an enabled account.
- No entry wave, unresolved attempt, managed trade, pending ASW order, Silver intent,
  or queued safety control remaining.
- Verified rules and fresh strictly admitted account state for every enabled account,
  including daily-ledger verification and verified prop failure floors.
- Two successful, separated live broker snapshots showing empty positions and working
  orders on every enabled account. Missing or malformed data does not count as flat.
- An atomic SQLite compare-and-reset that rechecks circuit identity, durable ownership,
  pending safety work, and account-state freshness before committing.

Any failed check discards earlier confirmation. Restart also discards confirmation.
Configuration-only bounded state fallback cannot supply recovery proof. Other execution,
protection, authentication, and ambiguous-order circuits retain their existing behavior.
Recovery performs reads and local state changes only; it never submits or flattens an
order, changes an arm variable, or replays a missed signal. The original incident is
retained under previous_circuit. A late read exception cannot reopen an attempt that
EXIT already closed.

## Diagnosis and monitoring

Health adds readback_auto_recovery_enabled, readback_auto_recovery, and
broker_transport_diagnostics. Diagnostics record a request correlation ID, sanitized
endpoint, attempt count, HTTP duration, rate-limit wait, read-slot wait, and retry result.
They omit tokens, query values, account names, order IDs, and exception message contents.
Counters cover this client process only; structured warnings are also emitted to logs.

The reported incident proves that a GET positions transport read exhausted four attempts
with ReadTimeout. It does not identify the upstream cause. Default six-second request
timeouts with three retries and backoff can occupy about 29 seconds; production overrides
and earlier attempts must be checked in Railway logs. Possible server/network causes
remain hypotheses until correlated logs establish them. Timeout settings are unchanged.

TradingView webhook delivery has a separate three-second deadline. An HTTP 503 while
the circuit is open is a deliberate Router rejection, not proof of that delivery timeout.
TradingView's Stopped — Calculation error also requires its Pine runtime error detail;
this release cannot restart alerts or repair a Pine calculation failure.

## Validation

420 applicable tests passed: 419 with live mode, plus the shadow-default test separately.
This includes 35 new cases covering successful recovery, stale/incomplete/nonflat proof,
outages, restart, wrong circuit types, unresolved ownership, queued controls, circuit
replacement races, prop-state checks, late timeout after EXIT, sanitized diagnostics,
and no retry of mutation timeouts. Legacy tests importing the historical app package and
test_production_rc.py were excluded because they do not target the current flat layout.
Compilation and git diff --check passed. Broker calls were mocked; no live orders were
placed during testing.

## Rollout

Review and merge the PR when ready for Railway to deploy the updated main branch.
After deployment verify version 1.2.8.15-readback-auto-recovery-rc1 and the recovery health
fields. Existing live arming and alert-contract settings retain their usual authority.
Observe logs for recovered circuits or blocked recovery reasons. If any required broker
proof remains unavailable, the circuit remains open for investigation. To disable the
new behavior set READBACK_AUTO_RECOVERY_ENABLED=false; transport diagnostics remain.
