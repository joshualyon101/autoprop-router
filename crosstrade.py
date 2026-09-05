from __future__ import annotations

from typing import Any
from urllib.parse import quote
import httpx


class CrossTradeError(RuntimeError):
    pass


class CrossTradeClient:
    """
    CrossTrade's server-side Tradovate REST surface.

    One long-lived AsyncClient is deliberately reused so TLS/TCP connections stay hot.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 4.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            http2=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40, keepalive_expiry=60),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    async def close(self):
        await self.client.aclose()

    async def _json(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            raise CrossTradeError(f"CrossTrade {response.status_code}: {response.text[:500]}")
        try:
            return response.json()
        except Exception as exc:
            raise CrossTradeError(f"CrossTrade returned non-JSON: {response.text[:500]}") from exc

    async def get_accounts(self) -> Any:
        return await self._json(await self.client.get("/v1/api/tv/accounts"))

    async def get_accounts_snapshot(self) -> Any:
        return await self._json(await self.client.get("/v1/api/tv/accounts/snapshot"))

    async def get_orders(self, account: str) -> Any:
        return await self._json(
            await self.client.get(f"/v1/api/tv/accounts/{quote(account, safe='')}/orders")
        )

    async def get_positions(self, account: str) -> Any:
        return await self._json(
            await self.client.get(f"/v1/api/tv/accounts/{quote(account, safe='')}/positions")
        )

    async def place_order(
        self,
        *,
        account: str,
        instrument: str,
        action: str,
        qty: int,
        order_id: str,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        require_market_position: str | None = None,
        max_positions: int | None = None,
        atm_targets: str | None = None,
        atm_stops: str | None = None,
        atm_qtys: str | None = None,
        atm_breakeven: int | None = None,
        atm_breakeven_offset: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instrument": instrument,
            "action": action.lower(),
            "qty": qty,
            "orderType": "market",
            "tif": "day",
            "orderId": order_id,
            "text": "AutoProp Router",
        }
        if atm_targets is not None:
            payload["atmTargets"] = atm_targets
            payload["atmStops"] = atm_stops
            if atm_qtys is not None:
                payload["atmQtys"] = atm_qtys
            if atm_breakeven is not None:
                payload["atmBreakeven"] = atm_breakeven
            if atm_breakeven_offset is not None:
                payload["atmBreakevenOffset"] = atm_breakeven_offset
        else:
            if take_profit is not None:
                payload["takeProfit"] = take_profit
            if stop_loss is not None:
                payload["stopLoss"] = stop_loss

        if require_market_position:
            payload["requireMarketPosition"] = require_market_position
        if max_positions is not None:
            payload["maxPositions"] = max_positions

        url = f"/v1/api/tv/accounts/{quote(account, safe='')}/orders/place"
        return await self._json(await self.client.post(url, json=payload))

    async def flatten_position(
        self,
        *,
        account: str,
        instrument: str,
    ) -> dict[str, Any]:
        url = f"/v1/api/tv/accounts/{quote(account, safe='')}/positions/flatten"
        return await self._json(
            await self.client.post(url, json={"instrument": instrument})
        )
