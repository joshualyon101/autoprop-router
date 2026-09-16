from __future__ import annotations

import asyncio
import time
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx


class CrossTradeError(RuntimeError):
    pass


class AmbiguousMutation(CrossTradeError):
    """Network/5xx ambiguity where blindly resending could duplicate a live order."""


class RateLimitExceeded(CrossTradeError):
    """CrossTrade explicitly rejected a request because the per-user rate limit was hit."""


class InstrumentContractError(CrossTradeError):
    """Broker execution symbol is invalid/untranslatable for the MNQ-only AutoProp route."""


_MNQ_DATED_RE = re.compile(r"^MNQ[FGHJKMNQUVXZ]\d{1,2}$")


def normalize_tradovate_symbol(instrument: str) -> str:
    """Return a CrossTrade/Tradovate-routable MNQ symbol and fail closed otherwise.

    AutoProp is an MNQ-only system.  The canonical strategy/root identity may be ``MNQ``
    while CrossTrade requires a resolvable futures contract.  ``MNQ1!`` delegates front-
    month resolution to CrossTrade and is therefore the default production transport form.
    Explicit dated Tradovate MNQ contracts remain valid for diagnostics/migrations.
    """
    raw = str(instrument or "").strip().upper()
    if raw in {"MNQ", "MNQ1!"}:
        return "MNQ1!"
    if _MNQ_DATED_RE.fullmatch(raw):
        return raw
    raise InstrumentContractError(
        f"unsupported AutoProp execution instrument {instrument!r}; expected MNQ/MNQ1! or dated MNQ contract"
    )


# CrossTrade currently documents/enforces 180 requests/minute per user.  Keep a
# process-wide rolling budget below that ceiling so independently-created LiveRouter
# clients cannot stampede the same CrossTrade user.  This limiter deliberately allows
# bursts (important for fast multi-account execution) while capping the rolling minute.
_RATE_TIMES: deque[float] = deque()
_RATE_LOCK: asyncio.Lock | None = None
_RATE_LOCK_LOOP = None
_BLOCKED_UNTIL = 0.0


def _rate_lock() -> asyncio.Lock:
    """Return a lock bound to the current event loop (safe for tests using asyncio.run)."""
    global _RATE_LOCK, _RATE_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _RATE_LOCK is None or _RATE_LOCK_LOOP is not loop:
        _RATE_LOCK = asyncio.Lock()
        _RATE_LOCK_LOOP = loop
    return _RATE_LOCK


async def _acquire_rate_slot(limit: int, window_seconds: float) -> None:
    """Acquire one process-wide CrossTrade request slot without exceeding the local budget."""
    global _BLOCKED_UNTIL
    limit = max(1, int(limit))
    window_seconds = max(1.0, float(window_seconds))
    while True:
        lock = _rate_lock()
        async with lock:
            now = time.monotonic()
            while _RATE_TIMES and now - _RATE_TIMES[0] >= window_seconds:
                _RATE_TIMES.popleft()

            if now < _BLOCKED_UNTIL:
                delay = _BLOCKED_UNTIL - now
            elif len(_RATE_TIMES) < limit:
                _RATE_TIMES.append(now)
                return
            else:
                delay = max(0.01, window_seconds - (now - _RATE_TIMES[0]))
        await asyncio.sleep(delay)


def _explicit_rate_limit_response(response: httpx.Response) -> tuple[bool, float]:
    """Return (is_explicit_rate_limit_rejection, retry_after_seconds)."""
    retry_after = 1.0
    raw_header = response.headers.get("Retry-After")
    if raw_header:
        try:
            retry_after = max(0.0, float(raw_header))
        except ValueError:
            pass
    payload: dict[str, Any] | None = None
    try:
        candidate = response.json()
        if isinstance(candidate, dict):
            payload = candidate
    except Exception:
        payload = None
    if payload is not None:
        raw = payload.get("retryAfter")
        if raw is not None:
            try:
                retry_after = max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        explicit = payload.get("success") is False and str(payload.get("error") or "").lower() == "rate_limited"
        return explicit, retry_after
    return False, retry_after


async def _apply_global_cooldown(seconds: float) -> None:
    global _BLOCKED_UNTIL
    seconds = max(0.0, float(seconds))
    lock = _rate_lock()
    async with lock:
        _BLOCKED_UNTIL = max(_BLOCKED_UNTIL, time.monotonic() + seconds)


