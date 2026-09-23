# AutoProp Router v1.2.8.9 validation

Version: `1.2.8.9-exact-parity-priority-management-rc1`

## Result

`311 passed`

The complete exact-parity, execution, durable-entry, bracket-readback, Core normalization,
ASW, control-fence, transport, readiness, and automatic-onboarding suite remains green.

## New regression coverage

- Silver stop webhook completes once and records durable intent instead of requeueing.
- Stage requests coalesce monotonically to the highest pending stage.
- A stage-1 service pass cannot clear a concurrently recorded stage 2.
- An already-`ACTIVE` account advances while a sibling remains unresolved.
- Intent clears only after all active Silver trades reach the requested stage.
- Already-completed destinations are not reread on later intent retries.
- Background account-state refresh is serial rather than fleet-wide concurrent fanout.
- Background state refresh pauses while accepted exposure requires reconciliation.
- The prior flat-proof and owned-order cleanup safety contracts remain intact.

## Retained onboarding coverage

- FundedNext Legacy and MyFundedFutures Pro recognized challenge profiles.
- Unique verified same-cohort/same-size inheritance.
- Untouched-account proof, atomic persistence, idempotent discovery, and quarantine on any
  missing, conflicting, traded, open, or unsafe account fact.

## Safety invariant

A management alert is acknowledged only after its intent is durably stored. The intent is
not considered complete until every relevant managed destination has reached that stage or
has been proven flat. Background diagnostics never take read priority over that proof.
