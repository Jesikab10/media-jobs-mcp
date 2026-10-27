"""Graph routing with the deterministic reasoner, over a real MCP session and real Postgres."""

from typing import Any

import pytest
from mcp import Client

from media_jobs_mcp.agent.graph import McpTools, build_graph, run_triage
from media_jobs_mcp.agent.reasoner import Decision, Evidence, Intent, RuleReasoner
from media_jobs_mcp.api_client import JobsApiClient
from media_jobs_mcp.config import Settings
from media_jobs_mcp.safety import find_leaks
from media_jobs_mcp.server import build_server


@pytest.fixture
def triage(settings: Settings, api: JobsApiClient) -> Any:
    """Run one triage over a fresh MCP session (opened and closed inside the test's task)."""

    async def run(question: str, approver: Any, reasoner: Any = None, **kw: Any) -> Any:
        async with Client(build_server(settings, api)) as client:
            graph = build_graph(McpTools(client), reasoner or RuleReasoner())
            return await run_triage(graph, question, approver, **kw)

    return run


class Approver:
    def __init__(self, answer: bool):
        self.answer = answer
        self.asked: list[dict[str, Any]] = []

    async def __call__(self, request: dict[str, Any]) -> bool:
        self.asked.append(request)
        return self.answer


async def test_transient_failure_is_retried_after_approval(triage: Any) -> None:
    approver = Approver(True)
    state = await triage("Why did MJ-0001 fail?", approver, RuleReasoner())
    assert state["decision"].action == "retry"
    assert approver.asked[0]["job_code"] == "MJ-0001"
    assert state["acted"].retry_round == 1
    assert "retry_job" in state["tool_calls"]


async def test_retry_without_approval_changes_nothing(triage: Any) -> None:
    state = await triage("Why did MJ-0001 fail?", Approver(False), RuleReasoner())
    assert "acted" not in state
    assert "retry_job" not in state["tool_calls"]
    assert "not approved" in state["report"]


async def test_input_error_is_escalated_never_retried(triage: Any) -> None:
    approver = Approver(True)
    state = await triage("fix MJ-0002", approver, RuleReasoner())
    assert state["decision"].action == "escalate"
    assert approver.asked == []
    assert "retry_job" not in state["tool_calls"]


async def test_unknown_job_reports_without_crashing(triage: Any) -> None:
    state = await triage("status of MJ-9999", Approver(True), RuleReasoner())
    assert "404" in state["report"]


class HallucinatingReasoner(RuleReasoner):
    async def decide(self, question: str, evidence: Evidence) -> Decision:
        return Decision(action="escalate", reason="see docs", citations=["made-up.md#9"])


async def test_citations_not_retrieved_are_dropped(triage: Any) -> None:
    state = await triage("fix MJ-0002", Approver(True), HallucinatingReasoner())
    assert state["decision"].citations == []


class LeakyReasoner(RuleReasoner):
    async def decide(self, question: str, evidence: Evidence) -> Decision:
        return Decision(action="escalate", reason="Call ops at ops@example.com or +1 415 555 0100.")


async def test_report_never_leaks_pii_even_if_the_model_does(triage: Any) -> None:
    state = await triage("fix MJ-0002", Approver(True), LeakyReasoner())
    assert find_leaks(state["report"]) == []


class LoopingReasoner(RuleReasoner):
    async def classify(self, question: str) -> Intent:
        return Intent(kind="triage", job_code="MJ-0001")


async def test_step_budget_ends_the_run_safely(triage: Any) -> None:
    approver = Approver(True)
    state = await triage("x", approver, LoopingReasoner(), max_steps=2)
    assert state["report"].startswith("Stopped without acting")
    assert approver.asked == []
