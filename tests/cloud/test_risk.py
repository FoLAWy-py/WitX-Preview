"""The independent risk assessor: what it records for audit."""

from dataclasses import dataclass, field
from typing import Any, get_args
from uuid import uuid4

from witx.domain.model import EFFECT_DEFINITIONS, ORDINARY_RECORDS
from witx.harness import prompt, risk
from witx.harness.context import IMAGE_PLACEHOLDER
from witx.harness.risk import RiskAssessor
from witx.models import ModelError, ModelRequest, ModelResponse
from witx_contracts import api

from tests.cloud.support import png

VERDICT = {
    "type": "function_call",
    "name": "report_risk",
    "call_id": "c1",
    "arguments": '{"sensitive": false, "effects": [], "reason": "opens a list"}',
}


@dataclass
class OneAnswer:
    fail: bool = False
    requests: list[ModelRequest] = field(default_factory=list)

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.fail:
            raise ModelError("rate_limited", "slow down", retryable=True)
        usage = {"input_tokens": 812, "output_tokens": 31}
        return ModelResponse(id="r1", output=[VERDICT], usage=usage, raw={"id": "r1"})


async def assess(model: OneAnswer) -> Any:
    observation_id = uuid4()
    check = await RiskAssessor(model).assess(
        model="risk-model",
        observation_id=observation_id,
        screenshot=png(4, 8),
        payload={"kind": "tap", "x": 1, "y": 2},
        foreground_app="com.apple.reminders",
        target="Add button",
        expected_effect="A new reminder opens",
        allowed_providers=None,
    )
    return check, observation_id


async def test_an_assessment_keeps_its_model_call_without_the_screenshot() -> None:
    model = OneAnswer()
    check, observation_id = await assess(model)
    assert check.verdict.sensitive is False and not check.verdict.failed
    record = check.record
    assert record["model"] == "risk-model" and record["error"] is None
    assert record["usage"] == {"input_tokens": 812, "output_tokens": 31}
    assert record["response"] == {"id": "r1"} and record["duration_ms"] >= 0
    image = record["request"]["input"][0]["content"][1]
    assert image == {"type": "input_image", IMAGE_PLACEHOLDER: str(observation_id)}
    assert "base64" in str(model.requests[0].input)  # the model itself saw the image


async def test_a_failed_assessment_is_recorded_and_counts_as_sensitive() -> None:
    check, _ = await assess(OneAnswer(fail=True))
    assert check.verdict.failed and check.verdict.sensitive
    assert check.record["error"].startswith("rate_limited")
    assert "usage" not in check.record


def test_the_agent_and_the_risk_check_share_each_effect_definition() -> None:
    for name, meaning in EFFECT_DEFINITIONS.items():
        line = f"- {name}: {meaning}"
        assert line in prompt.INSTRUCTIONS and line in risk.INSTRUCTIONS
    assert ORDINARY_RECORDS in prompt.INSTRUCTIONS and ORDINARY_RECORDS in risk.INSTRUCTIONS


def test_a_change_alone_is_not_sensitive() -> None:
    call = {
        "type": "function_call",
        "name": "report_risk",
        "arguments": '{"sensitive": false, "effects": ["change_data"], "reason": "saves"}',
    }
    verdict = risk.parse_verdict([call])
    assert verdict.effects == ["change_data"] and not verdict.sensitive


def test_the_api_knows_every_effect_the_agent_and_the_risk_check_can_name() -> None:
    assert set(get_args(api.EffectName)) == set(EFFECT_DEFINITIONS)


CLEAR = {name: 0.01 for name in [*EFFECT_DEFINITIONS, "consent"]}


@dataclass
class FakeDecisions:
    """Stands in for the Decisions API; answers ``probabilities`` or fails."""

    probabilities: dict[str, float]
    fail: bool = False
    calls: int = 0

    async def decide(self, *, model: str, input: Any, questions: Any) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise ModelError("http_503", "busy", retryable=True)
        return {
            "answers": [
                {
                    "type": "predicate",
                    "name": q["name"],
                    "probability": self.probabilities[q["name"]],
                }
                for q in questions
            ],
            "usage": {"input_tokens": 900},
        }


async def fast_assess(
    model: OneAnswer,
    decisions: FakeDecisions,
    *,
    fast: bool = True,
    name: str = "gpt-6-luna",
    allowed: tuple[str, ...] | None = None,
) -> Any:
    assessor = RiskAssessor(
        model, decisions=decisions, decision_models={"gpt-6-luna": "gpt-6-luna"}
    )
    return await assessor.assess(
        model=name,
        observation_id=uuid4(),
        screenshot=png(4, 8),
        payload={"kind": "tap", "x": 1, "y": 2},
        foreground_app="com.android.settings",
        target="Battery",
        expected_effect="Battery details open",
        allowed_providers=allowed,
        fast=fast,
    )


async def test_a_sure_fast_check_decides_without_the_full_check() -> None:
    model, decisions = OneAnswer(), FakeDecisions(CLEAR)
    check = await fast_assess(model, decisions)
    assert check.verdict.sensitive is False and not check.verdict.failed
    assert model.requests == []  # no full check
    assert check.record["fast"]["decided"] is True
    assert check.record["usage"] == {"input_tokens": 900}


async def test_anything_the_fast_check_is_not_sure_about_goes_to_the_full_check() -> None:
    for unsure in ({"send_message": 0.6}, {"consent": 0.3}, {"payment": 0.12}):
        model = OneAnswer()
        check = await fast_assess(model, FakeDecisions(CLEAR | unsure))
        assert len(model.requests) == 1, unsure  # the full check decided
        assert check.record["fast"]["decided"] is False
        assert check.record["fast"]["probabilities"] == CLEAR | unsure


async def test_a_failed_fast_check_falls_back_to_the_full_check() -> None:
    model = OneAnswer()
    check = await fast_assess(model, FakeDecisions(CLEAR, fail=True))
    assert len(model.requests) == 1
    assert "http_503" in check.record["fast"]["error"]


async def test_the_fast_check_runs_only_when_chosen_and_for_a_model_it_serves() -> None:
    for kwargs in ({"fast": False}, {"name": "deepseek-v4.1-flash"}):
        model, decisions = OneAnswer(), FakeDecisions(CLEAR)
        await fast_assess(model, decisions, **kwargs)
        assert decisions.calls == 0 and len(model.requests) == 1


async def test_the_fast_check_never_sends_to_openai_when_the_tenant_does_not_allow_it() -> None:
    model, decisions = OneAnswer(), FakeDecisions(CLEAR)
    await fast_assess(model, decisions, allowed=("deepinfra",))
    assert decisions.calls == 0
    assert model.requests[0].allowed_providers == ("deepinfra",)  # the router decides the rest
