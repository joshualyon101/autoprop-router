from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
import httpx


class CrossTradeError(RuntimeError):
    pass


class AmbiguousMutation(CrossTradeError):
    """Network/5xx ambiguity where blindly resending could duplicate a live order."""


@dataclass
class CrossTradeClient:
    base_url: str
    token: str
    timeout: float = 4.0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.request(method, url, headers=self._headers(), **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                raise AmbiguousMutation(str(e)) from e
            raise CrossTradeError(str(e)) from e
        if r.status_code >= 500 and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            raise AmbiguousMutation(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise CrossTradeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def list_accounts(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/api/tv/accounts")

    async def get_account(self, account: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/api/tv/accounts/{account}")

    async def positions(self, account: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/api/tv/accounts/{account}/positions")

    async def position(self, account: str, instrument: str = "MNQ1!") -> dict[str, Any]:
        return await self._request("GET", f"/v1/api/tv/accounts/{account}/position", params={"instrument": instrument})

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
        return await self._request("POST", f"/v1/api/tv/accounts/{account}/orders/place", json=payload)

    async def change(self, account: str, order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"/v1/api/tv/accounts/{account}/orders/{order_id}/change", json=payload)

    async def flatten(self, account: str, instrument: str = "MNQ") -> dict[str, Any]:
        return await self._request("POST", f"/v1/api/tv/accounts/{account}/positions/flatten",
                                   json={"instrument": instrument})
