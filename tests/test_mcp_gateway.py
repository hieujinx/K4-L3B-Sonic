from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.mcp_gateway import EvidenceGateway


class RecordingContracts:
    def __init__(self) -> None:
        self.validated: list[tuple[dict[str, Any], str]] = []

    def validate_evidence(self, value: dict[str, Any], label: str = "MCP response") -> None:
        self.validated.append((value, label))


class FakeSession:
    def __init__(self, *, error: bool = False) -> None:
        self.error = error
        self.list_count = 0
        self.arguments: dict[str, str] | None = None
        self.evidence = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_12345678901234567890",
            "result_hash": f"sha256:{'a' * 64}",
            "domain": "order",
            "data": {"order_id": "ORDER_1"},
        }

    async def list_tools(self) -> Any:
        self.list_count += 1
        tool = SimpleNamespace(name="get_order", inputSchema={"type": "object"})
        return SimpleNamespace(tools=[tool])

    async def call_tool(self, tool_name: str, *, arguments: dict[str, str]) -> Any:
        self.arguments = arguments
        if self.error:
            return SimpleNamespace(
                isError=True,
                content=[SimpleNamespace(text="denied")],
                structuredContent=None,
            )
        return SimpleNamespace(
            isError=False,
            content=[],
            structuredContent=self.evidence,
        )


def test_gateway_caches_discovery_and_preserves_authoritative_evidence() -> None:
    session = FakeSession()
    contracts = RecordingContracts()
    gateway = EvidenceGateway(session, contracts)  # type: ignore[arg-type]

    assert asyncio.run(gateway.list_tools()) == ["get_order"]
    assert asyncio.run(gateway.describe_tools()) == {"get_order": {"type": "object"}}
    evidence = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="ORDER_1"))

    assert session.list_count == 1
    assert session.arguments == {"case_id": "CASE_001", "order_id": "ORDER_1"}
    assert evidence is session.evidence
    assert evidence["evidence_ref"] == "ev_12345678901234567890"
    assert contracts.validated == [(evidence, "MCP tool get_order")]


def test_gateway_rejects_mcp_error_without_fabricating_evidence() -> None:
    gateway = EvidenceGateway(FakeSession(error=True), RecordingContracts())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="MCP tool get_order failed: denied"):
        asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="ORDER_1"))
