from __future__ import annotations

import json
from pathlib import Path

import pytest

from student_agent import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from student_agent.cases import CaseSet, load_case_set
from student_agent.contracts import Contracts
from student_agent.submission import build_manifest


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_load_case_set_rejects_wrong_variant(tmp_path: Path) -> None:
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": "l3a", "case_ids": ["CASE_001"]},
    )
    write_json(tmp_path / "inputs" / "CASE_001.json", {"case_id": "CASE_001"})
    with pytest.raises(ValueError, match="expected variant"):
        load_case_set(tmp_path, expected_count=1)


def test_load_case_set_accepts_exact_input_inventory(tmp_path: Path) -> None:
    case_ids = ["CASE_001", "CASE_002"]
    write_json(
        tmp_path / "case-set.json",
        {"case_set_version": "test-v1", "variant_id": VARIANT_ID, "case_ids": case_ids},
    )
    for case_id in case_ids:
        write_json(tmp_path / "inputs" / f"{case_id}.json", {"case_id": case_id})
    loaded = load_case_set(tmp_path, expected_count=2)
    assert loaded.case_ids == tuple(case_ids)


def test_generated_manifest_matches_public_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    case_set = CaseSet("test-v1", VARIANT_ID, ("CASE_001",), {})
    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)
    assert manifest["output_schema_version"] == OUTPUT_SCHEMA_VERSION


def test_solve_case_produces_schema_valid_output() -> None:
    from student_agent.workflow import solve_case

    class FakeGateway:
        async def list_tools(self) -> list[str]:
            return [
                "get_customer_history",
                "get_order_details",
                "get_shipment_tracking",
                "get_payment_history",
                "get_policy",
            ]

        async def call(self, tool_name: str, *, case_id: str, **kwargs: object) -> dict[str, object]:
            payload = {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789",
                "result_hash": "sha256:00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
                "domain": {
                    "get_customer_history": "customer",
                    "get_order_details": "order",
                    "get_shipment_tracking": "shipment",
                    "get_payment_history": "payment",
                    "get_policy": "policy",
                }[tool_name],
                "data": {
                    "case_id": case_id,
                    "tool_name": tool_name,
                    "customer_unique_id": kwargs.get("customer_unique_id", "CUST_1001"),
                    "order_id": kwargs.get("order_id", "ORDER_1001"),
                    "shipment_id": kwargs.get("shipment_id", "SHIP_1001"),
                    "status": "delivered" if "shipment" in tool_name else "ok",
                },
            }
            return payload

    class FakeTrace:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def emit(self, **kwargs: object) -> dict[str, object]:
            self.events.append(kwargs)
            return kwargs

    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    case = {
        "case_id": "CASE_001",
        "customer_unique_id": "CUST_1001",
        "order_id": "ORDER_1001",
        "seller_id": "SELLER_77",
        "item_ids": ["ITEM_1"],
        "shipment_id": "SHIP_1001",
        "payment_references": ["PAY_1001"],
        "issue_summary": "delivery arrived late and payment was refunded",
        "claim_id": "CLAIM_900",
    }
    trace = FakeTrace()
    output = __import__("asyncio").run(solve_case(case, FakeGateway(), trace))
    contracts.validate_output(output, "outputs/CASE_001.json")
    assert output["case_id"] == "CASE_001"
    assert output["entity_resolution"]["resolved_order_ids"] == ["ORDER_1001"]
    assert trace.events
    assert any(event.get("event_type") == "verification_completed" for event in trace.events)


def test_solve_case_ignores_nested_customer_request_objects() -> None:
    from student_agent.workflow import solve_case

    class FakeGateway:
        async def list_tools(self) -> list[str]:
            return ["get_customer_history"]

        async def call(self, tool_name: str, *, case_id: str, **kwargs: object) -> dict[str, object]:
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_0123456789abcdef0123456789",
                "result_hash": "sha256:00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
                "domain": "customer",
                "data": {"customer_unique_id": "customer-597dc70ef07b", "order_id": "af0bbb47f125381ce9f3597dc70ef07b"},
            }

    class FakeTrace:
        def emit(self, **kwargs: object) -> dict[str, object]:
            return kwargs

    case = {
        "case_id": "L3B_CASE_001",
        "customer_request": {
            "language": "vi",
            "message": "Điều tra đa nguồn: resolve đúng order, kiểm tra customer history, shipment, payment và policy trước khi kết luận.",
            "claimed_order_id": "af0bbb47f125381ce9f3597dc70ef07b",
            "claims": [{"claim_id": "claim-001-a", "topic": "late_delivery_logistics"}],
        },
        "candidate_order_ids": ["af0bbb47f125381ce9f3597dc70ef07b", "candidate-001"],
        "customer_unique_id_hint": "customer-597dc70ef07b",
    }

    output = __import__("asyncio").run(solve_case(case, FakeGateway(), FakeTrace()))
    assert output["customer_context"]["customer_unique_id"] == "customer-597dc70ef07b"
