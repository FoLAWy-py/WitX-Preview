"""What one turn of a run carries between its phases: what was prepared before the model
call, what its tool calls decided, and the reading of the model's response."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain.model import GoalVerdict, Run, RunSummary, Step, Task
from witx.harness.risk import RiskCheck
from witx.models import ModelResponse

if TYPE_CHECKING:
    from witx.harness.loop import Harness


def utcnow() -> datetime:
    return datetime.now(UTC)


def reply_text(response: ModelResponse) -> str:
    """What the agent said in words this turn."""
    return "\n".join(
        part.get("text", "")
        for item in response.output
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


def calls_of(response: ModelResponse) -> list[dict[str, Any]]:
    return [i for i in response.output if i.get("type") == "function_call"]


def has_goal(task: Task) -> bool:
    """Whether the task has a goal the user has not paused (else a completion is judged
    against the request)."""
    return bool(task.goal) and not task.goal_paused


@dataclass(frozen=True)
class Prepared:
    run: Run
    task: Task
    epoch: int
    step_id: UUID
    items: list[dict[str, Any]]  # with screenshot placeholders
    allowed_providers: tuple[str, ...] | None  # the tenant's choice (see ModelRequest)
    # For context compression: the steps so far, what this step adds, the summary
    # in use, the last attempt at one, the last step's input size, and unknown inputs.
    steps: list[Step] = field(default_factory=list)
    new: list[dict[str, Any]] = field(default_factory=list)
    summary: RunSummary | None = None
    last_attempt: int | None = None
    last_input_tokens: int | None = None
    unknown: list[str] = field(default_factory=list)
    input_seq: int = 0  # the newest input this step reads


@dataclass
class Outcome:
    calls: int = 0  # the tool calls of this turn
    plan: str | None = None  # a plan presented for approval: the run waits for the user
    goal_continue: bool = False  # the turn ended, the goal is not met: work goes on
    input_seen: bool = False
    question: str | None = None
    ask_main: bool = False  # a sub-agent's question is for the main agent
    options: list[str] = field(default_factory=list)  # answers offered with the question
    recommended: int | None = None  # the index of the one the agent recommends
    repeating: bool = False  # the agent went in circles: it stops for the user
    goal: GoalVerdict | None = None  # whether the task's goal holds at this turn's end
    completed: dict[str, Any] | None = None


@dataclass
class Turn:
    """What each tool call of a turn is handled against, in the turn's transaction."""

    harness: "Harness"
    conn: AsyncConnection
    task: Task
    run: Run
    outcome: Outcome
    verdict: tuple[str, RiskCheck] | None  # the risk check of the turn's input, by call ID
