from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.submission import validate_artifacts
from student_agent.trace import TraceWriter
from student_agent.workflow import TOOL_OWNER, solve_case


class FakeGateway:
    tools = {
        "get_customer_history",
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
        "get_refund_timeline",
        "get_shipment_summary",
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.case_ids: list[str] = []

    async def describe_tools(self) -> dict[str, dict[str, Any]]:
        return {name: {} for name in self.tools}

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        self.case_ids.append(case_id)
        order_id = arguments.get("order_id", "")
        data: Any = {
            "get_order": {
                "order_id": order_id,
                "order_status": "canceled",
                "customer_unique_id": "CUSTOMER_1",
            },
            "get_order_items": [
                {"order_item_id": "ITEM_1", "seller_id": "SELLER_1", "price": 100.0}
            ],
            "get_order_payments": [
                {"payment_sequential": 1, "payment_type": "credit_card", "payment_value": 100.0}
            ],
            "get_payment_timeline": {"events": [{"event": "captured"}]},
            "get_refund_timeline": {"refund_status": "not_started", "refunded_amount": 0.0},
            "get_customer_history": {"order_ids": ["ORDER_1", "ORDER_OLD"]},
            "get_policy": {"policy_version": arguments.get("policy_version")},
            "get_shipment_summary": {},
        }[tool_name]
        raw = json.dumps([tool_name, case_id, arguments], sort_keys=True).encode()
        token = hashlib.sha256(raw).hexdigest()
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{token[:24]}",
            "result_hash": f"sha256:{hashlib.sha256(json.dumps(data).encode()).hexdigest()}",
            "domain": {
                "get_order": "order",
                "get_order_items": "item",
                "get_order_payments": "payment",
                "get_payment_timeline": "payment",
                "get_refund_timeline": "refund",
                "get_customer_history": "customer",
                "get_policy": "policy",
                "get_shipment_summary": "shipment",
            }[tool_name],
            "data": data,
        }


class OverchargeGateway(FakeGateway):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        evidence = await super().call(tool_name, case_id=case_id, **arguments)
        if tool_name == "get_order":
            evidence["data"]["order_status"] = "delivered"
        elif tool_name == "get_order_payments":
            evidence["data"][0]["payment_value"] = 130.0
        elif tool_name == "get_payment_timeline":
            evidence["data"] = {"status": "capture_mismatch"}
        return evidence


def test_workflow_produces_contract_valid_evidence_linked_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = FakeGateway()
    case = {
        "case_id": "CASE_001",
        "order_id": "ORDER_1",
        "claim_id": "CLAIM_1",
        "claim": "Customer paid for a canceled order and requests a refund",
        "policy_version": "policy-v1",
    }

    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id="CASE_001", event_type="case_finalized", actor="coordinator")

    contracts.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 100.0
    assert output["entity_resolution"]["resolved_order_ids"] == ["ORDER_1"]
    assert "get_refund_timeline" in {name for name, _ in gateway.calls}
    assert "get_shipment_summary" not in {name for name, _ in gateway.calls}
    assert gateway.case_ids and set(gateway.case_ids) == {"CASE_001"}
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    consumed = {
        evidence_ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for evidence_ref in event["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    for event in events:
        if event["event_type"] == "tool_result_consumed":
            assert event["actor"] == TOOL_OWNER[event["tool_name"]]
    assert events[-2]["event_type"] == "verification_completed"
    assert events[-1]["event_type"] == "case_finalized"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    (output_root / "CASE_001.json").write_text(json.dumps(output), encoding="utf-8")
    case_set = CaseSet("test-v1", "l3b", ("CASE_001",), {"CASE_001": case})
    validated, _ = validate_artifacts(tmp_path, case_set, contracts)
    assert validated["CASE_001"] == output


def test_workflow_does_not_invent_entities_without_order_scope(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway()

    output = asyncio.run(
        solve_case({"case_id": "CASE_002", "claim": "unknown"}, gateway, trace)
    )

    contracts.validate_output(output, "test output")
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["affected_entities"]["order_ids"] == []
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert gateway.calls == []


def test_policy_and_verifier_refund_only_the_payment_overcharge(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_003",
                "order_id": "ORDER_1",
                "claim": "The captured payment amount is wrong",
            },
            OverchargeGateway(),
            trace,
        )
    )

    assert output["assessment"]["primary_issue"] == "payment_mismatch"
    assert output["financial_resolution"]["recommended_refund_brl"] == 30.0
    assert output["financial_resolution"]["refund_lines"][0]["amount_brl"] == 30.0
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "payment_provider", "party_id": None}
    ]
    assert "ESCALATE_PAYMENT_PROVIDER" in output["resolution_actions"]
