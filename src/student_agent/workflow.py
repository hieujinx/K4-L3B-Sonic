from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

MAX_CANDIDATES = 5
MAX_RESOLVED_ORDERS = 2
MAX_ATTEMPTS = 3

TOOL_OWNER = {
    "get_order": "entity-agent",
    "get_customer_history": "entity-agent",
    "get_order_items": "order-item-agent",
    "get_product_context": "order-item-agent",
    "get_sellers": "order-item-agent",
    "get_order_payments": "payment-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "payment-agent",
    "get_shipment_summary": "shipment-agent",
    "get_policy": "policy-agent",
}


@dataclass
class Investigation:
    case_id: str
    available_tools: set[str]
    evidence: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = field(
        default_factory=dict
    )
    failures: list[str] = field(default_factory=list)

    @property
    def evidence_refs(self) -> list[str]:
        return _unique(
            value["evidence_ref"]
            for value in self.evidence.values()
            if isinstance(value.get("evidence_ref"), str)
        )

    def by_tool(self, tool_name: str) -> list[dict[str, Any]]:
        return [value for (name, _), value in self.evidence.items() if name == tool_name]

    def refs_for(self, tool_names: set[str]) -> list[str]:
        return _unique(
            evidence["evidence_ref"]
            for (tool_name, _), evidence in self.evidence.items()
            if tool_name in tool_names and isinstance(evidence.get("evidence_ref"), str)
        )


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _walk(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = (*path, str(key).lower())
            yield child_path, child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, path)


def _strings(value: Any, keys: set[str]) -> list[str]:
    found: list[str] = []
    for path, child in _walk(value):
        if not path or path[-1] not in keys:
            continue
        candidates = child if isinstance(child, list) else [child]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                found.append(candidate.strip())
    return _unique(found)


def _first_string(value: Any, keys: set[str]) -> str | None:
    values = _strings(value, keys)
    return values[0] if values else None


def _flag(value: Any, key: str) -> bool:
    return any(path and path[-1] == key and child is True for path, child in _walk(value))


def _case_text(case: Mapping[str, Any]) -> str:
    values: list[str] = []
    for _, value in _walk(case):
        if isinstance(value, str):
            values.append(value.lower())
    return " ".join(values)


def _candidate_order_ids(case: Mapping[str, Any]) -> tuple[list[str], set[str]]:
    exact = _strings(case, {"order_id", "order_ids", "resolved_order_id"})
    candidates = _strings(
        case,
        {
            "candidate_order_id",
            "candidate_order_ids",
            "order_candidates",
            "candidate_ids",
        },
    )
    nested_candidates: list[str] = []
    for path, value in _walk(case):
        if (
            path
            and path[-1] == "order_id"
            and isinstance(value, str)
            and any("candidate" in component for component in path[:-1])
        ):
            nested_candidates.append(value)
    candidate_set = set(candidates) | set(nested_candidates)
    authoritative = set(exact) - candidate_set
    authoritative_ordered = [order_id for order_id in exact if order_id in authoritative]
    ordered = _unique([*authoritative_ordered, *candidates, *nested_candidates])
    return ordered[:MAX_CANDIDATES], authoritative


def _has_data(evidence: dict[str, Any]) -> bool:
    data = evidence.get("data")
    if data is None or data == {} or data == []:
        return False
    if isinstance(data, Mapping):
        status = str(data.get("status", "")).lower()
        if status in {"not_found", "missing", "unknown"} or data.get("found") is False:
            return False
    return True


def _score_candidate(case: Mapping[str, Any], order_id: str, evidence: dict[str, Any]) -> int:
    if not _has_data(evidence):
        return -100
    score = 1
    data = evidence.get("data")
    if order_id in _strings(data, {"order_id", "order_ids"}):
        score += 4
    hint_keys = {
        "customer_id",
        "customer_unique_id",
        "seller_id",
        "product_id",
        "payment_reference",
        "shipment_id",
    }
    score += 2 * len(set(_strings(case, hint_keys)) & set(_strings(data, hint_keys)))
    return score


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _find_times(value: Any, keys: set[str]) -> list[datetime]:
    result: list[datetime] = []
    for path, child in _walk(value):
        if path and path[-1] in keys:
            parsed = _parse_time(child)
            if parsed is not None:
                result.append(parsed)
    return result


