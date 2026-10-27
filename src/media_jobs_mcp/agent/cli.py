"""Command-line triage agent.

    uv run mjm-agent "Why did MJ-0001 fail?"                  # deterministic, no API key
    uv run mjm-agent "Why did MJ-0001 fail?" --reasoner claude  # needs ANTHROPIC_API_KEY

Runs against the self-contained demo environment (embedded Postgres, in-process API and MCP).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from mcp import Client

from media_jobs_mcp.agent.graph import McpTools, build_graph, run_triage
from media_jobs_mcp.agent.reasoner import ClaudeReasoner, Reasoner, RuleReasoner
from media_jobs_mcp.demo import demo_environment

STEP_LABELS = {
    "classify": "understanding the question",
    "investigate": "reading status, errors and runbooks (read-only tools)",
    "decide": "deciding",
    "approve": "approval recorded",
    "act": "retrying the job",
    "report": "writing the report",
}


async def ask_human(request: dict[str, Any]) -> bool:
    print(f"\n  ? Approve {request['action']} on {request['job_code']}?")
    print(f"    reason: {request['reason']}")
    answer = await asyncio.to_thread(input, "    [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


async def main_async(question: str, reasoner: Reasoner, auto: bool | None) -> None:
    async def approve(request: dict[str, Any]) -> bool:
        if auto is None:
            return await ask_human(request)
        print(
            f"\n  ? Approve {request['action']} on {request['job_code']}? -> {'yes' if auto else 'no'}"
        )
        return auto

    async with demo_environment() as env, Client(env.server) as client:
        graph = build_graph(McpTools(client), reasoner)
        state = await run_triage(
            graph, question, approve, on_step=lambda n: print(f"  - {STEP_LABELS.get(n, n)}")
        )
    print("\n" + state.get("report", ""))


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    for noisy in ("httpx", "pgserver", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="mjm-agent")
    parser.add_argument("question")
    parser.add_argument("--reasoner", choices=["rules", "claude"], default="rules")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--yes", action="store_true", help="approve writes without prompting")
    group.add_argument("--no", action="store_true", help="deny writes without prompting")
    args = parser.parse_args()

    if args.reasoner == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set (create one at console.anthropic.com).")
    reasoner: Reasoner = ClaudeReasoner() if args.reasoner == "claude" else RuleReasoner()
    auto = True if args.yes else False if args.no else None
    print(f"> {args.question}")
    asyncio.run(main_async(args.question, reasoner, auto))


if __name__ == "__main__":
    main()
