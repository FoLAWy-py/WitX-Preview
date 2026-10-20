"""System permission prompts: "auto" and "full" allow what a task may need in the
narrowest form and decline tracking; "ask" lets the user answer every prompt."""

import json

import pytest
from witx.domain import rules
from witx.domain.model import PermissionMode, PermissionRequest, RiskVerdict
from witx.domain.rules import Rejected
from witx.harness import risk
from witx.store import execution

ASK, AUTO, FULL = PermissionMode.ASK, PermissionMode.AUTO, PermissionMode.FULL


def prompt(
    category: str, *, grants: bool = True, broad: bool = False, effects: list[str] | None = None
) -> RiskVerdict:
    """What the risk check reports for an input that answers a permission prompt."""
    named = ["share_data"] if effects is None and grants else effects or []
    return RiskVerdict(
        sensitive=bool(named),
        effects=named,
        reason="answers a permission prompt",
        permission=PermissionRequest(category, grants, broad),
    )


def decide(mode: PermissionMode, verdict: RiskVerdict, declared: list[str] | None = None) -> str:
    return rules.decide_policy(
        mode=mode,
        kind="tap",
        declared_effects=declared or [],
        risk=verdict,
        app_sensitive=False,
        text_flags=[],
    ).decision


@pytest.mark.parametrize("mode", [AUTO, FULL])
@pytest.mark.parametrize("category", sorted(rules.GRANTED_PERMISSIONS))
def test_allowing_what_a_task_may_need_runs_without_asking_in_auto_and_full(
    mode: PermissionMode, category: str
) -> None:
    assert decide(mode, prompt(category)) == "allow"
    assert decide(mode, prompt(category), declared=["share_data"]) == "allow"  # the agent's label


@pytest.mark.parametrize("mode", [AUTO, FULL])
def test_declining_a_prompt_runs_without_asking_in_auto_and_full(mode: PermissionMode) -> None:
    assert decide(mode, prompt("tracking", grants=False)) == "allow"
    assert decide(mode, prompt("location", grants=False)) == "allow"


@pytest.mark.parametrize("category", ["location", "tracking"])
@pytest.mark.parametrize("grants", [True, False])
def test_the_user_answers_every_permission_prompt_in_ask_mode_declines_too(
    category: str, grants: bool
) -> None:
    assert decide(ASK, prompt(category, grants=grants)) == "ask"


def test_allowing_more_than_the_narrowest_option_is_held_in_auto() -> None:
    assert decide(AUTO, prompt("photos", broad=True)) == "ask"
    assert decide(AUTO, prompt("other")) == "ask"  # a kind nobody decided on: as before


def test_a_check_that_calls_the_prompt_sensitive_without_naming_an_effect_still_allows_it() -> None:
    unnamed = RiskVerdict(
        sensitive=True,
        effects=[],
        reason="grants location once",
        permission=PermissionRequest("location", True),
    )
    assert decide(AUTO, unnamed) == "allow"
    assert decide(FULL, unnamed) == "allow"


@pytest.mark.parametrize("effect", ["payment", "delete_data", "account_change"])
def test_a_permission_prompt_does_not_hide_another_sensitive_effect(effect: str) -> None:
    verdict = prompt("location", effects=["share_data", effect])
    assert decide(AUTO, verdict) == "ask"
    assert decide(FULL, verdict) == ("ask" if effect == "payment" else "allow")


def test_a_prompt_the_check_could_not_assess_is_not_taken_for_one() -> None:
    failed = RiskVerdict(
        sensitive=True,
        effects=[],
        reason="timeout",
        failed=True,
        permission=PermissionRequest("location", True),
    )
    assert decide(AUTO, failed) == "ask"


@pytest.mark.parametrize("mode", [AUTO, FULL])
def test_allowing_tracking_is_refused_so_the_agent_declines_instead(mode: PermissionMode) -> None:
    with pytest.raises(Rejected) as refused:
        rules.check_permission_prompt(mode=mode, risk=prompt("tracking"))
    assert refused.value.code == "permission_declined"
    rules.check_permission_prompt(mode=mode, risk=prompt("tracking", grants=False))
    rules.check_permission_prompt(mode=mode, risk=prompt("location"))


def test_allowing_tracking_is_the_users_to_decide_in_ask_mode() -> None:
    rules.check_permission_prompt(mode=ASK, risk=prompt("tracking"))


def test_a_permission_prompt_is_read_from_the_risk_check_s_answer() -> None:
    answer = {
        "sensitive": True,
        "effects": ["share_data"],
        "reason": "location",
        "permission": {"category": "location", "grants": True, "broad": False},
        "seen": {},
    }
    call = {"type": "function_call", "name": "report_risk", "arguments": json.dumps(answer)}
    assert risk.parse_verdict([call]).permission == PermissionRequest("location", True, False)
    answer["permission"] = {"category": "dancing", "grants": True, "broad": False}
    assert risk.parse_verdict([{**call, "arguments": json.dumps(answer)}]).permission is None
    answer["permission"] = None  # type: ignore[assignment]
    assert risk.parse_verdict([{**call, "arguments": json.dumps(answer)}]).permission is None


def test_the_permission_prompt_is_kept_with_the_policy() -> None:
    policy = rules.decide_policy(
        mode=AUTO,
        kind="tap",
        declared_effects=[],
        risk=prompt("location"),
        app_sensitive=False,
        text_flags=[],
    )
    stored = execution.policy_json(policy)
    assert execution._policy(stored) == policy


def test_in_plan_mode_answering_a_permission_prompt_is_refused_either_way() -> None:
    for grants in (True, False):
        with pytest.raises(Rejected) as refused:
            rules.check_only_looking(
                kind="tap",
                declared_effects=[],
                risk=prompt("location", grants=grants),
                app_sensitive=False,
                text_flags=[],
            )
        assert refused.value.code == "plan_mode"
