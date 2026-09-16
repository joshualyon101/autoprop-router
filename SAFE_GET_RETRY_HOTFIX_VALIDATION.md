# AutoProp Router v1.2.8.2 — Safe GET Retry Hotfix Validation

Date: 2026-09-16

## Trigger
Repeated live-readiness passes showed transient CrossTrade read-only failures on CT_59230927:
- GET position ReadTimeout
- HTTP 429 snapshot_refresh_pending with retryAfter=1

These are read-only broker/state operations. They should remain fail-closed if persistent, but a single transient GET failure should not unnecessarily block an otherwise healthy account or cause that destination to skip a live entry.

## Scope
This is a transport-read hardening patch only.

Changed production files:
- crosstrade.py
- version.py

Added regression test:
- test_safe_get_retry_hotfix.py

All strategy/account allocation, execution geometry, bracket verification, management, ASW lifecycle, state math, persistence, and Pine code remain unchanged.

## New behavior
Read-only GET requests:
- retry up to 2 times after the initial attempt on httpx timeout/network errors;
- retry CrossTrade HTTP 429 `snapshot_refresh_pending` using `retryAfter`;
- remain bounded and fail closed after retries are exhausted.

Mutations (POST/PUT/PATCH/DELETE):
- network/timeout ambiguity is still NEVER blindly retried;
- immediately raises AmbiguousMutation as before;
- explicit CrossTrade `rate_limited` proven-rejection behavior is unchanged.

## Validation
`python -m compileall -q .` — PASS

`PYTHONPATH=. pytest -q` — **172 passed**

New focused coverage:
1. GET timeout -> bounded retry -> success.
2. GET timeout persists -> exactly 3 total attempts -> fail closed.
3. Mutation timeout -> exactly 1 request -> AmbiguousMutation.
4. GET snapshot_refresh_pending -> retry -> success.

## MNQ transport
The existing symbol-hotfix regression remains passing. The CrossTrade boundary still normalizes:
- MNQ -> MNQ1!
- MNQ1! -> MNQ1!
- dated MNQ -> retained
- non-MNQ -> fail closed

No symbol code was changed in this hotfix.

## Deployment
1. Keep `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`.
2. Deploy v1.2.8.2 flat-root ZIP over v1.2.8.1.
3. Verify /health reports v1.2.8.2, execution_symbol MNQ1!, configuration_ready true, broker_mutation_armed false.
4. Run /admin/live-readiness.
5. Require problems=[] and all 8 accounts flat.
6. Re-arm only after readiness is clean.

Pine/TradingView alert does NOT need to be recreated for this Router-only patch.