@dataclass
class CrossTradeClient:
    base_url: str
    token: str
    timeout: float = 4.0
    rate_limit_per_minute: int = 150
    rate_limit_window_seconds: float = 60.0
    rate_limit_max_retries: int = 8
    rate_limit_fallback_seconds: float = 1.0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        method_u = method.upper()
        attempts = 0
        while True:
            await _acquire_rate_slot(self.rate_limit_per_minute, self.rate_limit_window_seconds)
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    r = await client.request(method_u, url, headers=self._headers(), **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                detail = str(e).strip() or type(e).__name__
                if method_u in {"POST", "PUT", "PATCH", "DELETE"}:
                    raise AmbiguousMutation(f"{method_u} {path} network error: {detail}") from e
                raise CrossTradeError(f"{method_u} {path} network error: {detail}") from e

            if r.status_code == 429:
                explicit, retry_after = _explicit_rate_limit_response(r)
                retry_after = retry_after if retry_after > 0 else self.rate_limit_fallback_seconds
                # An explicit {success:false,error:"rate_limited"} response proves the
                # broker rejected the request. It is therefore safe to retry even for a
                # mutation; this is not the ambiguous PLACE case guarded above.
                if explicit and attempts < max(0, int(self.rate_limit_max_retries)):
                    attempts += 1
                    await _apply_global_cooldown(max(retry_after, self.rate_limit_fallback_seconds))
                    continue
                raise RateLimitExceeded(f"HTTP 429: {r.text[:300]}")

            if r.status_code >= 500 and method_u in {"POST", "PUT", "PATCH", "DELETE"}:
                raise AmbiguousMutation(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                body = r.text[:300]
                lower = body.lower()
                if "cannot translate" in lower and "tradovate symbol" in lower:
                    raise InstrumentContractError(f"HTTP {r.status_code}: {body}")
                raise CrossTradeError(f"HTTP {r.status_code}: {body}")
            return r.json() if r.content else {}

    async def list_accounts(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/api/tv/accounts")

    async def get_account(self, account: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/api/tv/accounts/{account}")

    async def positions(self, account: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/api/tv/accounts/{account}/positions")

    async def position(self, account: str, instrument: str = "MNQ1!") -> dict[str, Any]:
        symbol = normalize_tradovate_symbol(instrument)
        return await self._request("GET", f"/v1/api/tv/accounts/{account}/position", params={"instrument": symbol})

    async def orders(self, account: str) -> dict[str, Any]:
        # Account-scoped working orders.
        return await self._request("GET", f"/v1/api/tv/accounts/{account}/orders")

    async def all_orders(self) -> dict[str, Any]:
        # Current-session Tradovate order history. Used only for reconciliation of an
        # ambiguous PLACE by its caller-supplied clOrdId; never as a mutation source.
        return await self._request("GET", "/v1/api/tv/orders")

    async def fills_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/api/tv/fills/order/{order_id}")

    async def fills_history(self, *, account: str, start: str, end: str,
                            cursor: str | None = None, limit: int = 1000) -> dict[str, Any]:
        params = {"account": account, "from": start, "to": end, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/v1/api/tv/fills/history", params=params)

    async def place(self, account: str, payload: dict[str, Any]) -> dict[str, Any]:
        if "instrument" not in payload:
            raise InstrumentContractError("CrossTrade PLACE payload missing instrument")
        normalized = dict(payload)
        normalized["instrument"] = normalize_tradovate_symbol(normalized["instrument"])
        return await self._request("POST", f"/v1/api/tv/accounts/{account}/orders/place", json=normalized)

    async def order_status(self, account: str, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/api/tv/accounts/{account}/orders/{order_id}/status")

    async def order_lifecycle(self, account: str, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/api/tv/accounts/{account}/orders/{order_id}/lifecycle")

    async def cancel_order(self, account: str, order_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/api/tv/accounts/{account}/orders/{order_id}/cancel", json={})

    async def change(self, account: str, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"/v1/api/tv/accounts/{account}/orders/{order_id}/change", json=payload)

    async def flatten(self, account: str, instrument: str = "MNQ1!") -> dict[str, Any]:
        symbol = normalize_tradovate_symbol(instrument)
        return await self._request("POST", f"/v1/api/tv/accounts/{account}/positions/flatten",
                                   json={"instrument": symbol})