def _mapping_records(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _mapping_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _mapping_records(child)


def _numbers(value: Any, keys: set[str]) -> list[float]:
    result: list[float] = []
    for path, child in _walk(value):
        if not path or path[-1] not in keys or isinstance(child, bool):
            continue
        try:
            number = float(child)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            result.append(number)
    return result


def _money(value: float | None) -> float | None:
    return None if value is None else round(value + 1e-10, 2)


def _monetary_total(
    documents: list[Any], aggregate_keys: set[str], line_keys: set[str]
) -> float | None:
    aggregate = [number for document in documents for number in _numbers(document, aggregate_keys)]
    if aggregate:
        return _money(max(aggregate))
    lines = [number for document in documents for number in _numbers(document, line_keys)]
    return _money(sum(lines)) if lines else None


async def _call(
    state: Investigation,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    actor: str,
    tool_name: str,
    **arguments: str,
) -> dict[str, Any] | None:
    if TOOL_OWNER.get(tool_name) != actor:
        raise PermissionError(f"{actor} is not allowed to call {tool_name}")
    if tool_name not in state.available_tools:
        return None
    key = (tool_name, tuple(sorted(arguments.items())))
    if key in state.evidence:
        return state.evidence[key]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            evidence = await gateway.call(tool_name, case_id=state.case_id, **arguments)
        except (RuntimeError, ValueError) as exc:
            state.failures.append(f"{tool_name}:{type(exc).__name__}")
            trace.emit(
                case_id=state.case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="MCP_CALL_FAILED",
                attributes={"tool": tool_name, "attempts": attempt},
            )
            return None
        except (OSError, TimeoutError) as exc:
            if attempt == MAX_ATTEMPTS:
                state.failures.append(f"{tool_name}:{type(exc).__name__}")
                trace.emit(
                    case_id=state.case_id,
                    event_type="handoff",
                    actor=actor,
                    target="coordinator",
                    decision_code="MCP_CALL_FAILED",
                    attributes={"tool": tool_name, "attempts": attempt},
                )
                return None
            await asyncio.sleep(0.75 * attempt)
            continue
        state.evidence[key] = evidence
        trace.emit(
            case_id=state.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence["evidence_ref"]],
            attributes={"attempt": attempt},
        )
        return evidence
    return None


async def _resolve_entities(
    case: Mapping[str, Any], state: Investigation, gateway: EvidenceGateway, trace: TraceWriter
) -> tuple[str, list[str], list[str], float]:
    candidates, authoritative = _candidate_order_ids(case)
    if not candidates:
        return "not_found", [], [], 0.0
    scored: list[tuple[int, str]] = []
    for order_id in candidates:
        evidence = await _call(
            state, gateway, trace, "entity-agent", "get_order", order_id=order_id
        )
        if evidence is not None:
            score = _score_candidate(case, order_id, evidence)
            scored.append((score, order_id))
            if score >= 5 and not authoritative:
                rejected = [candidate for candidate in candidates if candidate != order_id]
                return "resolved", [order_id], rejected, min(0.95, 0.65 + 0.05 * score)
    if authoritative:
        matching = [order_id for order_id in candidates if order_id in authoritative]
        resolved = matching[:MAX_RESOLVED_ORDERS]
        rejected = [order_id for order_id in candidates if order_id not in resolved]
        return "resolved", resolved, rejected, 0.98
    viable = sorted((item for item in scored if item[0] >= 0), reverse=True)
    if not viable:
        return "not_found", [], candidates, 0.15
    if len(viable) > 1 and viable[0][0] == viable[1][0]:
        return "ambiguous", [], [item[1] for item in viable], 0.45
    resolved = [viable[0][1]]
    rejected = [order_id for order_id in candidates if order_id not in resolved]
    confidence = min(0.95, 0.65 + 0.05 * max(0, viable[0][0]))
    return "resolved", resolved, rejected, confidence


def _domain_data(state: Investigation, tool_names: set[str]) -> list[Any]:
    return [
        evidence.get("data")
        for (tool_name, _), evidence in state.evidence.items()
        if tool_name in tool_names
    ]


