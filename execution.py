from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable

from crosstrade import AmbiguousMutation, CrossTradeClient, CrossTradeError, RateLimitExceeded, normalize_tradovate_symbol
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
    """Flatten common CrossTrade envelopes into order-like dict rows.

    Tradovate GET /tv/orders returns identity envelopes whose nested ``data``
    contains the raw order list; per-account GET /orders returns the raw list
    directly. Keep this helper tolerant of both shapes.
    """
    d = payload.get("data", payload)
    if isinstance(d, list):
        out: list[dict[str, Any]] = []
        for x in d:
            if not isinstance(x, dict):
                continue
            nested = x.get("data")
            if isinstance(nested, list) and any(k in x for k in ("environment", "userId", "name")):
                out.extend(y for y in nested if isinstance(y, dict))
            else:
                out.append(x)
        return out
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


def _has_exact_geometry(o: dict[str, Any]) -> bool:
    """True only when a readback row can prove role, quantity, and exact price."""
    typ = str(o.get("orderType") or o.get("type") or "").lower()
    if _qty(o) <= 0:
        return False
    if "stop" in typ:
        return _price(o, "stop") is not None
    if "limit" in typ:
        return _price(o, "target") is not None
    return False


@dataclass
class ExecutionReceipt:
    result: dict[str, Any]
    child_order_ids: list[str]
    parent_order_id: str | None = None
    target_order_ids: list[str] = field(default_factory=list)
    stop_order_ids: list[str] = field(default_factory=list)


