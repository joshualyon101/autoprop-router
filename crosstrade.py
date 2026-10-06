from __future__ import annotations

import asyncio
import inspect
import time
import re
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

_transport_logger = logging.getLogger('autoprop.transport')


def _diagnostic_endpoint(path: str) -> str:
    """Keep endpoint type without leaking account names, order IDs, or query values."""
    path = path.split('?', 1)[0]
    path = re.sub(r'(/accounts/)(?!snapshot(?:/|$))[^/]+', r'\1{account}', path)
    path = re.sub(r'(/orders/)[^/]+', r'\1{order}', path)
    return re.sub(r'(/fills/order/)[^/]+', r'\1{order}', path)


class CrossTradeError(RuntimeError):
    pass


class BrokerMutationDisabled(CrossTradeError):
    """A non-GET broker request was blocked by the execution-mode firewall."""


class AmbiguousMutation(CrossTradeError):
    """Network/5xx ambiguity where blindly resending could duplicate a live order."""


class RateLimitExceeded(CrossTradeError):
    """CrossTrade explicitly rejected a request because the per-user rate limit was hit."""


class InstrumentContractError(CrossTradeError):
    """Broker execution symbol is invalid/untranslatable for the MNQ-only AutoProp route."""


class EntryAdmissionClosed(CrossTradeError):
    """A new-risk mutation missed its deadline or crossed a durable control fence."""


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
_ACCOUNT_BLOCKED_UNTIL: dict[str, float] = {}
_NEXT_REQUEST_AT = 0.0
_BURST_TOKENS = 20.0
_BURST_UPDATED_AT = time.monotonic()
_GET_SEMAPHORE: asyncio.Semaphore | None = None
_GET_SEMAPHORE_LOOP = None
_GET_SEMAPHORE_CAPACITY = 0


def _guard_open(guard) -> bool:
    value = guard()
    if inspect.isawaitable(value):
        close = getattr(value, 'close', None)
        if close is not None:
            close()
        raise CrossTradeError('entry admission guard must be synchronous')
    return bool(value)


def _rate_lock() -> asyncio.Lock:
    """Return a lock bound to the current event loop (safe for tests using asyncio.run)."""
    global _RATE_LOCK, _RATE_LOCK_LOOP, _BURST_TOKENS, _BURST_UPDATED_AT
    global _NEXT_REQUEST_AT, _BLOCKED_UNTIL
    loop = asyncio.get_running_loop()
    if _RATE_LOCK is None or _RATE_LOCK_LOOP is not loop:
        _RATE_LOCK = asyncio.Lock()
        _RATE_LOCK_LOOP = loop
        _RATE_TIMES.clear()
        _BURST_TOKENS = 20.0
        _BURST_UPDATED_AT = time.monotonic()
        _NEXT_REQUEST_AT = 0.0
        _BLOCKED_UNTIL = 0.0
        _ACCOUNT_BLOCKED_UNTIL.clear()
    return _RATE_LOCK


def _account_from_path(path: str) -> str | None:
    match = re.search(r"/accounts/([^/?]+)", str(path))
    return match.group(1) if match else None


def _get_semaphore(capacity: int) -> asyncio.Semaphore:
    """Return a process-wide GET semaphore bound to the current event loop."""
    global _GET_SEMAPHORE, _GET_SEMAPHORE_LOOP, _GET_SEMAPHORE_CAPACITY
    loop = asyncio.get_running_loop()
    capacity = max(1, int(capacity))
    if (_GET_SEMAPHORE is None or _GET_SEMAPHORE_LOOP is not loop
            or _GET_SEMAPHORE_CAPACITY != capacity):
        _GET_SEMAPHORE = asyncio.Semaphore(capacity)
        _GET_SEMAPHORE_LOOP = loop
        _GET_SEMAPHORE_CAPACITY = capacity
    return _GET_SEMAPHORE


