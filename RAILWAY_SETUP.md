# AutoProp Router v0.1 — Railway Setup

## Required Railway layout

- One Railway service
- One persistent Volume mounted at `/data`
- One replica only
- Public Railway domain enabled
- SQLite path: `/data/autoprop_router.sqlite3`
- Execution mode: `shadow`

## Required variables

CROSSTRADE_BASE_URL=https://app.crosstrade.io
CROSSTRADE_TOKEN=<your token>
AUTOPROP_WEBHOOK_TOKEN=<long random token>
AUTOPROP_EXECUTION_MODE=shadow
AUTOPROP_LIVE_ARM=
STATE_POLL_SECONDS=10
MAX_STATE_AGE_SECONDS=20
REQUEST_TIMEOUT_SECONDS=4
DEFAULT_EXECUTION_SYMBOL=
MNQ_POINT_VALUE=2.0
MNQ_TICK_SIZE=0.25
MODELED_ROUND_TRIP_COST=2.90
CONSISTENCY_MIN_PROFIT_ROOM=50.0
USE_CROSSTRADE_POSITION_GATE=true
USE_CROSSTRADE_MAX_POSITIONS_GATE=false
ACCOUNT_CONFIG_PATH=config/accounts.json
SQLITE_PATH=/data/autoprop_router.sqlite3

## Do not put secrets in GitHub

Never commit the real CrossTrade token or webhook token. Add them only in Railway's Variables page, then seal them.