@dataclass
class Executor:
    client: CrossTradeClient
    execution_symbol: str = "MNQ1!"
    bracket_confirm_retries: int = 12
    bracket_confirm_delay: float = 0.25
    change_retries: int = 3
    change_delay: float = 0.25

    def _symbol(self) -> str:
        return normalize_tradovate_symbol(self.execution_symbol)

    async def _flatten_unverified_entry(self, account: str, reason: str) -> None:
        """Send exactly one fail-closed flatten; never hide an ambiguous flatten result."""
        try:
            await self.client.flatten(account, self._symbol())
        except CrossTradeError as exc:
            raise ProtectionFailure(f"{reason}; emergency flatten unconfirmed: {exc}") from exc
        raise ProtectionFailure(f"{reason}; flattened with one emergency request")

    async def _raw_working_orders(self, account: str) -> list[dict[str, Any]]:
        return _data_rows(await self.client.orders(account))

    async def _exact_snapshot_fallback(self, account: str, oid: str,
                                       raw: dict[str, Any]) -> dict[str, Any] | None:
        """Use alternate readback only when it independently exposes exact geometry.

        Raw Tradovate Order rows normally do not contain OrderVersion fields, so this
        fallback usually declines and the caller remains fail-closed. It is accepted only
        when the account/global snapshot itself proves order type, quantity, price and ID.
        """
        candidates = [dict(raw)] if raw else []
        fn = getattr(self.client, "all_orders", None)
        if fn is not None:
            try:
                candidates.extend(_data_rows(await fn()))
            except CrossTradeError:
                pass
        for candidate in candidates:
            if _order_id(candidate) != str(oid) or not _has_exact_geometry(candidate):
                continue
            # Preserve the freshest account-scoped live/terminal status when available.
            out = dict(candidate)
            for key in ("ordStatus", "orderState"):
                if raw.get(key) is not None:
                    out[key] = raw[key]
            return out
        return None

    async def _order_snapshot(self, account: str, oid: str, raw: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return an order row enriched with Tradovate OrderVersion fields.

        CrossTrade's Tradovate per-account working-orders endpoint intentionally returns
        raw Order entities only; quantity, order type and prices live on OrderVersion.
        Exact bracket verification therefore MUST use the order lifecycle endpoint.
        """
        raw = dict(raw or {})
        # Existing unit fakes / enriched callers may already provide everything needed.
        if _has_exact_geometry(raw):
            return raw
        fn = getattr(self.client, "order_lifecycle", None)
        if fn is None:
            if raw:
                return raw
            # Compatibility with existing test doubles / alternate clients that already
            # return enriched order rows but do not expose lifecycle. Production Tradovate
            # has lifecycle and therefore never relies on this fallback.
            try:
                for candidate in _data_rows(await self.client.orders(account)):
                    if _order_id(candidate) == str(oid):
                        return candidate
            except Exception:
                return None
            return None
        try:
            payload = await fn(account, str(oid))
        except CrossTradeError:
            fallback = await self._exact_snapshot_fallback(account, str(oid), raw)
            if fallback is not None:
                return fallback
            raise
        unavailable = {str(x) for x in (payload.get("unavailable") or [])} if isinstance(payload, dict) else set()
        if "version" in unavailable:
            return await self._exact_snapshot_fallback(account, str(oid), raw)
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            return None
        order = data.get("order") if isinstance(data.get("order"), dict) else {}
        version = data.get("version") if isinstance(data.get("version"), dict) else {}
        if not version:
            return None
        order_id = str(order.get("id") or version.get("orderId") or oid)
        out: dict[str, Any] = {
            **raw,
            "id": order_id,
            "orderId": order_id,
            "orderType": version.get("orderType"),
            "qty": version.get("orderQty"),
            "quantity": version.get("orderQty"),
            "orderQty": version.get("orderQty"),
            "limitPrice": version.get("price"),
            "price": version.get("price"),
            "stopPrice": version.get("stopPrice"),
            "orderState": order.get("ordStatus") or raw.get("ordStatus") or raw.get("orderState"),
            "ordStatus": order.get("ordStatus") or raw.get("ordStatus"),
        }
        commands = data.get("commands") if isinstance(data.get("commands"), list) else []
        for command in commands:
            if not isinstance(command, dict):
                continue
            cid = command.get("clOrdId") or command.get("clordId") or command.get("cl_ord_id")
            if cid:
                out["clOrdId"] = str(cid)
                break
        return out

    @staticmethod
    def _is_live_snapshot(row: dict[str, Any]) -> bool:
        status = str(row.get("ordStatus") or row.get("orderState") or "").strip().lower()
        return status not in {"filled", "canceled", "cancelled", "rejected", "expired", "completed"}

    async def _working_orders(self, account: str) -> list[dict[str, Any]]:
        raw_rows = await self._raw_working_orders(account)
        out: list[dict[str, Any]] = []
        for raw in raw_rows:
            oid = _order_id(raw)
            if not oid:
                continue
            snap = await self._order_snapshot(account, oid, raw)
            if snap is not None and self._is_live_snapshot(snap):
                out.append(snap)
        return out

    async def _all_orders(self) -> list[dict[str, Any]]:
        fn = getattr(self.client, "all_orders", None)
        if fn is None:
            return []
        return _data_rows(await fn())

    async def _reconcile_custom_order(self, account: str, custom_order_id: str) -> str | None:
        # Search the account's working orders first, then current-session history.
        # Tradovate keeps clOrdId on the New command, so lifecycle enrichment is required.
        candidates = await self._raw_working_orders(account)
        seen: set[str] = set()
        for raw in [*candidates, *(await self._all_orders())]:
            oid = _order_id(raw)
            if not oid or oid in seen:
                continue
            seen.add(oid)
            try:
                snap = await self._order_snapshot(account, oid, raw)
            except CrossTradeError:
                # All-orders spans identities; an order not owned by this account is expected.
                continue
            if snap is not None and _custom_id_match(snap, custom_order_id):
                return _order_id(snap)
        return None

    async def _discover_owned_children(self, account: str, before_ids: set[str],
                                       response_ids: set[str]) -> tuple[list[dict[str, Any]], set[str]]:
        raw_rows = await self._raw_working_orders(account)
        present = {_order_id(o) for o in raw_rows if _order_id(o)}
        if response_ids:
            owned = response_ids & present
        else:
            # Ambiguous/no-child-ID fallback: only orders newly appearing after this placement can
            # be considered. Never adopt pre-existing account orders by price/quantity similarity.
            owned = present - before_ids
        rows: list[dict[str, Any]] = []
        raw_map = {_order_id(o): o for o in raw_rows if _order_id(o)}
        for oid in sorted(owned):
            snap = await self._order_snapshot(account, oid, raw_map.get(oid))
            if snap is not None and self._is_live_snapshot(snap):
                rows.append(snap)
        # If response named children that are not yet visible, ``owned`` stays incomplete
        # and the caller retries rather than widening ownership.
        return rows, owned

    async def place_single(self, account: str, alloc: Allocation, custom_order_id: str) -> ExecutionReceipt:
        before = await self._raw_working_orders(account)
        before_ids = {_order_id(o) for o in before if _order_id(o)}
        payload = {"instrument": self._symbol(), "action": "buy" if alloc.side == "LONG" else "sell",
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
            try:
                orders, owned = await self._discover_owned_children(account, before_ids, response_ids)
            except CrossTradeError as exc:
                await self._flatten_unverified_entry(
                    account, f"single-target exact readback unavailable after accepted PLACE: {exc}"
                )
            if owned and verify_single_bracket(orders, alloc, owned):
                targets, stops = classify_children(_owned(orders, owned))
                target_ids = [_order_id(o) for o in targets if _order_id(o)]
                stop_ids = [_order_id(o) for o in stops if _order_id(o)]
                return ExecutionReceipt(result, sorted(owned), parent_id,
                                        sorted(target_ids), sorted(stop_ids))
            await asyncio.sleep(self.bracket_confirm_delay)
        await self._flatten_unverified_entry(
            account, "single-target protective bracket failed owned exact readback"
        )

    async def place_asw_limit(self, account: str, alloc: Allocation, custom_order_id: str,
                              *, expiry_time_ms: int, now_ms: int) -> ExecutionReceipt:
        if alloc.engine != "ASW":
            raise ValueError("place_asw_limit requires ASW allocation")
        remaining_ms = int(expiry_time_ms) - int(now_ms)
        if remaining_ms <= 0:
            raise ProtectionFailure("ASW limit candidate already expired")
        cancel_after = max(1, min(180, int((remaining_ms + 59999) // 60000)))
        payload = {
            "instrument": self._symbol(),
            "action": "buy" if alloc.side == "LONG" else "sell",
            "qty": alloc.qty,
            "orderType": "limit",
            "limitPrice": alloc.entry,
            "tif": "day",
            "orderId": custom_order_id,
            "takeProfit": alloc.tp1,
            "stopLoss": alloc.stop,
            "cancelAfter": cancel_after,
            "requireMarketPosition": "flat",
            "maxPositions": 1,
            "text": f"AutoProp ASW {alloc.event_id}"[:64],
        }
        try:
            result = await self.client.place(account, payload)
        except AmbiguousMutation:
            parent_id = await self._reconcile_custom_order(account, custom_order_id)
            if not parent_id:
                raise ProtectionFailure("ambiguous ASW LIMIT PLACE could not be reconciled; no resend")
            # We cannot prove the two OSO child identities from a lost PLACE response.
            # Cancel/flatten instead of managing an ownership-ambiguous bracket.
            try:
                await self.client.cancel_order(account, parent_id)
            except Exception:
                pass
            await self.client.flatten(account, self._symbol())
            raise ProtectionFailure("ambiguous ASW LIMIT accepted but OSO child ownership unavailable; canceled/flattened")
        parent_id = _place_parent_id(result)
        child_ids = _place_child_ids(result)
        if not parent_id or len(child_ids) < 2:
            if parent_id:
                try:
                    await self.client.cancel_order(account, parent_id)
                except Exception:
                    pass
            await self.client.flatten(account, self._symbol())
            raise ProtectionFailure("ASW LIMIT placement missing parent/OSO child identity; canceled/flattened")
        return ExecutionReceipt(result, sorted(child_ids), parent_id)

    async def place_core(self, account: str, alloc: Allocation, custom_order_id: str) -> ExecutionReceipt:
        if alloc.tp2 is None:
            raise ValueError("Core allocation missing TP2")
        before = await self._raw_working_orders(account)
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
        payload = {"instrument": self._symbol(), "action": "buy" if alloc.side == "LONG" else "sell",
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
        try:
            owned, target_ids, stop_ids = await self._normalize_core_multibracket(
                account, alloc, before_ids, response_ids
            )
        except CrossTradeError as exc:
            await self._flatten_unverified_entry(
                account, f"Core exact readback unavailable after accepted PLACE: {exc}"
            )
        return ExecutionReceipt(result, sorted(owned), parent_id,
                                sorted(target_ids), sorted(stop_ids))

    async def _read_order(self, account: str, oid: str) -> dict[str, Any] | None:
        try:
            return await self._order_snapshot(account, str(oid))
        except CrossTradeError:
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
            except RateLimitExceeded as e:
                # The transport never resends a rate-limited mutation. Reconcile first;
                # only the outer exact-change loop may issue another idempotent CHANGE.
                last = e
                current = await self._read_order(account, oid)
                if current is not None and _desired_matches(current, payload):
                    return
            except CrossTradeError as e:
                raise ProtectionFailure(f"definitive child {oid} change failure: {e}") from e
            else:
                current = await self._read_order(account, oid)
                if current is not None and _desired_matches(current, payload):
                    return
                last = ProtectionFailure(f"child {oid} change acknowledged but exact readback differs")
            await asyncio.sleep(self.change_delay)
        raise ProtectionFailure(f"could not normalize child {oid}: {last}")

    async def _normalize_core_multibracket(self, account: str, alloc: Allocation,
                                           before_ids: set[str], response_ids: set[str]) -> tuple[set[str], list[str], list[str]]:
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
                    check = []
                    for oid in sorted(owned_ids):
                        snap = await self._order_snapshot(account, oid)
                        if snap is not None and self._is_live_snapshot(snap):
                            check.append(snap)
                    if verify_core_bracket(check, alloc, owned_ids):
                        verified_targets, verified_stops = classify_children(_owned(check, owned_ids))
                        return (
                            owned_ids,
                            [_order_id(o) for o in verified_targets if _order_id(o)],
                            [_order_id(o) for o in verified_stops if _order_id(o)],
                        )
                    await asyncio.sleep(self.bracket_confirm_delay)
            await asyncio.sleep(self.bracket_confirm_delay)
        await self._flatten_unverified_entry(
            account, "Core owned bracket normalization/readback failed"
        )


    async def working_orders(self, account: str) -> list[dict[str, Any]]:
        return await self._working_orders(account)

    async def owned_roles(self, account: str, child_order_ids: list[str]) -> tuple[list[str], list[str]]:
        rows: list[dict[str, Any]] = []
        for oid in child_order_ids:
            snap = await self._order_snapshot(account, str(oid))
            if snap is not None and self._is_live_snapshot(snap):
                rows.append(snap)
        targets, stops = classify_children(rows)
        return ([_order_id(o) for o in targets if _order_id(o)],
                [_order_id(o) for o in stops if _order_id(o)])

    async def change_stop_orders(self, account: str, stop_order_ids: list[str],
                                 new_stop: float, expected_qty: int) -> list[str]:
        rows: list[dict[str, Any]] = []
        for oid in stop_order_ids:
            snap = await self._order_snapshot(account, str(oid))
            if snap is not None and self._is_live_snapshot(snap):
                rows.append(snap)
        _targets, stops = classify_children(rows)
        live = [o for o in stops if _order_id(o)]
        if not live or sum(_qty(o) for o in live) != int(expected_qty):
            raise ProtectionFailure('owned working stop coverage does not match live position')
        for o in live:
            await self._change_exact(account, _order_id(o),
                                     {'qty': _qty(o), 'orderType': 'stop', 'stopPrice': new_stop})
        check: list[dict[str, Any]] = []
        for o in live:
            snap = await self._order_snapshot(account, _order_id(o))
            if snap is not None and self._is_live_snapshot(snap):
                check.append(snap)
        _t2, s2 = classify_children(check)
        exact = [o for o in s2 if tick_equal(_price(o, 'stop'), new_stop)]
        if sum(_qty(o) for o in exact) != int(expected_qty):
            raise ProtectionFailure('stop change exact readback/coverage failed')
        return [_order_id(o) for o in exact if _order_id(o)]