def _shipment_analysis(state: Investigation, seller_ids: list[str]) -> dict[str, Any]:
    documents = _domain_data(state, {"get_order", "get_order_items", "get_shipment_summary"})
    text = " ".join(str(value).lower() for value in documents)
    verdict = "insufficient_evidence"
    if any(token in text for token in ("lost", "extraviad")):
        verdict = "lost"
    elif any(token in text for token in ("returned", "devolvid")):
        verdict = "returned"
    elif any(token in text for token in ("seller_delay", "seller late", "late_seller")) or (
        "delivered_late" in text and "'actor': 'seller'" in text
    ):
        verdict = "seller_delay"
    elif any(token in text for token in ("logistics_delay", "carrier_delay", "late delivery")) or (
        "delivered_late" in text and "logistics_provider" in text
    ):
        verdict = "logistics_delay"
    delivered = [
        time
        for document in documents
        for time in _find_times(
            document, {"order_delivered_customer_date", "delivered_at", "delivered_date"}
        )
    ]
    estimated = [
        time
        for document in documents
        for time in _find_times(
            document, {"order_estimated_delivery_date", "estimated_delivery", "estimated_at"}
        )
    ]
    carrier = [
        time
        for document in documents
        for time in _find_times(
            document, {"order_delivered_carrier_date", "carrier_received_at", "shipped_at"}
        )
    ]
    limits = [
        time
        for document in documents
        for time in _find_times(document, {"shipping_limit_date", "seller_deadline"})
    ]
    if verdict == "insufficient_evidence" and delivered and estimated:
        if max(delivered) <= max(estimated):
            verdict = "on_time"
        elif carrier and limits and max(carrier) > max(limits):
            verdict = "seller_delay"
        else:
            verdict = "logistics_delay"
    timeline_complete = bool(delivered and estimated and (carrier or verdict == "on_time"))
    return {
        "verdict": verdict,
        "late_seller_ids": seller_ids if verdict == "seller_delay" else [],
        "timeline_complete": timeline_complete,
    }


def _payment_analysis(state: Investigation) -> dict[str, Any]:
    canonical_payment_docs = _domain_data(state, {"get_order_payments"})
    payment_docs = canonical_payment_docs or _domain_data(state, {"get_payment_timeline"})
    all_payment_docs = _domain_data(state, {"get_order_payments", "get_payment_timeline"})
    refund_docs = _domain_data(state, {"get_refund_timeline"})
    purchase_times = [
        time
        for document in _domain_data(state, {"get_order"})
        for time in _find_times(document, {"order_purchase_timestamp", "purchased_at"})
    ]
    purchase_at = min(purchase_times) if purchase_times else None
    captured_events: list[float] = []
    for document in _domain_data(state, {"get_payment_timeline"}):
        for record in _mapping_records(document):
            if str(record.get("event_type", "")).lower() not in {"captured", "capture"}:
                continue
            event_at = _parse_time(record.get("event_at"))
            if purchase_at is not None and event_at is not None and event_at < purchase_at:
                continue
            captured_events.extend(_numbers(record, {"amount_brl", "captured_amount"}))
    captured = (
        _money(sum(captured_events))
        if captured_events
        else _monetary_total(
            payment_docs,
            {"captured_total_brl", "paid_total_brl", "payment_total", "captured_amount"},
            {"payment_value", "amount_brl"},
        )
    )
    refunded = _monetary_total(
        refund_docs,
        {"refunded_total_brl", "refund_total", "refunded_amount"},
        {"refund_amount", "amount_brl"},
    )
    if refund_docs and refunded is None:
        refunded = 0.0
    text = " ".join(str(value).lower() for value in [*all_payment_docs, *refund_docs])
    verdict = "reconciled" if payment_docs else "insufficient_evidence"
    if any(token in text for token in ("duplicate_capture", "duplicate charge", "duplicat")):
        verdict = "duplicate_capture"
    elif any(token in text for token in ("refund_failed", "refund failed")):
        verdict = "refund_failed"
    elif any(token in text for token in ("refund_pending", "refund pending", "processing")):
        verdict = "refund_pending"
    elif refund_docs and refunded is not None and captured is not None and refunded >= captured:
        verdict = "refunded"
    elif any(token in text for token in ("capture_mismatch", "payment_mismatch", "mismatch")):
        verdict = "capture_mismatch"
    refundable = None
    if captured is not None and refunded is not None:
        refundable = _money(max(0.0, captured - refunded))
    return {
        "verdict": verdict,
        "captured_total_brl": captured,
        "refunded_total_brl": refunded,
        "refundable_total_brl": refundable,
    }


