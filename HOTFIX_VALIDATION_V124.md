# AutoProp Router v1.2.4 RC3 — Hotfix Validation

Purpose: resolve CrossTrade cash-balance freshness and durable-ledger readiness without changing strategy/risk/allocation/execution/management logic.

Changes:
- Cash balance still uses closed cash only; no net-liquidation fallback.
- When CrossTrade balance payload omits a timestamp, use successful live GET observation time solely for freshness.
- Durable fills endpoint is now attempted directly. A successful well-formed read proves the current NY daily ledger for that refresh; malformed/unavailable history still fails closed.
- Railway start command uses shell expansion for $PORT.
- Version: 1.2.4-exact-parity-production-rc3.

Regression tests: 2/2 PASS.
Compile: PASS.
Local Uvicorn startup + /health: PASS.

Critical logic file identity versus v1.2.3:
parity.py: IDENTICAL 68440c7f09931cd7a25e46c74522547b3f306dc048f52056c76349ccaf403566
allocation.py: IDENTICAL 8ff08a4dac73cbc76e908e21198a711d87adb0d02f4405b6b525fde8d827bd45
execution.py: IDENTICAL ae4e079c70f029d9ec34eea7da250760d87e915c2b51f966da8bd6a791180126
management.py: IDENTICAL 7de3f59464f385d1a12c41b54c9b72ccdb1e4e86e8b1cc7a53d69eb43db681f7
events.py: IDENTICAL 923b56e9ce8b798cd7e1a02c8f2f1381fbd1a84a7f62d5a22f5d27f2af2564b3
models.py: IDENTICAL 225d3922c586822ea64f568674c194d703540f540ab7bb625fa87cbd77b1ed09
live.py: IDENTICAL c976feec6f01b4eb2fb139b0b9c2513837832af5c3cc701d65c055347c77e9dc
org_reentry.py: IDENTICAL 5404065fcf367d2e053a28924037a7f4deaa1eecebbe2b8b1708e9862be52ef6
store.py: IDENTICAL 9fa25cdb73709c490a222cdc1baf25c1bb87cda59e46d616653484903932918d
