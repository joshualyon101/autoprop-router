# AutoProp Router v1.2.8.6 — Core Normalization Race Hotfix Validation

Date: 2026-09-22

## Release identity

- Version: `1.2.8.6-exact-parity-core-normalization-race-hotfix-rc1`
- Execution mode: `live`
- Management mode: `exact_formula_parity`
- Execution symbol: `MNQ1!`
- Standard ENTRY submission: the v1.2.8.5 concurrent eight-account fanout is retained
- Core post-acceptance work: guarded, concurrent account normalization with broker pacing

## Live incident diagnosis

The September 22 Core short ENTRY reached all eight intended accounts without a duplicate
PLACE. The recorded quantities totaled 28 contracts and the account acceptances were close
together. The failure occurred after acceptance:

1. CrossTrade's Core ATM response supplied a strategy/parent identity but not public child
   order identities.
2. The Router therefore had to discover four child orders per account and normalize their
   exact absolute target and stop prices through lifecycle reads and CHANGE confirmations.
3. That work was performed account by account and repeated lifecycle reads, making the
   post-entry reconciliation slow enough to overlap the ordinary EXIT.
4. EXIT correctly observed the destinations flat and changed every attempt to `CLOSED`.
5. A stale normalization worker later handled its failed readback as authoritative and
   opened the entry circuit even though the attempt was already terminal and every account
   was flat.

The resulting `CORE_NORMALIZATION_UNCONFIRMED` circuit was therefore a Router concurrency
bug, not evidence of a duplicate entry or a remaining open position.

## Corrected behavior

### Race-safe ownership

- Core normalization is authorized only while the durable attempt is still `ACCEPTED`.
- Every lifecycle poll and every child CHANGE rechecks that authorization.
- An EXIT or hard-control fence immediately supersedes further normalization mutation.
- Successful normalization writes child identities with compare-and-swap semantics.
- A verifier that loses its compare-and-swap to EXIT returns an already-advanced result and
  cannot reopen the circuit.
- Circuit opening tied to an attempt is committed atomically only if that attempt still
  owns the expected unresolved state.

### Faster Core reconciliation

- Due Core accounts normalize concurrently, bounded by
  `CORE_NORMALIZATION_MAX_CONCURRENCY=8`.
- Independent child lifecycle reads are gathered concurrently.
- The shared CrossTrade safe-GET semaphore and rolling request budget remain authoritative.
- CHANGE operations inside one account remain ordered.
- A command-confirmed lifecycle snapshot is reused as final exact readback, eliminating a
  redundant second lifecycle sweep.
- Ordinary EXIT position reconciliation fans out across independent destination accounts.

### Mutation uncertainty

- Child geometry that is already exact is not changed again.
- After a CHANGE is submitted, the Router never resends that same CHANGE merely because its
  result is pending or ambiguous.
- A rejected or unconfirmed CHANGE triggers one fail-safe flatten claim and opens a manual
  review circuit only while the attempt still owns unresolved exposure.
- Read-only discovery failures can retry for up to 90 seconds while native ATM protection
  remains present; a timeout fails toward flattening.

### Snapshot refresh collision

- Overlapping account-state refresh batches are coalesced.
- CrossTrade's `snapshot_refresh_pending` response may reuse an existing cache row only
  while that row is still within the entry freshness limit.
- A missing or stale cache remains an error, so readiness and new risk continue to fail
  closed.

## Automated validation

Validated commands:

```bash
python -m compileall -q .
pytest -q
pip check
```

Validated result: `273 passed, 6 deprecation warnings, 0 failed`; dependency check reported
no broken requirements.

The regression suite includes:

1. the exact stale-normalizer-versus-EXIT race;
2. atomic refusal to open a circuit for a terminal attempt;
3. eight-account Core normalization fanout;
4. parallel lifecycle enrichment of owned child orders;
5. concurrent ordinary EXIT destination checks;
6. one fail-safe flatten for an unconfirmed Core CHANGE;
7. fresh-only cache reuse during `snapshot_refresh_pending`;
8. no duplicate PLACE after ambiguous submission;
9. plural fill-reconciled position and owned-order flat proof;
10. one-replica/one-worker and no-access-log deployment contracts; and
11. all existing allocation, exact-formula parity, Core, Silver/SB35, ASW, risk, control
    fence, and recovery tests.

The FastAPI/Starlette warnings are existing deprecation warnings and are not test failures.

## Required production acceptance

Follow `DEPLOY_V1_2_8_6.md`. Keep the service disarmed until the exact version is running,
all eight accounts are flat and clear, the persisted incident circuit has been safely reset,
and live readiness has no problems. The first natural Core trade is the final broker-backed
acceptance test; any mismatch or unexpected circuit is an immediate stop condition.