def _captured_event_count(state: Investigation) -> int:
    purchase_times = [
        time
        for document in _domain_data(state, {"get_order"})
        for time in _find_times(document, {"order_purchase_timestamp", "purchased_at"})
    ]
    purchase_at = min(purchase_times) if purchase_times else None
    count = 0
    for document in _domain_data(state, {"get_payment_timeline"}):
        for record in _mapping_records(document):
            if str(record.get("event_type", "")).lower() not in {"captured", "capture"}:
                continue
            event_at = _parse_time(record.get("event_at"))
            if purchase_at is not None and event_at is not None and event_at < purchase_at:
                continue
            count += 1
    return count


def _conflicts(state: Investigation) -> list[dict[str, Any]]:
    watched = {
        "order_status",
        "payment_status",
        "refund_status",
        "shipment_status",
        "customer_unique_id",
    }
    observed: dict[str, dict[str, str]] = {}
    for (tool_name, arguments), evidence in state.evidence.items():
        suffix = next((value for key, value in arguments if key == "order_id"), "")
        source = f"{tool_name}:{suffix}"[:80]
        for path, value in _walk(evidence.get("data")):
            if path and path[-1] in watched and isinstance(value, (str, int, float, bool)):
                observed.setdefault(path[-1], {})[source] = str(value)
    result: list[dict[str, Any]] = []
    precedence = [
        "get_refund_timeline",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_order",
    ]
    for field_name, sources in observed.items():
        if len(set(sources.values())) < 2 or len(sources) < 2:
            continue
        selected = next(
            (
                source
                for prefix in precedence
                for source in sources
                if source.startswith(prefix)
            ),
            None,
        )
        result.append(
            {
                "field": field_name,
                "sources": list(sources)[:5],
                "selected_source": selected,
                "resolution_code": "DOMAIN_EVENT_SOURCE_PRECEDENCE",
            }
        )
    return result[:5]


def _classify(
    order_statuses: list[str],
    shipment: dict[str, Any],
    payment: dict[str, Any],
    split_payment: bool,
) -> str:
    statuses = {status.lower() for status in order_statuses}
    outstanding = payment["refundable_total_brl"]
    paid = isinstance(outstanding, (int, float)) and outstanding > 0
    if any("cancel" in status for status in statuses) and paid:
        return "canceled_order_paid"
    if any("unavailable" in status for status in statuses) and paid:
        return "unavailable_order_paid"
    payment_map = {
        "duplicate_capture": "duplicate_charge",
        "capture_mismatch": "payment_mismatch",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    if payment["verdict"] in payment_map:
        return payment_map[payment["verdict"]]
    if shipment["verdict"] == "seller_delay":
        return "late_delivery_seller"
    if shipment["verdict"] in {"logistics_delay", "lost", "returned"}:
        return "late_delivery_logistics"
    if split_payment and payment["verdict"] in {"reconciled", "refunded"}:
        return "valid_split_payment"
    if payment["verdict"] == "reconciled":
        return "unsupported_claim"
    return "insufficient_evidence"


def _payable_total(item_data: list[Any], purchase_at: datetime | None) -> float | None:
    aggregate = _monetary_total(
        item_data,
        {"order_total_brl", "payable_total_brl", "items_total_brl"},
        set(),
    )
    if aggregate is not None:
        return aggregate
    valid_records: list[Mapping[str, Any]] = []
    for document in item_data:
        for record in _mapping_records(document):
            if "price" not in record and "freight_value" not in record:
                continue
            limit = _parse_time(record.get("shipping_limit_date"))
            if purchase_at is not None and limit is not None and limit < purchase_at:
                continue
            valid_records.append(record)
    prices = [number for record in valid_records for number in _numbers(record, {"price"})]
    freight = [number for record in valid_records for number in _numbers(record, {"freight_value"})]
    return _money(sum(prices) + sum(freight)) if prices or freight else None


def _calibrate_confidence(
    *,
    state: Investigation,
    entity_status: str,
    entity_confidence: float,
    issue: str,
    shipment: dict[str, Any],
    payment: dict[str, Any],
    policy_expected: bool,
    conflicts: list[dict[str, Any]],
) -> float:
    score = 0.1
    if entity_status == "resolved":
        score += 0.25
    elif entity_status == "ambiguous":
        score += 0.08
    if state.by_tool("get_order"):
        score += 0.15
    if issue.startswith("late_delivery"):
        score += 0.2 if state.by_tool("get_shipment_summary") else 0.0
        score += 0.1 if shipment["timeline_complete"] else 0.0
    elif issue in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "valid_split_payment",
    }:
        score += 0.2 if state.by_tool("get_order_payments") else 0.0
        if payment["captured_total_brl"] is not None:
            score += 0.1
    elif issue == "unsupported_claim":
        score += 0.15 if state.by_tool("get_order_payments") else 0.0
    if not policy_expected or state.by_tool("get_policy"):
        score += 0.1
    score -= min(0.3, 0.1 * len(conflicts))
    score -= min(0.3, 0.1 * len(state.failures))
    score = min(score, entity_confidence, 0.95)
    if entity_status == "ambiguous":
        score = min(score, 0.5)
    if conflicts:
        score = min(score, 0.75)
    return round(max(0.05, score), 2)


