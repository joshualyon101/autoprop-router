from __future__ import annotations

from typing import Iterable, Mapping, Any


def prove_prior_org_stop(*, fills: Iterable[Mapping[str, Any]], stop_order_ids: set[str],
                         entry_order_ids: set[str] | None = None) -> bool:
    """Return True only when a durable fill proves the destination's prior ORG stop filled.

    Source-Pine outcome is never used as a substitute for destination broker outcome.
    """
    if not stop_order_ids:
        return False
    for f in fills:
        oid = str(f.get("orderId") or f.get("order_id") or "")
        if oid in stop_order_ids and int(float(f.get("qty", 0) or 0)) > 0:
            return True
    return False