async def _acquire_rate_slot(limit: int, window_seconds: float, *, path: str,
                             min_interval_seconds: float,
                             deadline_epoch: float | None = None,
                             admission_guard=None,
                             reserve_tokens: float = 0.0) -> None:
    """Acquire one paced process-wide request slot and honor active penalty windows."""
    global _BLOCKED_UNTIL, _NEXT_REQUEST_AT, _BURST_TOKENS, _BURST_UPDATED_AT
    limit = max(1, int(limit))
    window_seconds = max(1.0, float(window_seconds))
    min_interval_seconds = max(0.0, float(min_interval_seconds))
    reserve_tokens = max(0.0, min(19.0, float(reserve_tokens)))
    account = _account_from_path(path)
    while True:
        lock = _rate_lock()
        async with lock:
            now = time.monotonic()
            elapsed = max(0.0, now - _BURST_UPDATED_AT)
            _BURST_TOKENS = min(20.0, _BURST_TOKENS + elapsed * 3.0)
            _BURST_UPDATED_AT = now
            while _RATE_TIMES and now - _RATE_TIMES[0] >= window_seconds:
                _RATE_TIMES.popleft()

            account_blocked_until = _ACCOUNT_BLOCKED_UNTIL.get(account, 0.0) if account else 0.0
            blocked_until = max(_BLOCKED_UNTIL, account_blocked_until, _NEXT_REQUEST_AT)
            if now < blocked_until:
                delay = blocked_until - now
            elif len(_RATE_TIMES) < limit and _BURST_TOKENS >= 1.0 + reserve_tokens:
                if deadline_epoch is not None and time.time() > float(deadline_epoch):
                    raise EntryAdmissionClosed(
                        f"request {path} blocked: entry dispatch deadline expired"
                    )
                if admission_guard is not None and not _guard_open(admission_guard):
                    raise EntryAdmissionClosed(f"request {path} blocked by control fence")
                _RATE_TIMES.append(now)
                _BURST_TOKENS -= 1.0
                _NEXT_REQUEST_AT = now + min_interval_seconds
                return
            else:
                delays = [0.01]
                if len(_RATE_TIMES) >= limit:
                    delays.append(window_seconds - (now - _RATE_TIMES[0]))
                if _BURST_TOKENS < 1.0 + reserve_tokens:
                    delays.append((1.0 + reserve_tokens - _BURST_TOKENS) / 3.0)
                delay = max(delays)
        await asyncio.sleep(delay)


def _rate_limit_response(response: httpx.Response) -> tuple[str | None, float]:
    """Return the explicit rejection code and broker-directed retry delay."""
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
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        raw = payload.get("retryAfter", details.get("retryAfter"))
        if raw is not None:
            try:
                retry_after = max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        error = payload.get("error") or payload.get("code")
        if isinstance(error, dict):
            error = error.get("code") or error.get("error")
        code = str(error or "").strip().lower()
        if payload.get("success") is False and code in {
            "rate_limited", "broker_rate_limited", "snapshot_refresh_pending"
        }:
            return code, retry_after
    return None, retry_after


async def _apply_global_cooldown(seconds: float) -> None:
    global _BLOCKED_UNTIL
    seconds = max(0.0, float(seconds))
    lock = _rate_lock()
    async with lock:
        _BLOCKED_UNTIL = max(_BLOCKED_UNTIL, time.monotonic() + seconds)


async def _apply_account_cooldown(path: str, seconds: float) -> None:
    account = _account_from_path(path)
    if not account:
        await _apply_global_cooldown(seconds)
        return
    seconds = max(0.0, float(seconds))
    lock = _rate_lock()
    async with lock:
        _ACCOUNT_BLOCKED_UNTIL[account] = max(
            _ACCOUNT_BLOCKED_UNTIL.get(account, 0.0), time.monotonic() + seconds
        )


def _get_retry_delay(base: float, multiplier: float, maximum: float, attempt: int) -> float:
    delay = max(0.0, float(base)) * (max(1.0, float(multiplier)) ** max(0, attempt - 1))
    return min(max(0.0, float(maximum)), delay)


