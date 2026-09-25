from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from student_agent.cases import CaseSet, load_case_set
from student_agent.contracts import Contracts
from student_agent.submission import build_manifest
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


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


class FakeGateway:
    def __init__(self) -> None:
        self.tools = {
            "get_order",
            "get_order_items",
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_shipment_summary",
            "get_customer_history",
            "get_sellers",
            "get_policy",
        }

    async def list_tools(self) -> list[str]:
        return sorted(self.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        suffix = f"{tool_name}_{arguments.get('order_id', 'case')}".replace("_", "-")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix:0<20}"[:40],
            "result_hash": "sha256:" + "a" * 64,
            "domain": "order" if tool_name == "get_order" else "policy",
            "data": {"order_id": arguments.get("order_id", "ORD_001")},
        }


def test_workflow_builds_schema_valid_output(tmp_path: Path) -> None:
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = asyncio.run(
        solve_case(
            {"case_id": "CASE_001", "exact_order_id": "ORD_001"},
            FakeGateway(),
            trace,
        )
    )
    contracts.validate_output(output, "workflow output")
    assert output["entity_resolution"]["resolved_order_ids"] == ["ORD_001"]