def _actions(issue: str, status: str, refund: float) -> list[str]:
    actions: list[str] = []
    if refund > 0:
        actions.append("ISSUE_REFUND")
    if issue in {"late_delivery_seller", "canceled_order_paid", "unavailable_order_paid"}:
        actions.append("NOTIFY_SELLER")
    if issue == "late_delivery_logistics":
        actions.append("ESCALATE_LOGISTICS")
    if issue in {"duplicate_charge", "payment_mismatch", "refund_failed"}:
        actions.append("ESCALATE_PAYMENT_PROVIDER")
    if status == "needs_investigation":
        actions.append("REQUEST_ADDITIONAL_EVIDENCE")
    return actions[:8]


def _policy_rule(state: Investigation, issue: str) -> Any:
    policies = _domain_data(state, {"get_policy"})
    for policy in policies:
        for path, value in _walk(policy):
            if path and path[-1] == issue and isinstance(value, Mapping):
                return value
    return policies[0] if policies else None


def _apply_financial_policy(state: Investigation, issue: str, amount: float) -> float:
    rule = _policy_rule(state, issue)
    if rule is None:
        return _money(amount) or 0.0
    eligibility = next(
        (
            value
            for path, value in _walk(rule)
            if path
            and path[-1] in {"refund_eligible", "eligible_for_refund"}
            and isinstance(value, bool)
        ),
        None,
    )
    if eligibility is False:
        return 0.0
    percentages = _numbers(rule, {"refund_percentage", "refund_percent"})
    if percentages:
        percentage = percentages[0] / 100 if percentages[0] > 1 else percentages[0]
        amount *= min(1.0, percentage)
    caps = _numbers(rule, {"max_refund_brl", "refund_cap_brl"})
    if caps:
        amount = min(amount, caps[0])
    return _money(max(0.0, amount)) or 0.0


def _claims(
    case: Mapping[str, Any],
    evidence_refs: list[str],
    issue: str,
    confidence: float,
    refund_amount: float,
) -> list[dict[str, Any]]:
    raw_claims = next(
        (
            value
            for path, value in _walk(case)
            if path and path[-1] == "claims" and isinstance(value, list)
        ),
        [],
    )
    result: list[dict[str, Any]] = []
    for claim in raw_claims[:5]:
        if not isinstance(claim, Mapping) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = str(claim.get("topic", ""))
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == issue:
            verdict = "supported"
        elif topic == "requested_full_refund":
            verdict = "supported" if refund_amount > 0 else "unsupported"
        else:
            verdict = "unsupported"
        result.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence_refs,
            }
        )
    return result