@dataclass
class CrossTradeClient:
    base_url: str
    token: str
    # This is a transport-boundary safety control, not a UI/readiness label.  Any
    # non-GET request is rejected before rate limiting or HTTP client creation while
    # the client is in shadow mode.  Callers may still choose to issue read-only GETs.
    execution_mode: str = "live"
    timeout: float = 4.0
    rate_limit_per_minute: int = 150
    rate_limit_window_seconds: float = 60.0
    rate_limit_max_retries: int = 8
    rate_limit_fallback_seconds: float = 1.0
    request_min_interval_seconds: float = 0.10
    mutation_min_interval_seconds: float = 0.02
    safe_get_max_concurrency: int = 2
    # Read-only broker calls are safe to retry on transient transport/snapshot-refresh
    # failures. Mutations retain the strict ambiguous-mutation no-resend contract.
    get_retry_max_retries: int = 3
    get_retry_delay_seconds: float = 0.75
    get_retry_backoff_multiplier: float = 2.0
    get_retry_max_delay_seconds: float = 5.0
    _http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _http_loop: Any = field(default=None, init=False, repr=False)
    transport_diagnostics: dict[str, Any] = field(
        default_factory=lambda: {'process_started_at_epoch': time.time(),
                                'transport_failures': 0, 'rate_limit_responses': 0},
        init=False, repr=False,
    )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _http_client(self) -> httpx.AsyncClient:
        """Reuse TCP/TLS connections instead of rebuilding a client for every broker call."""
        loop = asyncio.get_running_loop()
        if self._http is None or self._http_loop is not loop:
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
            self._http_loop = loop
        return self._http

    async def close(self) -> None:
        client, self._http = self._http, None
        self._http_loop = None
        if client is not None:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        method_u = method.upper()
        if method_u != "GET" and str(self.execution_mode).strip().lower() != "live":
            raise BrokerMutationDisabled(
                f"{method_u} {path} blocked: broker mutations are disabled in "
                f"{self.execution_mode!r} execution mode"
            )
        url = self.base_url.rstrip("/") + path
        deadline_epoch = kwargs.pop("_deadline_epoch", None)
        admission_guard = kwargs.pop("_admission_guard", None)
        get_attempts = 0
        request_started = time.monotonic()
        request_id = uuid.uuid4().hex[:16]
        while True:
            # The all-account snapshot is the ENTRY preflight immediately before the
            # mutation wave. CrossTrade documents a 20-request burst allowance, so do
            # not make the first PLACE inherit the slower background-GET pacing gap.
            # It still consumes a token and remains inside the shared rolling budget.
            request_interval = (
                self.mutation_min_interval_seconds
                if method_u != "GET" or path == "/v1/api/tv/accounts/snapshot"
                else self.request_min_interval_seconds
            )
            rate_started = time.monotonic()
            await _acquire_rate_slot(
                self.rate_limit_per_minute, self.rate_limit_window_seconds,
                path=path,
                min_interval_seconds=request_interval,
                deadline_epoch=deadline_epoch,
                admission_guard=admission_guard,
                # Preserve enough of CrossTrade's documented 20-token burst for one
                # account snapshot plus eight entry/control mutations. Low-priority reads
                # refill above this reserve instead of starving safety commands.
                reserve_tokens=(0.0 if method_u != "GET"
                                or path == "/v1/api/tv/accounts/snapshot" else 9.0),
            )
            rate_wait = time.monotonic() - rate_started
            # The final admission check deliberately occurs after rate waiting and directly
            # before the irreversible broker call.
            if deadline_epoch is not None and time.time() > float(deadline_epoch):
                raise EntryAdmissionClosed(f"{method_u} {path} blocked: entry dispatch deadline expired")
            if admission_guard is not None and not _guard_open(admission_guard):
                raise EntryAdmissionClosed(f"{method_u} {path} blocked by control fence")
            semaphore_started = time.monotonic()
            semaphore_wait = 0.0
            http_started = None
            try:
                client = self._http_client()
                if method_u == "GET":
                    async with _get_semaphore(self.safe_get_max_concurrency):
                        semaphore_wait = time.monotonic() - semaphore_started
                        http_started = time.monotonic()
                        r = await client.request(method_u, url, headers=self._headers(), **kwargs)
                else:
                    http_started = time.monotonic()
                    r = await client.request(method_u, url, headers=self._headers(), **kwargs)
            except httpx.TransportError as e:
                exhausted = (method_u != 'GET' or get_attempts >= max(0, int(self.get_retry_max_retries)))
                diagnostic = {
                    'request_id': request_id, 'method': method_u,
                    'endpoint': _diagnostic_endpoint(path),
                    'error_type': type(e).__name__, 'attempt': get_attempts + 1,
                    'exhausted': exhausted, 'observed_at_epoch': time.time(),
                    'configured_timeout_seconds': self.timeout,
                    'rate_wait_seconds': round(rate_wait, 4),
                    'semaphore_wait_seconds': round(semaphore_wait, 4),
                    'http_elapsed_seconds': round(time.monotonic() - (http_started or semaphore_started), 4),
                    'request_elapsed_seconds': round(time.monotonic() - request_started, 4),
                }
                self.transport_diagnostics['transport_failures'] += 1
                self.transport_diagnostics['last_transport_failure'] = diagnostic
                _transport_logger.warning('AutoProp broker transport failure %s', json.dumps(diagnostic))
                detail = str(e).strip() or type(e).__name__
                if method_u in {"POST", "PUT", "PATCH", "DELETE"}:
                    raise AmbiguousMutation(f"{method_u} {path} network error: {detail}") from e
                if method_u == "GET" and get_attempts < max(0, int(self.get_retry_max_retries)):
                    get_attempts += 1
                    await asyncio.sleep(_get_retry_delay(
                        self.get_retry_delay_seconds, self.get_retry_backoff_multiplier,
                        self.get_retry_max_delay_seconds, get_attempts,
                    ))
                    continue
                raise CrossTradeError(f"{method_u} {path} network error after {get_attempts + 1} attempt(s): {detail}") from e

            if r.status_code == 429:
                rejection, retry_after = _rate_limit_response(r)
                diagnostic = {
                    'request_id': request_id, 'method': method_u,
                    'endpoint': _diagnostic_endpoint(path), 'status_code': 429,
                    'rejection': rejection, 'retry_after_seconds': retry_after,
                    'attempt': get_attempts + 1, 'observed_at_epoch': time.time(),
                    'rate_wait_seconds': round(rate_wait, 4),
                    'semaphore_wait_seconds': round(semaphore_wait, 4),
                    'http_elapsed_seconds': round(time.monotonic() - (http_started or semaphore_started), 4),
                }
                self.transport_diagnostics['rate_limit_responses'] += 1
                self.transport_diagnostics['last_rate_limit_response'] = diagnostic
                _transport_logger.warning('AutoProp broker rate limit %s', json.dumps(diagnostic))
                retry_after = retry_after if retry_after > 0 else self.rate_limit_fallback_seconds
                cooldown = max(retry_after, _get_retry_delay(
                    self.get_retry_delay_seconds, self.get_retry_backoff_multiplier,
                    self.get_retry_max_delay_seconds, max(1, get_attempts + 1),
                ))
                if rejection == "snapshot_refresh_pending":
                    await _apply_account_cooldown(path, cooldown)
                else:
                    # CrossTrade's public budget and Tradovate's broker 429 are both
                    # user/identity scoped, not account scoped. Continuing on sibling
                    # accounts during a broker penalty can extend the block.
                    await _apply_global_cooldown(cooldown)
                if (method_u == "GET"
                        and rejection in {"snapshot_refresh_pending", "broker_rate_limited", "rate_limited"}
                        and get_attempts < max(0, int(self.get_retry_max_retries))):
                    get_attempts += 1
                    continue
                # Mutations are never resent by the transport layer, even after an explicit
                # 429. The caller must reconcile the broker state before any new mutation.
                raise RateLimitExceeded(
                    f"HTTP 429 {rejection or 'rate_limit_unknown'} after {get_attempts + 1} attempt(s): {r.text[:300]}"
                )

            if r.status_code >= 500 and method_u in {"POST", "PUT", "PATCH", "DELETE"}:
                raise AmbiguousMutation(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                body = r.text[:300]
                lower = body.lower()
                if "cannot translate" in lower and "tradovate symbol" in lower:
                    raise InstrumentContractError(f"HTTP {r.status_code}: {body}")
                raise CrossTradeError(f"HTTP {r.status_code}: {body}")
            if not r.content:
                return {}
            try:
                payload = r.json()
            except Exception as exc:
                if method_u in {"POST", "PUT", "PATCH", "DELETE"}:
                    raise AmbiguousMutation(
                        f"{method_u} {path} returned malformed success JSON"
                    ) from exc
                raise CrossTradeError(f"GET {path} returned malformed JSON") from exc
            if not isinstance(payload, dict):
                if method_u in {"POST", "PUT", "PATCH", "DELETE"}:
                    raise AmbiguousMutation(
                        f"{method_u} {path} returned a non-object success body"
                    )
                raise CrossTradeError(f"GET {path} returned a non-object body")
            if payload.get("success") is False:
                detail = str(payload.get("error") or payload.get("message") or payload)[:300]
                raise CrossTradeError(
                    f"{method_u} {path} API rejected request: {detail}"
                )
            if get_attempts:
                diagnostic = {
                    'request_id': request_id, 'method': method_u,
                    'endpoint': _diagnostic_endpoint(path), 'attempts': get_attempts + 1,
                    'request_elapsed_seconds': round(time.monotonic() - request_started, 4),
                    'observed_at_epoch': time.time(),
                }
                self.transport_diagnostics['last_retry_recovery'] = diagnostic
                _transport_logger.warning('AutoProp broker read recovered after retry %s', json.dumps(diagnostic))
            return payload

    async def list_accounts(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/api/tv/accounts")

    async def accounts_snapshot(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/api/tv/accounts/snapshot")

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

    async def place(self, account: str, payload: dict[str, Any], *,
                    deadline_epoch: float | None = None,
                    admission_guard=None) -> dict[str, Any]:
        if "instrument" not in payload:
            raise InstrumentContractError("CrossTrade PLACE payload missing instrument")
        normalized = dict(payload)
        normalized["instrument"] = normalize_tradovate_symbol(normalized["instrument"])
        return await self._request(
            "POST", f"/v1/api/tv/accounts/{account}/orders/place", json=normalized,
            _deadline_epoch=deadline_epoch, _admission_guard=admission_guard,
        )

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
