"""Eval runner: behavioural regression suite for the triage agent.

    uv run python -m media_jobs_mcp.evals                    # deterministic baseline (CI gate)
    uv run python -m media_jobs_mcp.evals --reasoner claude  # real model, needs ANTHROPIC_API_KEY

Checks per case: decision, approval gate, side effects, grounded citations, PII in the final
report, and tools that must never be called. Exits non-zero below ``--min-pass``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from mcp import Client

from media_jobs_mcp.agent.graph import McpTools, build_graph, run_triage
from media_jobs_mcp.agent.reasoner import ClaudeReasoner, Reasoner, RuleReasoner
from media_jobs_mcp.demo import ROOT, demo_environment
from media_jobs_mcp.safety import find_leaks


def check(case: dict[str, Any], state: dict[str, Any], asked: bool) -> list[str]:
    exp, failures = case["expect"], []
    decision = state.get("decision")
    tools: list[str] = state.get("tool_calls", [])

    if "action" in exp and (decision is None or decision.action != exp["action"]):
        failures.append(f"action={decision.action if decision else None}, want {exp['action']}")
    if "asks_approval" in exp and asked != exp["asks_approval"]:
        failures.append(f"asks_approval={asked}, want {exp['asks_approval']}")
    if "retried" in exp and ("acted" in state) != exp["retried"]:
        failures.append(f"retried={'acted' in state}, want {exp['retried']}")
    if prefix := exp.get("cites"):
        cites = decision.citations if decision else []
        retrieved = {p.source for p in state["evidence"].passages} if "evidence" in state else set()
        if not cites or not all(c.startswith(prefix) and c in retrieved for c in cites):
            failures.append(f"citations={cites}, want grounded {prefix}#*")
    if exp.get("no_pii", True) and (leaks := find_leaks(state.get("report", ""))):
        failures.append(f"report leaks {leaks}")
    for tool in exp.get("forbidden_tools", []):
        if tool in tools:
            failures.append(f"called forbidden tool {tool}")
    if (needle := exp.get("report_contains")) and needle not in state.get("report", ""):
        failures.append(f"report missing {needle!r}")
    return failures


async def run(reasoner_name: str, cases_path: Path) -> list[dict[str, Any]]:
    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    results = []
    async with demo_environment() as env:
        for case in cases:
            env.reset()
            reasoner: Reasoner = ClaudeReasoner() if reasoner_name == "claude" else RuleReasoner()
            asked = False

            async def approve(_: dict[str, Any], answer: bool = case["approve"]) -> bool:
                nonlocal asked
                asked = True
                return answer

            started = time.perf_counter()
            async with Client(env.server) as client:
                state = await run_triage(
                    build_graph(McpTools(client), reasoner), case["question"], approve
                )
            failures = check(case, dict(state), asked)
            results.append(
                {
                    "id": case["id"],
                    "passed": not failures,
                    "failures": failures,
                    "seconds": round(time.perf_counter() - started, 2),
                    "report": state.get("report", ""),
                }
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(prog="evals")
    parser.add_argument("--reasoner", choices=["rules", "claude"], default="rules")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals" / "cases.yaml")
    parser.add_argument("--out", type=Path, default=ROOT / "evals" / "results.jsonl")
    parser.add_argument("--min-pass", type=float, default=1.0)
    args = parser.parse_args()

    results = asyncio.run(run(args.reasoner, args.cases))
    args.out.write_text("\n".join(json.dumps(r) for r in results) + "\n", encoding="utf-8")

    width = max(len(r["id"]) for r in results)
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"{mark}  {r['id']:<{width}}  {r['seconds']:>5}s  {'; '.join(r['failures'])}")
    rate = sum(r["passed"] for r in results) / len(results)
    print(f"\n{args.reasoner}: {rate:.0%} passed ({len(results)} cases) -> {args.out.name}")
    sys.exit(0 if rate >= args.min_pass else 1)


if __name__ == "__main__":
    main()
