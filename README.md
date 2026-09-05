# AutoProp Router v1.0.0 RC1 — Production Full Scale

Cloud risk router for TradingView -> CrossTrade -> Tradovate.

Primary production behavior:
- hot-cache account balance/position state;
- auto-discovery of linked prop accounts;
- persistent manual rule/MLL overrides;
- live per-account cushion sizing with nearest-contract rounding;
- concurrent multi-account order fan-out;
- full-scale live mode with no artificial quantity cap (`LIVE_MAX_QTY_PER_ACCOUNT=0`);
- broker-hosted Tradovate protection;
- event-level idempotency;
- force-flat path independent of entry risk-readiness;
- weekend dry-run and alert-normalization endpoints.

Read `PRODUCTION_FULL_SCALE_SETUP.md` before arming broker mutation.
