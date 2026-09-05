from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from config import Settings
from crosstrade import CrossTradeClient
from models import TradeSignal
from persistence import Store
from router import AutoPropRouter
from state import AccountStateCache, StatePoller


settings = Settings.from_env()
accounts = settings.load_accounts()
store = Store(settings.sqlite_path)
client = CrossTradeClient(settings.crosstrade_base_url, settings.crosstrade_token, settings.request_timeout_seconds)
cache = AccountStateCache(accounts, store)
poller = StatePoller(client, cache, settings.state_poll_seconds)
router = AutoPropRouter(settings, accounts, client, cache, store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Read-only background account refresh. It is intentionally decoupled from webhook execution.
    if settings.crosstrade_token:
        poller.start()
        try:
            await asyncio.wait_for(poller.refresh_once(), timeout=settings.request_timeout_seconds + 1)
        except Exception:
            pass
    yield
    await poller.stop()
    await client.close()


app = FastAPI(title="AutoProp Router", version="0.1.1", lifespan=lifespan)


@app.get("/health")
async def health():
    states = await cache.snapshot()
    return {
        "ok": True,
        "version": "0.1.0",
        "execution_mode": "live" if settings.live_enabled else "shadow",
        "live_armed": settings.live_enabled,
        "cached_accounts": len(states),
        "poll_last_success": poller.last_success,
        "poll_last_error": poller.last_error,
        "state_poll_seconds": settings.state_poll_seconds,
        "max_state_age_seconds": settings.max_state_age_seconds,
    }


@app.get("/admin/discover/{hook_token}")
async def discover_linked_accounts(hook_token: str):
    """Protected read-only passthrough of CrossTrade linked Tradovate accounts."""
    if not settings.webhook_token or hook_token != settings.webhook_token:
        raise HTTPException(404, "Not found")
    if not settings.crosstrade_token:
        raise HTTPException(503, "CROSSTRADE_TOKEN is not configured")
    raw = await client.get_accounts()
    return raw


@app.get("/accounts")
async def account_state():
    states = await cache.snapshot()
    return {
        "accounts": {
            k: v.model_dump(mode="json", exclude={"raw"})
            for k, v in states.items()
        }
    }


@app.post("/admin/refresh")
async def refresh(background_tasks: BackgroundTasks):
    if not settings.crosstrade_token:
        raise HTTPException(503, "CROSSTRADE_TOKEN is not configured")
    # Non-blocking by design; returns immediately.
    background_tasks.add_task(poller.refresh_once)
    return {"accepted": True}


@app.post("/webhook/tradingview/{hook_token}")
async def tradingview_webhook(hook_token: str, signal: TradeSignal):
    if not settings.webhook_token or hook_token != settings.webhook_token:
        raise HTTPException(404, "Not found")
    response = await router.route(signal)
    return JSONResponse(response.model_dump(mode="json"))
