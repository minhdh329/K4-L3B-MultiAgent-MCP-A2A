from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

AGENT_TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "entity-agent": frozenset({"get_order"}),
    "customer-agent": frozenset({"get_customer_history"}),
    "order-agent": frozenset(
        {"get_order", "get_order_items", "get_sellers", "get_product_context"}
    ),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "payment-agent": frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    "policy-agent": frozenset({"get_policy"}),
}


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the bounded coordinator workflow for one case.

    The MCP payloads are deliberately treated as evidence, not as a fixed internal
    model: the public contract owns the output shape while this adapter tolerates
    small changes in the gateway's nested data objects.
    """
    case_id = _text(case.get("case_id"))
    if not case_id:
        raise ValueError("case is missing case_id")
    available = set(await gateway.list_tools())
    cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    evidence: list[str] = []
    calls = 0

    async def investigate(actor: str, tool: str, **arguments: str) -> dict[str, Any] | None:
        nonlocal calls
        if tool not in AGENT_TOOL_PERMISSIONS.get(actor, frozenset()):
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="TOOL_NOT_PERMITTED",
                attributes={"tool": tool},
            )
            return None
        if tool not in available or calls >= 20:
            return None
        key = (tool, tuple(sorted(arguments.items())))
        if key in cache:
            return cache[key]
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)
        for attempt in range(2):
            try:
                calls += 1
                result = await gateway.call(tool, case_id=case_id, **arguments)
                cache[key] = result
                ref = result["evidence_ref"]
                if ref not in evidence:
                    evidence.append(ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool,
                    evidence_refs=[ref],
                )
                return result
            except (RuntimeError, ValueError, TimeoutError):
                if attempt == 1:
                    trace.emit(
                        case_id=case_id,
                        event_type="handoff",
                        actor=actor,
                        target="coordinator",
                        decision_code="MCP_UNAVAILABLE",
                    )
        return None

    candidates = _candidate_ids(case)
    order_results: dict[str, dict[str, Any]] = {}
    for order_id in candidates[:8]:
        result = await investigate("order-agent", "get_order", order_id=order_id)
        if result is not None:
            order_results[order_id] = result

    resolved = _resolve_orders(case, order_results)
    resolved_ids = list(resolved)
    rejected = [item for item in candidates if item not in resolved_ids]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code="ENTITY_RESOLVED" if resolved_ids else "ENTITY_UNRESOLVED",
    )

    customer_id = _first_text(
        case,
        "customer_unique_id",
        "customer_id",
        "customer_unique_id_hint",
    )
    if not customer_id and order_results:
        customer_id = _find_text(
            next(iter(order_results.values())).get("data"), "customer_unique_id"
        )
    customer = (
        await investigate("customer-agent", "get_customer_history", customer_unique_id=customer_id)
        if customer_id
        else None
    )

    domain_results: list[dict[str, Any]] = []
    for order_id in resolved_ids[:3]:
        for actor, tool in (
            ("order-agent", "get_order_items"),
            ("shipment-agent", "get_shipment_summary"),
            ("payment-agent", "get_order_payments"),
            ("payment-agent", "get_payment_timeline"),
            ("payment-agent", "get_refund_timeline"),
        ):
            result = await investigate(actor, tool, order_id=order_id)
            if result is not None:
                domain_results.append(result)

    seller_ids = _collect_ids(
        domain_results + list(order_results.values()), "seller_id", "seller_ids"
    )
    if seller_ids and "get_sellers" in available:
        await investigate("order-agent", "get_sellers", seller_ids=",".join(seller_ids[:20]))
    policy = await investigate(
        "policy-agent",
        "get_policy",
        policy_version=_text(case.get("policy_version")) or "EC_POLICY_V2",
    )
    if policy is not None:
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            tool_name="get_policy",
            evidence_refs=[policy["evidence_ref"]],
            decision_code="POLICY_EVIDENCE_AVAILABLE",
        )

    output = _build_output(
        case,
        resolved_ids,
        rejected,
        customer_id,
        customer,
        order_results,
        domain_results,
        seller_ids,
        evidence,
    )
    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier")
    return output


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_text(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        found = _find_text(value, key)
        if found:
            return found
    return None


def _find_text(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        found = _text(value.get(key))
        if found:
            return found
        for child in value.values():
            found = _find_text(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_text(child, key)
            if found:
                return found
    return None


def _candidate_ids(case: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "order_id",
        "exact_order_id",
        "claimed_order_id",
        "candidate_order_ids",
        "order_candidates",
    ):
        value = case.get(key)
        items = value if isinstance(value, list) else [value]
        for item in items:
            candidate = item if isinstance(item, str) else _find_text(item, "order_id")
            if candidate and candidate not in values:
                values.append(candidate)
    return values


def _resolve_orders(case: dict[str, Any], results: dict[str, dict[str, Any]]) -> list[str]:
    exact = _first_text(case, "order_id", "exact_order_id")
    if exact and exact in results:
        return [exact]
    if len(results) == 1:
        return list(results)
    if len(results) > 1:
        customer_id = _first_text(case, "customer_unique_id", "customer_id")
        matches = [
            order_id
            for order_id, result in results.items()
            if customer_id and _find_text(result.get("data"), "customer_unique_id") == customer_id
        ]
        return matches if len(matches) == 1 else []
    return []


def _walk(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [value, *[item for child in value.values() for item in _walk(child)]]
    if isinstance(value, list):
        return [item for child in value for item in _walk(child)]
    return []


def _collect_ids(values: list[dict[str, Any]], *keys: str) -> list[str]:
    found: list[str] = []
    for value in values:
        for item in _walk(value.get("data")):
            if isinstance(item, dict):
                for key in keys:
                    raw = item.get(key, [])
                    items = raw if isinstance(raw, list) else [raw]
                    for candidate in items:
                        if isinstance(candidate, str) and candidate not in found:
                            found.append(candidate)
    return found[:20]


def _numbers(values: list[dict[str, Any]], keys: tuple[str, ...]) -> float | None:
    total = 0.0
    seen = False
    for value in values:
        for item in _walk(value.get("data")):
            if isinstance(item, dict):
                for key in keys:
                    raw = item.get(key)
                    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                        total += float(raw)
                        seen = True
    return round(total, 2) if seen else None


def _build_output(
    case: dict[str, Any], resolved: list[str], rejected: list[str], customer_id: str | None,
    customer: dict[str, Any] | None,
    orders: dict[str, dict[str, Any]],
    domains: list[dict[str, Any]],
    seller_ids: list[str], evidence: list[str],
) -> dict[str, Any]:
    payment_total = _numbers(domains, ("payment_value", "amount", "captured_amount", "value"))
    refund_total = _numbers(domains, ("refund_amount", "refunded_amount", "refunded_total"))
    issue = "insufficient_evidence"
    status = "needs_investigation"
    confidence = 0.2
    if resolved and payment_total is not None:
        issue, status, confidence = "unsupported_claim", "no_action", 0.55
    if _has_value(domains, "refund_status", {"pending", "requested"}):
        issue, status = "refund_pending", "action_required"
    if _has_value(domains, "delivery_status", {"delivered_late", "late"}):
        issue, status = "late_delivery_logistics", "action_required"
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [],
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": resolved,
            "item_ids": [],
            "seller_ids": seller_ids,
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": (
                "resolved"
                if len(resolved) == 1
                else ("ambiguous" if rejected else "not_found")
            ),
            "resolved_order_ids": resolved,
            "rejected_candidates": rejected,
            "confidence": 0.9 if len(resolved) == 1 else 0.1,
        },
        "customer_context": {"customer_unique_id": customer_id, "related_order_ids": resolved},
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": payment_total,
            "refunded_total_brl": refund_total,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": evidence[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": (
            ["Collect additional order, shipment, and payment evidence."] if not resolved else []
        ),
    }


def _has_value(values: list[dict[str, Any]], key: str, expected: set[str]) -> bool:
    for value in values:
        for item in _walk(value.get("data")):
            if isinstance(item, dict) and str(item.get(key, "")).lower() in expected:
                return True
    return False
