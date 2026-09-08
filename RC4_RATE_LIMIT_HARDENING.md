# AutoProp Router v1.2.5 Exact Parity Production RC4 — CrossTrade Rate-Limit Hardening

This release changes only CrossTrade transport/rate-limit handling plus version/config wiring. Strategy, sizing, allocation, execution ownership, fill-proof, management, ORG re-entry, and state-accounting formulas are unchanged.

## Changes
- Adds a process-wide rolling CrossTrade request budget of 150 requests per 60 seconds by default, below CrossTrade's observed 180 requests/minute user ceiling.
- Preserves burst capacity for multi-account execution instead of forcing fixed per-request spacing.
- Coordinates independently-created Router clients through one process-wide budget/cooldown.
- Recognizes only an explicit CrossTrade `success:false, error:"rate_limited"` HTTP 429 as a proven rejection.
- Honors `retryAfter` JSON and `Retry-After` response headers, applies a global cooldown, and retries up to 8 times by default.
- Explicit rate-limit rejections may be retried for mutations because CrossTrade has positively stated that request was rejected. Network errors and 5xx mutation responses remain `AmbiguousMutation` and are never blindly resent.
- Adds optional environment controls with safe defaults:
  - `CROSSTRADE_RATE_LIMIT_PER_MINUTE=150`
  - `CROSSTRADE_RATE_LIMIT_WINDOW_SECONDS=60`
  - `CROSSTRADE_RATE_LIMIT_MAX_RETRIES=8`
  - `CROSSTRADE_RATE_LIMIT_FALLBACK_SECONDS=1`

No new Railway variables are required; defaults are active automatically.