def _verify_invariants(output: dict[str, Any]) -> None:
    affected = output["affected_entities"]["order_ids"]
    resolution = output["entity_resolution"]
    if affected != resolution["resolved_order_ids"]:
        raise ValueError("resolved order scope is inconsistent")
    if set(affected) & set(resolution["rejected_candidates"]):
        raise ValueError("a rejected candidate appears in affected entities")
    financial = output["financial_resolution"]
    line_total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
    if line_total != round(financial["recommended_refund_brl"], 2):
        raise ValueError("refund lines do not equal the recommended refund")
    if output["assessment"]["case_status"] == "no_action" and (
        financial["recommended_refund_brl"] != 0 or output["resolution_actions"]
    ):
        raise ValueError("no_action output contains a financial or operational action")
    issue = output["assessment"]["primary_issue"]
    parties = {
        party["party_type"] for party in output["root_cause_analysis"]["responsible_parties"]
    }
    expected_parties = {
        "late_delivery_seller": {"seller"},
        "late_delivery_logistics": {"logistics_provider"},
        "payment_mismatch": {"payment_provider"},
        "duplicate_charge": {"payment_provider"},
        "refund_pending": {"payment_provider"},
        "refund_failed": {"payment_provider"},
        "canceled_order_paid": {"platform"},
        "unavailable_order_paid": {"seller", "platform"},
        "valid_split_payment": {"customer"},
        "unsupported_claim": {"customer"},
        "insufficient_evidence": {"unknown"},
    }
    if not parties <= expected_parties[issue]:
        raise ValueError(f"responsible party is inconsistent with {issue}")
    required_actions = {
        "late_delivery_seller": "NOTIFY_SELLER",
        "late_delivery_logistics": "ESCALATE_LOGISTICS",
        "payment_mismatch": "ESCALATE_PAYMENT_PROVIDER",
        "duplicate_charge": "ESCALATE_PAYMENT_PROVIDER",
        "refund_failed": "ESCALATE_PAYMENT_PROVIDER",
    }
    required_action = required_actions.get(issue)
    if required_action and required_action not in output["resolution_actions"]:
        raise ValueError(f"resolution action is inconsistent with {issue}")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run a bounded L3B coordinator/specialist investigation for one case."""
    case_id = case.get("case_id")
    if not isinstance(case_id, str):
        raise ValueError("case.case_id must be a string")
    if hasattr(gateway, "describe_tools"):
        available_tools = set(await gateway.describe_tools())
    else:
        available_tools = set(await gateway.list_tools())
    state = Investigation(case_id, available_tools)
    text = _case_text(case)

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        decision_code="RESOLVE_ORDER_SCOPE",
    )
    entity_status, resolved, rejected, entity_confidence = await _resolve_entities(
        case, state, gateway, trace
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code=f"ENTITY_{entity_status.upper()}",
        evidence_refs=state.evidence_refs,
        attributes={"resolved_count": len(resolved), "rejected_count": len(rejected)},
    )

    for specialist in ["order-item-agent", "payment-agent", "shipment-agent"]:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=specialist,
            decision_code="INVESTIGATE_DOMAIN",
            attributes={"order_count": len(resolved)},
        )

    shipment_cue = any(
        token in text for token in ("ship", "deliver", "late", "carrier", "logistic", "seller")
    )
    refund_cue = any(
        token in text for token in ("refund", "cancel", "unavailable", "estorno", "reembolso")
    )
    payment_cue = refund_cue or any(
        token in text for token in ("pay", "charge", "capture", "duplicate", "payment")
    )
    product_cue = _flag(case, "include_product_context") or any(
        token in text for token in ("product", "item", "produto", "wrong item")
    )

    for order_id in resolved:
        calls = [
            _call(
                state,
                gateway,
                trace,
                "order-item-agent",
                "get_order_items",
                order_id=order_id,
            ),
            _call(
                state,
                gateway,
                trace,
                "payment-agent",
                "get_order_payments",
                order_id=order_id,
            ),
        ]
        if payment_cue:
            calls.append(
                _call(
                    state,
                    gateway,
                    trace,
                    "payment-agent",
                    "get_payment_timeline",
                    order_id=order_id,
                )
            )
        if refund_cue:
            calls.append(
                _call(
                    state,
                    gateway,
                    trace,
                    "payment-agent",
                    "get_refund_timeline",
                    order_id=order_id,
                )
            )
        if shipment_cue:
            calls.append(
                _call(
                    state,
                    gateway,
                    trace,
                    "shipment-agent",
                    "get_shipment_summary",
                    order_id=order_id,
                )
            )
            calls.append(
                _call(
                    state,
                    gateway,
                    trace,
                    "order-item-agent",
                    "get_sellers",
                    order_id=order_id,
                )
            )
        if product_cue:
            calls.append(
                _call(
                    state,
                    gateway,
                    trace,
                    "order-item-agent",
                    "get_product_context",
                    order_id=order_id,
                )
            )
        await asyncio.gather(*calls)

    specialist_tools = {
        "order-item-agent": {"get_order_items", "get_product_context", "get_sellers"},
        "payment-agent": {
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
        },
        "shipment-agent": {"get_shipment_summary"},
    }
    for specialist, tool_names in specialist_tools.items():
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=specialist,
            target="coordinator",
            decision_code="DOMAIN_EVIDENCE_READY",
            evidence_refs=state.refs_for(tool_names)[:20],
            attributes={"evidence_count": len(state.refs_for(tool_names))},
        )

    customer_unique_id = _first_string(
        [case, *_domain_data(state, {"get_order"})],
        {"customer_unique_id", "customer_unique_id_hint"},
    )
    if customer_unique_id and (
        _flag(case, "include_customer_history") or state.by_tool("get_order")
    ):
        await _call(
            state,
            gateway,
            trace,
            "entity-agent",
            "get_customer_history",
            customer_unique_id=customer_unique_id,
        )
    policy_version = _first_string(case, {"policy_version"})
    if policy_version:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="policy-agent",
            decision_code="EVALUATE_POLICY",
        )
        await _call(
            state,
            gateway,
            trace,
            "policy-agent",
            "get_policy",
            policy_version=policy_version,
        )

    candidate_ids, _ = _candidate_order_ids(case)
    if candidate_ids and not state.by_tool("get_order"):
        raise RuntimeError(
            f"MCP Gateway returned no authoritative order evidence for {case_id}"
        )

    order_data = _domain_data(state, {"get_order"})
    item_data = _domain_data(state, {"get_order_items", "get_product_context", "get_sellers"})
    order_ids = _unique(resolved)
    item_ids = _strings(item_data, {"order_item_id", "item_id"})[:20]
    seller_ids = _strings(item_data, {"seller_id", "seller_ids"})[:20]
    payment_references = _strings(
        _domain_data(state, {"get_order_payments", "get_payment_timeline"}),
        {"payment_reference", "payment_id", "transaction_id"},
    )[:20]
    shipment_ids = _strings(
        _domain_data(state, {"get_shipment_summary"}),
        {"shipment_id", "tracking_id", "tracking_code"},
    )[:20]
    related_order_ids = _strings(
        _domain_data(state, {"get_customer_history"}), {"order_id", "order_ids"}
    )[:20]
    shipment = _shipment_analysis(state, seller_ids)
    payment = _payment_analysis(state)
    purchase_times = [
        time
        for document in order_data
        for time in _find_times(document, {"order_purchase_timestamp", "purchased_at"})
    ]
    payable_total = _payable_total(
        _domain_data(state, {"get_order_items"}),
        min(purchase_times) if purchase_times else None,
    )
    order_statuses = _strings(order_data, {"order_status", "status"})
    issue = _classify(order_statuses, shipment, payment, _captured_event_count(state) > 1)
    conflicts = _conflicts(state)
    if entity_status != "resolved" or issue == "insufficient_evidence" or state.failures:
        case_status = "needs_investigation"
    elif issue in {"unsupported_claim", "valid_split_payment"}:
        case_status = "no_action"
    else:
        case_status = "action_required"
    confidence = _calibrate_confidence(
        state=state,
        entity_status=entity_status,
        entity_confidence=entity_confidence,
        issue=issue,
        shipment=shipment,
        payment=payment,
        policy_expected=policy_version is not None,
        conflicts=conflicts,
    )

    responsibility: list[dict[str, str | None]] = []
    cause = "INSUFFICIENT_EVIDENCE"
    if issue == "late_delivery_seller":
        cause = "SELLER_FULFILLMENT_DELAY"
        responsibility = [
            {"party_type": "seller", "party_id": seller_id} for seller_id in seller_ids[:5]
        ]
    elif issue == "late_delivery_logistics":
        cause = "LOGISTICS_DELIVERY_DELAY"
        responsibility = [{"party_type": "logistics_provider", "party_id": None}]
    elif issue in {"duplicate_charge", "payment_mismatch", "refund_failed", "refund_pending"}:
        cause = issue.upper()
        responsibility = [{"party_type": "payment_provider", "party_id": None}]
    elif issue in {"canceled_order_paid", "unavailable_order_paid"}:
        cause = issue.upper()
        if issue == "unavailable_order_paid" and seller_ids:
            responsibility = [
                {"party_type": "seller", "party_id": seller_id} for seller_id in seller_ids[:5]
            ]
        else:
            responsibility = [{"party_type": "platform", "party_id": None}]
    elif issue in {"unsupported_claim", "valid_split_payment"}:
        cause = "CLAIM_NOT_SUPPORTED_BY_RECORDS"
        responsibility = [{"party_type": "customer", "party_id": None}]
    if not responsibility:
        responsibility = [{"party_type": "unknown", "party_id": None}]

    refundable = payment["refundable_total_brl"]
    captured = payment["captured_total_brl"]
    refunded = payment["refunded_total_brl"]
    refundable_issues = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "payment_mismatch",
        "refund_failed",
    }
    refund_amount = 0.0
    if case_status == "action_required" and issue in refundable_issues:
        if issue in {"duplicate_charge", "payment_mismatch"} and isinstance(
            captured, (int, float)
        ):
            already_refunded = float(refunded) if isinstance(refunded, (int, float)) else 0.0
            expected = payable_total if payable_total is not None else float(captured)
            refund_amount = max(0.0, float(captured) - expected - already_refunded)
        elif isinstance(refundable, (int, float)):
            refund_amount = float(refundable)
    refund_amount = _apply_financial_policy(state, issue, refund_amount)
    refund_lines = (
        [{"reason_code": issue.upper(), "amount_brl": refund_amount, "entity_id": order_ids[0]}]
        if refund_amount > 0 and order_ids
        else []
    )
    evidence_refs = state.evidence_refs[:30]
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": _unique(
                [
                    payment["verdict"] if payment["verdict"] != "reconciled" else "",
                    shipment["verdict"]
                    if shipment["verdict"] not in {"on_time", "insufficient_evidence"}
                    else "",
                ]
            ),
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_references,
            "shipment_ids": shipment_ids,
        },
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": order_ids,
            "rejected_candidates": _unique(rejected)[:20],
            "confidence": round(entity_confidence, 2),
        },
        "customer_context": {
            "customer_unique_id": customer_unique_id,
            "related_order_ids": related_order_ids,
        },
        "shipment_analysis": shipment,
        "payment_analysis": payment,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": cause, "rank": 1}],
            "responsible_parties": responsibility,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _money(refund_amount),
            "refund_lines": refund_lines,
        },
        "resolution_actions": _actions(issue, case_status, refund_amount),
    }
    claim_assessments = _claims(case, evidence_refs, issue, confidence, refund_amount)
    if claim_assessments:
        output["claim_assessments"] = claim_assessments

    _verify_invariants(output)
    trace.contracts.validate_output(output, "verifier output")

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier-agent",
        decision_code=issue.upper(),
        evidence_refs=[evidence["evidence_ref"] for evidence in state.by_tool("get_policy")],
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier-agent",
        decision_code="VERIFY_PUBLIC_CONTRACT",
        evidence_refs=evidence_refs[:20],
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code="INVARIANTS_PASSED",
        evidence_refs=evidence_refs[:20],
        attributes={
            "evidence_count": len(evidence_refs),
            "conflict_count": len(conflicts),
            "mcp_failure_count": len(state.failures),
        },
    )
    return output
