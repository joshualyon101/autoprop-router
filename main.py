from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Form
from fastapi.responses import JSONResponse, RedirectResponse

from config import Settings
from crosstrade import CrossTradeClient
from models import TradeSignal
from persistence import Store
from router import AutoPropRouter
from state import AccountStateCache, StatePoller
from admin_ui import render_account_manager


settings = Settings.from_env()
accounts = settings.load_accounts()
store = Store(settings.sqlite_path)
client = CrossTradeClient(settings.crosstrade_base_url, settings.crosstrade_token, settings.request_timeout_seconds)
cache = AccountStateCache(
    accounts,
    store,
    auto_discovery=settings.auto_discovery,
    fundednext_default_model=settings.fundednext_default_model,
)
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


app = FastAPI(title="AutoProp Router", version="0.3.0", lifespan=lifespan)


@app.get("/health")
async def health():
    states = await cache.snapshot()
    return {
        "ok": True,
        "version": "0.3.0",
        "execution_mode": settings.execution_mode,
        "live_armed": settings.live_enabled,
        "cached_accounts": len(states),
        "auto_discovery": settings.auto_discovery,
        "registry_accounts": len(store.list_registry()),
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


@app.get("/admin/registry/{hook_token}")
async def account_registry(hook_token: str):
    if not settings.webhook_token or hook_token != settings.webhook_token:
        raise HTTPException(404, "Not found")
    return {
        "manual_on_off": "CrossTrade Tradovate Account Manager -> Block Signals",
        "accounts": store.list_registry(),
    }



@app.get("/admin/accounts/{hook_token}")
async def admin_accounts(hook_token: str):
    if not settings.webhook_token or hook_token != settings.webhook_token:
        raise HTTPException(404, "Not found")
    states = await cache.snapshot()
    return render_account_manager(
        token=hook_token,
        registry=store.list_registry(),
        states=states,
        overrides=store.list_overrides(),
        mode=settings.execution_mode,
    )


@app.post("/admin/account/{hook_token}/{account_id}/override")
async def save_account_override(
    hook_token: str,
    account_id: str,
    firm: str = Form(...),
    program: str = Form(...),
    phase: str = Form(...),
    starting_balance: float = Form(...),
    max_loss: float = Form(...),
    profit_target: float = Form(0),
    max_micros: int = Form(...),
    consistency_pct: str = Form(""),
    drawdown_type: str = Form(...),
    risk_profile: str = Form("Standard"),
    mll_floor: str = Form(""),
    mll_locked: str = Form("false"),
    rules_verified: str | None = Form(None),
    risk_ready: str | None = Form(None),
):
    if not settings.webhook_token or hook_token != settings.webhook_token:
        raise HTTPException(404, "Not found")

    payload = {
        "firm": firm.strip(),
        "program": program.strip(),
        "phase": phase,
        "starting_balance": starting_balance,
        "max_loss": max_loss,
        "profit_target": profit_target,
        "max_micros": max_micros,
        "consistency_pct": float(consistency_pct) if consistency_pct.strip() else None,
        "drawdown_type": drawdown_type,
        "risk_profile": risk_profile,
        "rules_verified": rules_verified == "true",
        "risk_ready": risk_ready == "true",
    }
    if mll_floor.strip():
        payload["bootstrap_mll_floor"] = float(mll_floor)

    store.set_override(account_id, payload)

    if mll_floor.strip():
        floor = float(mll_floor)
        # For EOD trailing, the corresponding peak is floor + max_loss.
        peak = floor + max_loss
        store.reset_prop_state(
            account_id,
            mll_floor=floor,
            peak_eod_balance=peak,
            live_high_water=max(starting_balance, peak),
            mll_locked=(mll_locked == "true"),
        )

    await cache.apply_override_now(account_id)
    return RedirectResponse(
        url=f"/admin/accounts/{hook_token}#edit-{account_id}",
        status_code=303,
    )


@app.post("/admin/account/{hook_token}/{account_id}/override/reset")
async def reset_account_override(hook_token: str, account_id: str):
    if not settings.webhook_token or hook_token != settings.webhook_token:
        raise HTTPException(404, "Not found")
    store.delete_override(account_id)
    await poller.refresh_once()
    return RedirectResponse(
        url=f"/admin/accounts/{hook_token}#edit-{account_id}",
        status_code=303,
    )


@app.get("/admin/live-readiness/{hook_token}")
async def live_readiness(hook_token: str):
    if not settings.webhook_token or hook_token != settings.webhook_token:
        raise HTTPException(404, "Not found")
    states = await cache.snapshot()
    configs = await cache.configs_snapshot()
    problems = []
    if not settings.crosstrade_token:
        problems.append("CROSSTRADE_TOKEN missing")
    if not settings.webhook_token:
        problems.append("AUTOPROP_WEBHOOK_TOKEN missing")
    if not settings.sqlite_path.startswith("/data/"):
        problems.append("SQLite is not on Railway persistent /data volume")
    if poller.last_success is None:
        problems.append("No successful CrossTrade snapshot yet")
    if settings.execution_require_bracket and not settings.use_native_atm:
        problems.append("Native ATM disabled while bracket requirement is enabled")

    selected = configs
    if settings.execution_mode == "canary":
        selected = [c for c in configs if c.id == settings.canary_account_id]
        if not selected:
            problems.append("CANARY_ACCOUNT_ID does not match a discovered account")

    for c in selected:
        if not c.rules_verified:
            problems.append(f"{c.id}: rules not verified")
        if not c.risk_ready:
            problems.append(f"{c.id}: MLL/risk state not ready")
        if c.id not in states:
            problems.append(f"{c.id}: no hot state")

    return {
        "ready": len(problems) == 0,
        "execution_mode": settings.execution_mode,
        "armed": settings.live_enabled,
        "native_atm": settings.use_native_atm,
        "require_market_position": settings.use_crosstrade_position_gate,
        "canary_account_id": settings.canary_account_id or None,
        "canary_max_qty": settings.canary_max_qty,
        "problems": problems,
    }


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
