# AutoProp Router v0.1 — Tradovate / CrossTrade


## Recommended production host: Render

Use a paid always-on Render Web Service. Do not use Render Free for trading because free web services can spin down after inactivity.

This package's `render.yaml` attaches a 1 GB persistent disk at `/var/data` and stores SQLite state at:

```text
/var/data/autoprop_router.sqlite3
```

That state contains MLL/high-water/deduplication data and must persist across restarts and deploys.

Use **one worker** and **one service instance** for this SQLite-backed v0.1. Do not horizontally scale this version.


This build is the first executable foundation for moving **live account state and sizing out of TradingView**.

## What v0.1 does

- Pulls a consolidated CrossTrade/Tradovate account snapshot in a background loop.
- Keeps the latest account state in an in-memory **hot cache**.
- Never fetches account balances synchronously in the TradingView webhook execution path.
- Persists prop MLL/high-water state in SQLite.
- Automatically snapshots EOD-trailing balances at a per-account configured futures-session cutoff, outside the alert path.
- Tracks each account's prop trading day and applies Fusion's consistency size-first + target-clipping logic independently.
- Implements the frozen AutoProp ICT Fusion v1.0.2 Challenge and Funded risk-budget formulas recovered from the production source.
- Uses Pine-style nearest-whole-contract sizing (`0.5 -> 1`).
- Fans out account orders concurrently in live mode instead of serially.
- Deduplicates `trade_id` atomically.
- Fails closed on stale/missing account state, non-flat state, working orders, unverified rules, or zero quantity.
- Defaults to **SHADOW mode**. It cannot place orders unless two explicit live gates are changed.
- Uses one persistent HTTP client with keep-alive/HTTP2 to avoid repeated TLS setup.

## Why this minimizes alert latency

The critical path is:

`TradingView webhook -> local validation -> hot-cache lookup -> local sizing -> concurrent CrossTrade POST`

It is **not**:

`TradingView webhook -> fetch 7 balances -> wait -> size -> place`

The read loop runs independently every `STATE_POLL_SECONDS` (default 10 seconds). A signal is rejected if the cache is older than `MAX_STATE_AGE_SECONDS` (default 20 seconds).

The starter registry uses a **6:00 PM ET trading-day boundary and 6:05 PM ET EOD snapshot** because the current AutoProp farm is already flat well before then and its evening session starts later. These fields are per-account and remain `rules_verified=false` until verified. FundedNext says its futures EOD metrics typically update after 5:00 PM CT; MFFU says dashboard EOD metrics refresh after market close. A conservative closed-balance snapshot after the farm is flat prevents a network lookup from being added to the next entry.

The Router reports `router_processing_ms` on every signal so we can measure the code's own latency separately from TradingView/network/CrossTrade/Tradovate latency.

## CrossTrade safety-gate latency

`USE_CROSSTRADE_POSITION_GATE=true` is intentionally the default for first live testing. CrossTrade's `requireMarketPosition=flat` performs an additional live broker-state gate and can add latency. The Router already has its own hot-cache flat/order check, but do **not** remove the CrossTrade gate until shadow and simulator data proves the latency/safety trade is acceptable.

`USE_CROSSTRADE_MAX_POSITIONS_GATE=false` by default because this farm is MNQ-only and the Router already rejects cached non-flat accounts.

