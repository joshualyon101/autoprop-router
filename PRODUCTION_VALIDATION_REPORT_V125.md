# AutoProp Router v1.2.5 Exact Parity Production RC4 — Validation
Date: 2026-09-08

## Purpose
Harden CrossTrade transport against the observed HTTP 429 per-user limit without changing AutoProp's five-engine strategy, sizing, allocation, execution ownership, management, state accounting, or ORG re-entry logic.

## Validation completed
- Focused v1.2.5 rate-limit tests: **4/4 PASS**
  - explicit 429 GET rejection retries and succeeds
  - explicit 429 mutation rejection retries only because server positively rejected it
  - 5xx mutation remains ambiguous and is not retried
  - non-explicit 429 is not blindly retried
- Frozen formula parity test module: **102/102 PASS**
- Python compileall: **PASS**
- Local Uvicorn startup: **PASS**
- Local `/health`: **PASS**, reports `1.2.5-exact-parity-production-rc4`

## Critical-file identity versus deployed v1.2.4 RC3 package
The following files are byte-identical to v1.2.4 RC3:
- parity.py
- allocation.py
- execution.py
- management.py
- events.py
- models.py
- org_reentry.py
- store.py
- state.py
- state_refresh.py
- config.py
- readiness.py
- main.py
- service.py

Only transport/config wiring changed in executable logic:
- crosstrade.py
- live.py (CrossTrade client constructor wiring only)
- settings.py (new transport defaults)
- version.py

## Rate-limit behavior
Default local rolling budget: 150 requests / 60 seconds, intentionally below the observed CrossTrade 180 requests/minute ceiling while retaining burst capacity.

On HTTP 429, automatic retry occurs only when CrossTrade explicitly returns a proven rejection (`success:false`, `error:"rate_limited"`). The Router honors `retryAfter`/`Retry-After` and coordinates a process-wide cooldown. Mutation network errors and 5xx responses remain ambiguous and are never blindly resent.

## Deployment variables
No new Railway variables are required. Defaults activate automatically. Optional tuning variables exist in `.env.example`.
