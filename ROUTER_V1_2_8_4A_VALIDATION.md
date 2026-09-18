# AutoProp Router v1.2.8.4A — Broker Read Hardening Validation

Date: 2026-09-18

## Release identity

- Version: `1.2.8.4A-exact-parity-six-engine-silver-sb35-read-hardening-rc1`
- Baseline: `AutoProp_Router_v1.2.8.3_SILVER_SB35_STAGED_RC1_FLAT_ROOT.zip`
- Verified baseline SHA-256: `1b4697cbeb3b1fd3830bb65b2e8ec78fac2900309f7f5083db1781b116b00dc7`

## Trigger

The 2026-09-18 live ORG event exposed two transport/readback gaps that were not
addressed by the earlier v1.2.8.3 safe-GET patch:

1. Tradovate returned HTTP 429 with `error=broker_rate_limited` and
   `retryAfter=5`, but the client recognized only CrossTrade's
   `error=rate_limited` response.
2. Exact child-order verification made repeated lifecycle reads after a PLACE,
   increasing broker-read pressure and allowing a later read timeout to obscure a
   bracket that had already been verified.

Manual trading explained at least one working-order gate skip. The working-order
gate was intentionally not weakened, and this release does not auto-cancel unknown
or manual orders.

## Scope

Router-only transport and verification hardening. No changes were made to:

- Fusion/Pine strategy logic;
- six-engine signal priority or entry geometry;
- challenge, funded, or personal sizing;
- SB35 stop management;
- ASW contract semantics;
- MNQ/MNQ1! normalization;
- account rules, MLL logic, or consistency calculations;
- TradingView alert format.

Production files changed:

- `crosstrade.py`
- `execution.py`
- `live.py`
- `settings.py`
- `.env.example`
- `version.py`

Regression coverage added:

- `test_transport_read_scheduler_v1_2_8_4a.py`

## Corrected behavior

### Safe broker reads

- All GET requests use one bounded retry path.
- Default GET timeout increases from 4 to 6 seconds.
- Transient GET failures receive up to three retries after the first attempt.
- Retry spacing uses bounded exponential backoff: 0.75s, 1.5s, then 3.0s.
- Process-wide GET concurrency is capped at two.
- Request starts are paced by a 0.10-second minimum interval.

### Tradovate 429 handling

- Recognizes `broker_rate_limited`, `rate_limited`, and
  `snapshot_refresh_pending`.
- Honors `Retry-After` from either the response header or JSON payload.
- A broker/account penalty pauses the affected account; other accounts remain
  eligible to proceed.
- A CrossTrade user-level rate limit applies a process-wide cooldown.
- GET retries remain bounded and fail closed.

### Mutation safety

- The transport layer never resends PLACE, CHANGE, CANCEL, or FLATTEN.
- A rate-limited mutation is surfaced to the caller after its cooldown is
  recorded.
- CHANGE reconciliation must read the exact order state before the higher-level
  exact-change loop can make another idempotent attempt.
- Ambiguous PLACE behavior is unchanged: reconcile by caller-supplied order ID;
  never blindly resend.

### Exact bracket readback

- Exact readback still requires role, quantity, price, broker ID, and ownership.
- If lifecycle is unavailable, an alternate order snapshot is accepted only when
  that snapshot independently contains all exact geometry fields.
- Raw status-only Tradovate Order rows never satisfy exact verification.
- A successful bracket verification now carries verified target and stop IDs in
  the execution receipt, eliminating an immediate duplicate lifecycle pass.
- If an accepted entry cannot be verified, Router sends exactly one emergency
  flatten request and reports whether that flatten was confirmed or ambiguous.

## Validation results

- `python -m compileall -q .` — PASS
- `pytest -q` — **188 passed**

Focused coverage proves:

1. lifecycle timeout retries remain bounded;
2. lifecycle exhaustion can use only an exact alternate snapshot;
3. `broker_rate_limited` GET honors a five-second Retry-After;
4. a rate-limited PLACE is sent exactly once;
5. concurrent safe GET traffic never exceeds two active reads;
6. an accepted PLACE with unprovable protection triggers one flatten and no
   second PLACE;
7. verified child roles are reused without a second lifecycle pass;
8. all existing allocation, parity, ASW, SB35, symbol, persistence, and execution
   tests remain passing.

The six test-suite warnings are existing FastAPI/Starlette deprecation warnings;
there are no test failures.

## Deployment checklist

1. Keep `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false`.
2. Confirm all eight accounts are flat and have zero working MNQ orders.
3. Deploy the v1.2.8.4A flat-root ZIP with one Railway replica and the existing
   persistent `/data` volume.
4. Set or update these Railway variables:

   - `REQUEST_TIMEOUT_SECONDS=6`
   - `CROSSTRADE_REQUEST_MIN_INTERVAL_SECONDS=0.10`
   - `CROSSTRADE_SAFE_GET_MAX_CONCURRENCY=2`
   - `CROSSTRADE_GET_RETRY_MAX_RETRIES=3`
   - `CROSSTRADE_GET_RETRY_DELAY_SECONDS=0.75`
   - `CROSSTRADE_GET_RETRY_BACKOFF_MULTIPLIER=2.0`
   - `CROSSTRADE_GET_RETRY_MAX_DELAY_SECONDS=5.0`

5. Verify `/health` reports:

   - version `1.2.8.4A-exact-parity-six-engine-silver-sb35-read-hardening-rc1`;
   - `execution_symbol=MNQ1!`;
   - `configuration_ready=true`;
   - `broker_mutation_armed=false`;
   - `active_trades=0`;
   - `asw_pending_limits=0`.

6. Run `/admin/live-readiness/<token>` and require `problems=[]`, all eight
   accounts flat, and no unexpected working orders.
7. Set `TRADINGVIEW_ALERT_CONTRACT_VERIFIED=true` and verify
   `broker_mutation_armed=true`.
8. Use the next natural Fusion trade as the live acceptance test. Do not trade or
   manually flatten Router-controlled accounts during that acceptance event unless
   an emergency requires it.

No TradingView alert recreation or Fusion/Pine update is required for this
Router-only release.