## Install locally

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
cp config/accounts.example.json config/accounts.json
```

Load environment variables using your preferred secret manager or platform. On Render, enter them in the Environment section.

Run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health:

```bash
curl http://localhost:8000/health
```

## First configuration

### 1. CrossTrade

Set:

- `CROSSTRADE_TOKEN`
- `AUTOPROP_WEBHOOK_TOKEN`

Never put the CrossTrade bearer token in Pine or in the TradingView alert message.

### 2. Account registry

Edit `config/accounts.json`.

The seven known farm slots are already present, but all are deliberately:

```json
"enabled": false,
"rules_verified": false
```

Replace every `account_name` with the exact Tradovate account name shown by CrossTrade.

**Do not enable an account until every rule field has been independently verified.**

The MFFU rule fields are deliberately placeholders rather than guessed values.

### 3. Concrete MNQ contract

Set `DEFAULT_EXECUTION_SYMBOL` to the actual Tradovate front-month contract used for execution. Do not permanently hard-code `MNQ1!`; a concrete contract avoids an extra contract-resolution lookup on the order path.

### 4. Start in shadow

Keep:

```text
AUTOPROP_EXECUTION_MODE=shadow
AUTOPROP_LIVE_ARM=
```

Send test TradingView signals. `/webhook/tradingview/...` returns a per-account routing decision without broker mutations.

## TradingView signal design

TradingView should send **account-independent setup information** only.

Preferred ENTRY body:

```json
{
  "trade_id": "APF-ORG-20260905-093501-L-001",
  "event": "ENTRY",
  "engine": "ORG",
  "direction": "LONG",
  "instrument": "MNQ1!",
  "contract_risk_dollars": 68.40,
  "entry": 23850.25,
  "stop": 23817.50,
  "tp1": 23925.00,
  "tp2": 23980.00
}
```

Webhook URL:

```text
https://YOUR-ROUTER/webhook/tradingview/YOUR_LONG_RANDOM_WEBHOOK_TOKEN
```

The signal contains **no balance, MLL, prop account name, or account-specific quantity**.

The preferred `contract_risk_dollars` is the exact setup-specific one-contract risk already computed by Pine. This keeps strategy geometry in Pine and changing account state in the Router.

For 40% Challenge consistency, the Router also uses `entry`, `tp1`, `tp2`, and `min_runner_qty` to reproduce Fusion's size-first behavior. If the projected target profit would exceed the remaining daily consistency room, the Router reduces quantity and, when necessary, clips that account's target price. This is per account because each account can have different realized P&L that day.

## AutoProp risk parity implemented

### Challenge

Current usable cushion:

`balance - current MLL floor`

Frozen base cushion-risk profiles:

| Drawdown | Standard | Aggressive |
|---|---:|---:|
| Static | 15.00% | 32.50% |
| EOD trail | 18.75% | 19.25% |
| Live trail | 18.75% | 19.75% |

Risk is capped at `1.30 × base % × original max loss`.

At >=80% challenge progress:
- Static Standard multiplies risk by 0.75.
- All other frozen profiles multiply by 0.85.

### Funded

Pre-lock:
- >=75% cushion remaining: 15.00% of Max Loss
- >=50%: 13.75%
- otherwise: 12.50%
- Aggressive multiplies those percentages by 1.10

Post-lock:
- Standard: max(pre-lock healthy risk floor, 12.5% of current cushion)
- Aggressive: max(pre-lock healthy risk floor, 15.0% of current cushion)

Below 30% pre-lock cushion, the one-contract survival guard is active and 1 MNQ is allowed only when one-contract structural risk is <=35% of remaining cushion.

## MLL handling

Static and live-trailing state can update from live state immediately.

EOD trailing is different: the Router intentionally does **not** ratchet an EOD floor from every intraday balance snapshot. The background poller advances it once per day after each account's configured `eod_snapshot_time_et`. This prevents an intraday winner from incorrectly tightening an EOD MLL while still making the new floor available before the next evening session.

Before live use, verify each firm's exact trading-day boundary and set `trading_day_start_et` / `eod_snapshot_time_et` accordingly. The next production milestone is broker-fill/order reconciliation plus shadow parity against real Fusion alerts.

## Going live

Live requires both values:

```text
AUTOPROP_EXECUTION_MODE=live
AUTOPROP_LIVE_ARM=I_UNDERSTAND_LIVE_ORDERS
```

Even then, only accounts with both:

```json
"enabled": true,
"rules_verified": true
```

can be routed.

Do not arm live execution until shadow quantities are compared trade-for-trade with the production Pine strategy and simulator execution is reconciled.

## Endpoints

- `GET /health`
- `GET /accounts`
- `POST /admin/refresh` — triggers a non-blocking read refresh
- `POST /webhook/tradingview/{hook_token}`

## Immediate next validation

1. Add the CrossTrade token and exact seven Tradovate account names.
2. Run `/health` and `/accounts`.
3. Confirm all seven live balances appear.
4. Verify prop rules and bootstrap MLL/high-water values once.
5. Enable **shadow** account routing only.
6. Feed real AutoProp alerts and compare Router quantity vs Pine quantity.
7. Only after parity: simulator order submission and latency benchmarking.
