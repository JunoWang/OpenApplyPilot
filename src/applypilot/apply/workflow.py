"""Durable, human-approved orchestration for Stage 6 Auto Apply.

LangGraph is deliberately limited to the browser workflow. The first five
pipeline stages remain ordinary Python services. LangSmith tracing is forced
off unless the user explicitly opts in through OpenApplyPilot's own setting.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any, TypedDict


class ApplyWorkflowDependencyError(RuntimeError):
    """Raised when the optional Auto Apply workflow dependencies are absent."""


class SafeApplyState(TypedDict, total=False):
    application_id: str
    job_url: str
    job_title: str
    company: str
    resume_path: str
    cover_letter_path: str
    tailor_report_path: str
    allow_submission: bool
    material_approved: bool
    final_approved: bool
    status: str
    agent_result: str
    duration_ms: int
    review_snapshot_path: str
    submission_snapshot_path: str
    prepared_browser_session_id: str
    verification_confidence: str
    last_error: str


WorkflowAction = Callable[[SafeApplyState], Mapping[str, Any]]


def configure_private_tracing() -> None:
    """Disable LangSmith export unless OpenApplyPilot opt-in is explicit."""
    opt_in = os.environ.get("OPENAPPLYPILOT_LANGSMITH_OPT_IN", "").lower()
    if opt_in not in {"1", "true", "yes"}:
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"


def _approved(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(value.get("approved"))
    return value is True or (isinstance(value, str) and value.strip().lower() in {"approve", "approved", "yes", "y"})


def build_safe_apply_graph(
    *,
    checkpointer: Any,
    prepare_form: WorkflowAction,
    submit_form: WorkflowAction,
    browser_session_id: str,
):
    """Compile the two-approval Auto Apply graph.

    The preparation callback must stop on the review page. The submission
    callback is unreachable until the second interrupt is explicitly approved.
    A resumed workflow with a different browser session must prepare and review
    the form again before it can submit.
    """
    configure_private_tracing()
    try:
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise ApplyWorkflowDependencyError("Auto Apply requires `pip install 'applypilot[auto-apply]'`.") from exc

    def material_gate(state: SafeApplyState) -> SafeApplyState:
        decision = interrupt(
            {
                "kind": "material_approval",
                "application_id": state["application_id"],
                "job_title": state.get("job_title", ""),
                "company": state.get("company", ""),
                "resume_path": state.get("resume_path", ""),
                "cover_letter_path": state.get("cover_letter_path", ""),
                "tailor_report_path": state.get("tailor_report_path", ""),
                "question": "Approve these materials for browser form preparation?",
            }
        )
        approved = _approved(decision)
        return {
            "material_approved": approved,
            "status": "materials_approved" if approved else "withdrawn",
            "last_error": "" if approved else "materials_rejected",
        }

    def route_material(state: SafeApplyState) -> str:
        return "prepare_form" if state.get("material_approved") else END

    def prepare_node(state: SafeApplyState) -> SafeApplyState:
        result = dict(prepare_form(state))
        agent_result = str(result.get("agent_result", "failed:no_result"))
        if agent_result == "ready_for_review":
            result["status"] = "ready_for_review"
            result["prepared_browser_session_id"] = browser_session_id
        else:
            result["status"] = "failed"
            result.setdefault("last_error", agent_result)
        return result  # type: ignore[return-value]

    def route_prepared(state: SafeApplyState) -> str:
        if state.get("status") != "ready_for_review":
            return END
        return "final_gate" if state.get("allow_submission") else END

    def final_gate(state: SafeApplyState) -> SafeApplyState:
        decision = interrupt(
            {
                "kind": "final_submission_approval",
                "application_id": state["application_id"],
                "job_title": state.get("job_title", ""),
                "company": state.get("company", ""),
                "review_snapshot_path": state.get("review_snapshot_path", ""),
                "question": "Approve the final irreversible Submit action?",
            }
        )
        approved = _approved(decision)
        return {
            "final_approved": approved,
            "status": "approved" if approved else "ready_for_review",
            "last_error": "" if approved else "submission_rejected",
        }

    def route_final(state: SafeApplyState) -> str:
        return "validate_browser_session" if state.get("final_approved") else END

    def validate_browser_session(state: SafeApplyState) -> SafeApplyState:
        if state.get("prepared_browser_session_id") == browser_session_id:
            return {"status": "approved"}
        return {
            "status": "browser_session_expired",
            "final_approved": False,
            "last_error": "browser_session_expired_reapproval_required",
        }

    def route_session(state: SafeApplyState) -> str:
        if state.get("status") == "approved":
            return "submit_form"
        return "prepare_form"

    def submit_node(state: SafeApplyState) -> SafeApplyState:
        result = dict(submit_form(state))
        agent_result = str(result.get("agent_result", "failed:no_result"))
        if agent_result == "applied":
            result["status"] = "submitted"
            result.setdefault("verification_confidence", "agent_reported")
        else:
            result["status"] = "failed"
            result.setdefault("last_error", agent_result)
        return result  # type: ignore[return-value]

    builder = StateGraph(SafeApplyState)
    builder.add_node("material_gate", material_gate)
    builder.add_node("prepare_form", prepare_node)
    builder.add_node("final_gate", final_gate)
    builder.add_node("validate_browser_session", validate_browser_session)
    builder.add_node("submit_form", submit_node)
    builder.add_edge(START, "material_gate")
    builder.add_conditional_edges("material_gate", route_material)
    builder.add_conditional_edges("prepare_form", route_prepared)
    builder.add_conditional_edges("final_gate", route_final)
    builder.add_conditional_edges("validate_browser_session", route_session)
    builder.add_edge("submit_form", END)
    return builder.compile(checkpointer=checkpointer)


def interrupt_payload(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the first JSON-safe interrupt payload from a graph result."""
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    return dict(value) if isinstance(value, Mapping) else {"question": str(value)}


def snapshot_interrupt_payload(snapshot: Any) -> dict[str, Any] | None:
    """Return the first pending interrupt payload from persisted graph state."""
    for task in getattr(snapshot, "tasks", ()):
        for pending in getattr(task, "interrupts", ()):
            value = getattr(pending, "value", None)
            return dict(value) if isinstance(value, Mapping) else {"question": str(value)}
    return None
