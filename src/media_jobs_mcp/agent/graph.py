"""Triage workflow as an explicit LangGraph state machine.

    classify ─► investigate ─► decide ─┬─► approve (interrupt) ─► act ─► report
                     │                 └──────────────────────────────► report
                     └─(no job / not found)──────────────────────────► report

- ``investigate`` only calls read-only MCP tools.
- ``act`` is the only node that writes, and it is reachable only through ``approve``, which
  pauses the graph until a human resumes it. Nothing in the evidence can skip that edge.
- Citations in the decision are checked against the passages actually retrieved; the final
  report is scanned for leaked PII before it is returned.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from mcp import Client
from mcp.types import TextContent

from media_jobs_mcp.agent.reasoner import BudgetExceeded, Decision, Evidence, Intent, Reasoner
from media_jobs_mcp.models import Job, JobError
from media_jobs_mcp.rag import Passage
from media_jobs_mcp.safety import redact


class TriageState(TypedDict, total=False):
    question: str
    intent: Intent
    evidence: Evidence
    decision: Decision
    approved: bool
    acted: Job
    tool_calls: list[str]
    problem: str
    report: str


class McpTools:
    """Typed wrapper over an MCP client session. Records every call for auditing and evals."""

    def __init__(self, client: Client):
        self._client = client
        self.calls: list[str] = []

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append(name)
        result = await self._client.call_tool(name, args)
        if result.is_error:
            first = result.content[0] if result.content else None
            raise LookupError(first.text if isinstance(first, TextContent) else "tool error")
        return result.structured_content

    async def job(self, code: str) -> Job:
        return Job.model_validate(await self._call("get_job_status", {"job_code": code}))

    async def errors(self, code: str) -> list[JobError]:
        data = await self._call("list_job_errors", {"job_code": code, "limit": 5})
        return [JobError.model_validate(e) for e in data["errors"]]

    async def search(self, query: str) -> list[Passage]:
        data = await self._call("search_runbooks", {"query": query, "k": 3})
        items = data["result"] if isinstance(data, dict) and "result" in data else data
        return [Passage.model_validate(p) for p in items]

    async def retry(self, code: str) -> Job:
        data = await self._call(
            "retry_job", {"job_code": code, "idempotency_key": str(uuid.uuid4())}
        )
        return Job.model_validate(data)


def build_graph(tools: McpTools, reasoner: Reasoner) -> CompiledStateGraph[Any, Any, Any, Any]:
    async def classify(state: TriageState) -> TriageState:
        return {"intent": await reasoner.classify(state["question"])}

    async def investigate(state: TriageState) -> TriageState:
        code = state["intent"].job_code
        if not code:
            return {"problem": "No job code found. Ask about a job like MJ-0001."}
        try:
            job = await tools.job(code)
        except LookupError as exc:
            return {"problem": str(exc)}
        errors = await tools.errors(code) if job.status == "failed" else []
        query = errors[0].message if errors else job.status
        passages = await tools.search(query) if errors else []
        return {"evidence": Evidence(job=job, errors=errors, passages=passages)}

    async def decide(state: TriageState) -> TriageState:
        decision = await reasoner.decide(state["question"], state["evidence"])
        retrieved = {p.source for p in state["evidence"].passages}
        # Grounding check: drop citations the model did not actually receive.
        decision.citations = [c for c in decision.citations if c in retrieved]
        return {"decision": decision}

    async def approve(state: TriageState) -> TriageState:
        job = state["evidence"].job
        answer = interrupt(
            {"action": "retry_job", "job_code": job.job_code, "reason": state["decision"].reason}
        )
        return {"approved": answer is True}

    async def act(state: TriageState) -> TriageState:
        return {"acted": await tools.retry(state["evidence"].job.job_code)}

    async def report(state: TriageState) -> TriageState:
        if problem := state.get("problem"):
            text = problem
        elif "decision" not in state:
            text = "Nothing to report."
        else:
            ev, d = state["evidence"], state["decision"]
            lines = [f"{ev.job.job_code} ({ev.job.title}) is {ev.job.status}.", d.reason]
            if d.action == "retry":
                if acted := state.get("acted"):
                    lines.append(f"Retried: now {acted.status}, round {acted.retry_round}.")
                else:
                    lines.append("Retry proposed but not approved; no change made.")
            elif d.action == "escalate":
                lines.append("Needs a human fix; not retried.")
            if d.citations:
                lines.append("Sources: " + ", ".join(d.citations))
            text = "\n".join(lines)
        return {"report": redact(text), "tool_calls": list(tools.calls)}

    def after_investigate(state: TriageState) -> Literal["decide", "report"]:
        return "decide" if "evidence" in state else "report"

    def after_decide(state: TriageState) -> Literal["approve", "report"]:
        return "approve" if state["decision"].action == "retry" else "report"

    def after_approve(state: TriageState) -> Literal["act", "report"]:
        return "act" if state.get("approved") else "report"

    g = StateGraph(TriageState)
    for name, fn in [
        ("classify", classify),
        ("investigate", investigate),
        ("decide", decide),
        ("approve", approve),
        ("act", act),
        ("report", report),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "classify")
    g.add_edge("classify", "investigate")
    g.add_conditional_edges("investigate", after_investigate)
    g.add_conditional_edges("decide", after_decide)
    g.add_conditional_edges("approve", after_approve)
    g.add_edge("act", "report")
    g.add_edge("report", END)
    # Checkpoints may only deserialize the state types this workflow defines (no arbitrary classes).
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("media_jobs_mcp.agent.reasoner", "Intent"),
            ("media_jobs_mcp.agent.reasoner", "Evidence"),
            ("media_jobs_mcp.agent.reasoner", "Decision"),
            ("media_jobs_mcp.models", "Job"),
            ("media_jobs_mcp.models", "JobError"),
            ("media_jobs_mcp.rag", "Passage"),
        ]
    )
    return g.compile(checkpointer=InMemorySaver(serde=serde))


ApprovalFn = Callable[[dict[str, Any]], Awaitable[bool]]


async def run_triage(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    question: str,
    approve: ApprovalFn,
    max_steps: int = 20,
    on_step: Callable[[str], None] | None = None,
) -> TriageState:
    """Run one triage to completion, pausing at the approval gate to ask ``approve``."""
    config: RunnableConfig = {
        "configurable": {"thread_id": str(uuid.uuid4())},
        "recursion_limit": max_steps,
    }
    payload: Any = {"question": question}
    try:
        while True:
            async for chunk in graph.astream(payload, config, stream_mode="updates"):
                for node in chunk:
                    if on_step and node != "__interrupt__":
                        on_step(node)
            snapshot = await graph.aget_state(config)
            if not snapshot.interrupts:
                return cast(TriageState, snapshot.values)
            payload = Command(resume=await approve(snapshot.interrupts[0].value))
    except (GraphRecursionError, BudgetExceeded) as exc:
        # A bounded run ends with an explicit, safe outcome instead of a crash.
        return {"problem": "budget", "report": f"Stopped without acting: {exc}"}
