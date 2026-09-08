from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

from crosstrade import AmbiguousMutation, CrossTradeClient, CrossTradeError
from models import Allocation

TICK = 0.25


class ProtectionFailure(RuntimeError):
    pass


def tick_equal(a: float | None, b: float | None, tick: float = TICK) -> bool:
    if a is None or b is None:
        return False
    # Prices must agree at exact tick index; no loose floating tolerance.
    return int(round(float(a) / tick)) == int(round(float(b) / tick))


def _order_id(o: dict[str, Any]) -> str:
    return str(o.get("id") or o.get("orderId") or o.get("order_id") or "")


def _qty(o: dict[str, Any]) -> int:
    return int(float(o.get("qty") or o.get("quantity") or o.get("orderQty") or 0))


def _price(o: dict[str, Any], kind: str) -> float | None:
    keys = ("limitPrice", "price") if kind == "target" else ("stopPrice", "price")
    for k in keys:
        if o.get(k) is not None:
            return float(o[k])
    return None


def classify_children(orders: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    targets, stops = [], []
    for o in orders:
        typ = str(o.get("orderType") or o.get("type") or "").lower()
        if "stop" in typ:
            stops.append(o)
        elif "limit" in typ:
            targets.append(o)
    return targets, stops


def _owned(orders: Iterable[dict[str, Any]], owned_ids: set[str]) -> list[dict[str, Any]]:
    return [o for o in orders if _order_id(o) in owned_ids]


def verify_single_bracket(orders: Iterable[dict[str, Any]], alloc: Allocation,
                          owned_ids: set[str] | None = None) -> bool:
    rows = list(orders)
    if owned_ids is not None:
        rows = _owned(rows, owned_ids)
    targets, stops = classify_children(rows)
    target_qty = sum(_qty(o) for o in targets if tick_equal(_price(o, "target"), alloc.tp1))
    stop_qty = sum(_qty(o) for o in stops if tick_equal(_price(o, "stop"), alloc.stop))
    # Exact coverage, not >=: unexpected extra owned protection is an ambiguous topology.
    return target_qty == alloc.qty and stop_qty == alloc.qty


def verify_core_bracket(orders: Iterable[dict[str, Any]], alloc: Allocation,
                        owned_ids: set[str] | None = None) -> bool:
    rows = list(orders)
    if owned_ids is not None:
        rows = _owned(rows, owned_ids)
    targets, stops = classify_children(rows)
    tp1_qty = sum(_qty(o) for o in targets if tick_equal(_price(o, "target"), alloc.tp1))
    tp2_qty = sum(_qty(o) for o in targets if alloc.tp2 is not None and tick_equal(_price(o, "target"), alloc.tp2))
    stop_qty = sum(_qty(o) for o in stops if tick_equal(_price(o, "stop"), alloc.stop))
    return tp1_qty == alloc.tp1_qty and tp2_qty == alloc.runner_qty and stop_qty == alloc.qty


def _data_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    d = payload.get("data", payload)
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    if isinstance(d, dict):
        for key in ("orders", "items", "data"):
            if isinstance(d.get(key), list):
                return [x for x in d[key] if isinstance(x, dict)]
        return [d]
    return []


def _place_child_ids(result: dict[str, Any]) -> set[str]:
    """Extract broker-returned OSO child IDs when CrossTrade provides them."""
    containers = [result]
    for key in ("response", "data"):
        if isinstance(result.get(key), dict):
            containers.append(result[key])
    out: set[str] = set()
    for c in containers:
        for key in ("oso1Id", "oso2Id"):
            if c.get(key) is not None:
                out.add(str(c[key]))
        ids = c.get("osoChildIds")
        if isinstance(ids, list):
            out.update(str(x) for x in ids if x is not None)
        elif isinstance(ids, str):
            out.update(x.strip() for x in ids.split(",") if x.strip())
    return out


def _place_parent_id(result: dict[str, Any]) -> str | None:
    containers = [result]
    for key in ("response", "data"):
        if isinstance(result.get(key), dict):
            containers.append(result[key])
    for c in containers:
        v = c.get("orderId") or c.get("id")
        if v is not None:
            return str(v)
    return None


def _custom_id_match(o: dict[str, Any], custom_order_id: str) -> bool:
    return any(str(o.get(k) or "") == custom_order_id
               for k in ("clOrdId", "orderId", "customOrderId", "custom_order_id"))


def _desired_matches(o: dict[str, Any], payload: dict[str, Any]) -> bool:
    if "qty" in payload and _qty(o) != int(payload["qty"]):
        return False
    if "limitPrice" in payload and not tick_equal(_price(o, "target"), float(payload["limitPrice"])):
        return False
    if "stopPrice" in payload and not tick_equal(_price(o, "stop"), float(payload["stopPrice"])):
        return False
    return True


@dataclass
class ExecutionReceipt:
    result: dict[str, Any]
    child_order_ids: list[str]
    parent_order_id: str | None = None


@dataclass
class Executor:
    client: CrossTradeClient
    bracket_confirm_retries: int = 12
    bracket_confirm_delay: float = 0.25
    change_retries: int = 3
    change_delay: float = 0.25

    async def _working_orders(self, account: str) -> list[dict[str, Any]]:
        return _data_rows(await self.client.orders(account))

    async def _all_orders(self) -> list[dict[str, Any]]:
        fn = getattr(self.client, "all_orders", None)
        if fn is None:
            return []
        return _data_rows(await fn())

    async def _reconcile_custom_order(self, account: str, custom_order_id: str) -> str | None:
        # Search working/history rows for caller-supplied clOrdId and return the real
        # Tradovate parent order id. No PLACE resend occurs.
        for o in await self._working_orders(account):
            if _custom_id_match(o, custom_order_id) and _order_id(o):
                return _order_id(o)
        for o in await self._all_orders():
            if _custom_id_match(o, custom_order_id) and _order_id(o):
                return _order_id(o)
        return None

    async def _discover_owned_children(self, account: str, before_ids: set[str],
                                       response_ids: set[str]) -> tuple[list[dict[str, Any]], set[str]]:
        rows = await self._working_orders(account)
        present = {_order_id(o) for o in rows if _order_id(o)}
        if response_ids:
            owned = response_ids & present
            # If response named children that are not yet visible, wait rather than widening ownership.
            return rows, owned
        # Ambiguous/no-child-ID fallback: only orders newly appearing after this placement can
        # be considered. Never adopt pre-existing account orders by price/quantity similarity.
        return rows, present - before_ids

    async def place_single(self, account: str, alloc: Allocation, custom_order_id: str) -> ExecutionReceipt:
        before = await self._working_orders(account)
        before_ids = {_order_id(o) for o in before if _order_id(o)}
        payload = {"instrument": "MNQ", "action": "buy" if alloc.side == "LONG" else "sell",
                   "qty": alloc.qty, "orderType": "market", "orderId": custom_order_id,
                   "takeProfit": alloc.tp1, "stopLoss": alloc.stop,
                   "text": f"AutoProp {alloc.engine} {alloc.event_id}"}
        try:
            result = await self.client.place(account, payload)
        except AmbiguousMutation:
            # NEVER blindly resend an ambiguous PLACE.
            parent_id = await self._reconcile_custom_order(account, custom_order_id)
            if not parent_id:
                raise ProtectionFailure("ambiguous PLACE could not be reconciled; no resend")
            result = {"reconciled": True, "response": {"orderId": parent_id}}
        parent_id = _place_parent_id(result)
        response_ids = _place_child_ids(result)
        for _ in range(self.bracket_confirm_retries):
            orders, owned = await self._discover_owned_children(account, before_ids, response_ids)
            if owned and verify_single_bracket(orders, alloc, owned):
                return ExecutionReceipt(result, sorted(owned), parent_id)
            await asyncio.sleep(self.bracket_confirm_delay)
        await self.client.flatten(account, "MNQ")
        raise ProtectionFailure("single-target protective bracket failed owned exact readback; flattened")

    async def place_core(self, account: str, alloc: Allocation, custom_order_id: str) -> ExecutionReceipt:
        if alloc.tp2 is None:
            raise ValueError("Core allocation missing TP2")
        before = await self._working_orders(account)
        before_ids = {_order_id(o) for o in before if _order_id(o)}
        # Immediate native multi-tier protection. CrossTrade's ATM arrays are documented as
        # comma-separated strings of fill-relative offsets/quantities. They are provisional;
        # exact absolute Pine-derived prices are normalized immediately after acceptance.
        risk_pts = abs(alloc.entry - alloc.stop)
        tp1_off = abs(alloc.tp1 - alloc.entry)
        tp2_off = abs(float(alloc.tp2) - alloc.entry)
        targets = [tp1_off] + ([tp2_off] if alloc.runner_qty else [])
        stops = [risk_pts] * len(targets)
        qtys = [alloc.tp1_qty] + ([alloc.runner_qty] if alloc.runner_qty else [])
        fmt = lambda xs: ",".join(f"{float(x):g}" for x in xs)
        payload = {"instrument": "MNQ", "action": "buy" if alloc.side == "LONG" else "sell",
                   "qty": alloc.qty, "orderType": "market", "orderId": custom_order_id,
                   "atmTargets": fmt(targets), "atmStops": fmt(stops),
                   "atmQtys": ",".join(str(int(x)) for x in qtys),
                   "text": f"AutoProp CORE {alloc.event_id}"}
        try:
            result = await self.client.place(account, payload)
        except AmbiguousMutation:
            parent_id = await self._reconcile_custom_order(account, custom_order_id)
            if not parent_id:
                raise ProtectionFailure("ambiguous Core PLACE could not be reconciled; no resend")
            result = {"reconciled": True, "response": {"orderId": parent_id}}
        parent_id = _place_parent_id(result)
        response_ids = _place_child_ids(result)
        # CRITICAL RC2 call site: normalization is mandatory in the direct production path.
        owned = await self._normalize_core_multibracket(account, alloc, before_ids, response_ids)
        return ExecutionReceipt(result, sorted(owned), parent_id)

    async def _read_order(self, account: str, oid: str) -> dict[str, Any] | None:
        for o in await self._working_orders(account):
            if _order_id(o) == str(oid):
                return o
        return None

    async def _change_exact(self, account: str, oid: str, payload: dict[str, Any]) -> None:
        last: Exception | None = None
        for _ in range(self.change_retries):
            try:
                await self.client.change(account, oid, payload)
            except AmbiguousMutation as e:
                # Read-after-ambiguous-change first. If the desired child state is already
                # present, treat the mutation as accepted; otherwise bounded idempotent retry.
                last = e
                current = await self._read_order(account, oid)
                if current is not None and _desired_matches(current, payload):
                    return
            except CrossTradeError as e:
                last = e
            else:
                current = await self._read_order(account, oid)
                if current is not None and _desired_matches(current, payload):
                    return
                last = ProtectionFailure(f"child {oid} change acknowledged but exact readback differs")
            await asyncio.sleep(self.change_delay)
        raise ProtectionFailure(f"could not normalize child {oid}: {last}")

    async def _normalize_core_multibracket(self, account: str, alloc: Allocation,
                                           before_ids: set[str], response_ids: set[str]) -> set[str]:
        """Normalize only proven children to exact absolute prices, then read back.

        Ownership comes from broker-returned child IDs when available, otherwise from the
        before/after working-order ID delta. Price/quantity similarity alone never grants
        mutation authority. Any ambiguity fails toward less exposure by flattening.
        """
        for _ in range(self.bracket_confirm_retries):
            orders, owned_ids = await self._discover_owned_children(account, before_ids, response_ids)
            owned_rows = _owned(orders, owned_ids)
            targets, stops = classify_children(owned_rows)
            expected_targets = 1 + (1 if alloc.runner_qty else 0)
            if len(targets) == expected_targets and stops:
                # TP1 is always the nearer target. Identify tiers by side/absolute price
                # rather than distance from the planned entry so broker slippage cannot swap
                # target ownership: LONG TP1 is the lower target; SHORT TP1 is the higher.
                targets.sort(key=lambda o: (_price(o, "target") if _price(o, "target") is not None else float("inf")),
                             reverse=(alloc.side == "SHORT"))
                desired = [(alloc.tp1_qty, alloc.tp1)]
                if alloc.runner_qty:
                    desired.append((alloc.runner_qty, float(alloc.tp2)))
                try:
                    for o, (q, p) in zip(targets, desired):
                        oid = _order_id(o)
                        if not oid:
                            raise ProtectionFailure("owned target missing broker ID")
                        await self._change_exact(account, oid, {"qty": q, "orderType": "limit", "limitPrice": p})
                    # Preserve each native stop child's quantity; normalize only its absolute price.
                    # Exact aggregate coverage is required by verify_core_bracket.
                    for o in stops:
                        oid = _order_id(o)
                        if not oid:
                            raise ProtectionFailure("owned stop missing broker ID")
                        await self._change_exact(account, oid, {"qty": _qty(o), "orderType": "stop", "stopPrice": alloc.stop})
                except ProtectionFailure:
                    break
                for _j in range(self.bracket_confirm_retries):
                    check = await self._working_orders(account)
                    if verify_core_bracket(check, alloc, owned_ids):
                        return owned_ids
                    await asyncio.sleep(self.bracket_confirm_delay)
            await asyncio.sleep(self.bracket_confirm_delay)
        await self.client.flatten(account, "MNQ")
        raise ProtectionFailure("Core owned bracket normalization/readback failed; flattened")


    async def working_orders(self, account: str) -> list[dict[str, Any]]:
        return await self._working_orders(account)

    async def owned_roles(self, account: str, child_order_ids: list[str]) -> tuple[list[str], list[str]]:
        ids = {str(x) for x in child_order_ids}
        rows = _owned(await self._working_orders(account), ids)
        targets, stops = classify_children(rows)
        return ([_order_id(o) for o in targets if _order_id(o)],
                [_order_id(o) for o in stops if _order_id(o)])

    async def change_stop_orders(self, account: str, stop_order_ids: list[str],
                                 new_stop: float, expected_qty: int) -> list[str]:
        ids = {str(x) for x in stop_order_ids}
        rows = _owned(await self._working_orders(account), ids)
        _targets, stops = classify_children(rows)
        live = [o for o in stops if _order_id(o)]
        if not live or sum(_qty(o) for o in live) != int(expected_qty):
            raise ProtectionFailure('owned working stop coverage does not match live position')
        for o in live:
            await self._change_exact(account, _order_id(o),
                                     {'qty': _qty(o), 'orderType': 'stop', 'stopPrice': new_stop})
        check = _owned(await self._working_orders(account), {_order_id(o) for o in live})
        _t2, s2 = classify_children(check)
        exact = [o for o in s2 if tick_equal(_price(o, 'stop'), new_stop)]
        if sum(_qty(o) for o in exact) != int(expected_qty):
            raise ProtectionFailure('stop change exact readback/coverage failed')
        return [_order_id(o) for o in exact if _order_id(o)]
