from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import httpx2

from . import VARIANT_ID
from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


@contextmanager
def _exclusive_run_lock(root: Path) -> Iterator[None]:
    """Prevent two batch runs from truncating or interleaving the same trace."""
    lock_path = root / "traces" / ".run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle: BinaryIO = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("another day09 run is already writing outputs/traces") from exc
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        lock_path.unlink(missing_ok=True)


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _create_competition_run(settings: Settings) -> dict[str, object]:
    """Open the server-side audit scope required by MCP data tools."""
    url = f"{settings.competition_api_url}/api/v2/runs"
    headers = {"Authorization": f"Bearer {settings.team_api_key}"}
    timeout = httpx2.Timeout(30.0, connect=15.0)
    async with httpx2.AsyncClient(headers=headers, timeout=timeout) as client:
        response = await client.post(url, json={"variant_id": VARIANT_ID})
    if response.status_code not in {200, 201}:
        detail = response.text.strip().replace("\n", " ")[:240]
        raise RuntimeError(
            f"could not create competition run ({response.status_code}): "
            f"{detail or 'empty response'}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("competition run endpoint returned a non-object response")
    return payload


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    run = await _create_competition_run(settings)
    run_id = run.get("run_id") or run.get("id")
    if run_id:
        print(f"Run scope: {run_id}")

    case_index = 0
    session_failures = 0
    received_cases: set[str] = set()
    while case_index < len(case_set.case_ids):
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                discovered_tools = await gateway.list_tools()
                if not discovered_tools:
                    raise RuntimeError("MCP Gateway returned no tools")
                while case_index < len(case_set.case_ids):
                    case_id = case_set.case_ids[case_index]
                    case = case_set.cases[case_id]
                    if case_id not in received_cases:
                        trace.emit(
                            case_id=case_id,
                            event_type="case_received",
                            actor="coordinator",
                        )
                        received_cases.add(case_id)
                    output = await solve_case(case, gateway, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    target = output_root / f"{case_id}.json"
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    temporary.replace(target)
                    trace.emit(
                        case_id=case_id,
                        event_type="case_finalized",
                        actor="coordinator",
                    )
                    case_index += 1
                    session_failures = 0
        except Exception as exc:
            if case_index >= len(case_set.case_ids):
                break
            session_failures += 1
            case_id = case_set.case_ids[case_index]
            if session_failures == 3:
                raise RuntimeError(
                    f"MCP session failed for {case_id} after 3 reconnects"
                ) from exc
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor="coordinator",
                target="coordinator",
                decision_code="MCP_SESSION_RECONNECT",
                attributes={
                    "attempt": session_failures,
                    "error_type": type(exc).__name__,
                },
            )
            await asyncio.sleep(1.5 * session_failures)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            with _exclusive_run_lock(root):
                asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
