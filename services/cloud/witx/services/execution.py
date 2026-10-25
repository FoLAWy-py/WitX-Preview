"""Observations and device actions: admission, delivery, results, approval, reconciliation.

All functions run inside the caller's transaction.
"""

from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.config import Limits
from witx.domain import rules
from witx.domain.model import (
    Action,
    ActionStatus,
    Effect,
    Grant,
    Observation,
    ObservationStatus,
    PermissionMode,
    Policy,
    RiskVerdict,
    Run,
    Task,
    ToolCall,
    WaitReason,
)
from witx.domain.rules import Rejected
from witx.store import devices, execution, purchases, runs, tasks

CALL_WAITS = (WaitReason.OBSERVATION, WaitReason.ACTION, WaitReason.APPROVAL, WaitReason.CHILDREN)

# What the agent is told when find_app finds nothing: the run goes on, and the
# phone's own search is the next place to look. Tapping a result is an ordinary input,
# judged by the policy like any other.
NOT_FOUND = (
    "No installed app's name or ID matches, but the app may be there under another name. "
    "Look for it with the phone's own search: on an iPhone go to the home screen "
    "(press_home) and swipe down from the middle of the screen to open Search; on Android "
    "use the search box of the home screen or the app list. Type the app's name as the "
    "user said it, then its other common names (its English or local name). Open only a "
    "result that is an app already installed on this phone: never an App Store or Play "
    "Store result, a web suggestion, or a Get / Install button, and never install an app "
    "for the user. If the phone's search does not show it either, the app is not on this "
    "phone: call ask_user to say so, with options (use another phone, or end the "
    "task); do not open a different app that only looks similar. Searching the installed "
    "apps again will not find it."
)


async def resolve(conn: AsyncConnection, call: ToolCall, output: dict[str, Any]) -> None:
    """Stores a tool result and lets the run continue if it was waiting for it."""
    if await runs.resolve_call(conn, call.id, output):
        await runs.wake(conn, call.run_id, CALL_WAITS)


# ---------------------------------------------------------------- observations


async def request_observation(
    conn: AsyncConnection, *, task: Task, device_id: UUID, epoch: int | None
) -> Observation:
    obs = await execution.request_observation(
        conn,
        id=uuid4(),
        tenant_id=task.tenant_id,
        device_id=device_id,
        task_id=task.id,
        epoch=epoch,
    )
    await tasks.emit(
        conn, task, "observation.requested", observation_id=obs.id, device_id=device_id
    )
    return obs


def observation_output(obs: Observation) -> dict[str, Any]:
    if obs.status == ObservationStatus.COMPLETE and obs.screen is not None:
        output: dict[str, Any] = {
            "status": "ok",
            "observation_id": str(obs.id),
            "device_id": str(obs.device_id),
            "width": obs.screen.width,
            "height": obs.screen.height,
            "foreground_app": obs.screen.foreground_app,
        }
        if obs.screen.sliders:
            output["sliders"] = obs.screen.sliders
        return output
    return {"status": "failed", "observation_id": str(obs.id), "error": "capture_failed"}


async def record_observation(
    conn: AsyncConnection,
    obs: Observation,
    *,
    captured_at: datetime | None,
    screen: dict[str, Any] | None,
    sha256: str | None,
    object_key: str | None,
    error: str | None,
) -> None:
    """Applies a Gateway's observation result (already verified against the upload)."""
    if error is None and screen is not None and sha256 and object_key and captured_at:
        changed = await execution.complete_observation(
            conn,
            obs.id,
            captured_at=captured_at,
            screen=screen,
            sha256=sha256,
            object_key=object_key,
        )
    else:
        changed = await execution.fail_observation(conn, obs.id, error or "capture_failed")
    if not changed:
        return  # duplicate delivery
    updated = await execution.get_observation(conn, obs.id)
    assert updated is not None
    if updated.task_id is not None:
        await tasks.emit(
            conn,
            updated.task_id,
            "observation.updated",
            observation_id=obs.id,
            status=updated.status.value,
        )
    call = await runs.call_by_ref(conn, "observation_id", obs.id)
    if call is not None:
        await resolve(conn, call, observation_output(updated))
        return
    # Otherwise it may be the screenshot taken right after an action.
    action_id = await execution.action_by_post_observation(conn, obs.id)
    if action_id is not None:
        await resolve_action_call(conn, action_id)
        return
    # Or one taken as a run starts: the run waits for it before its first model call.
    if updated.task_id is not None:
        await runs.wake_task(conn, updated.task_id, (WaitReason.OBSERVATION,))


# ---------------------------------------------------------------- admission


async def admit(
    conn: AsyncConnection,
    *,
    task: Task,
    run: Run,
    call: ToolCall,
    device_id: UUID,
    payload: dict[str, Any],
    observation_id: UUID | None,
    target: str,
    expected_effect: str,
    effects: list[str],
    risk: RiskVerdict | None,
    limits: Limits,
    now: datetime,
    intent: dict[str, str | None] | None = None,
    changes: str | None = None,
) -> Action:
    """Turns a model's input proposal into a queued action, or one awaiting approval.

    Whether it waits is decided by the policy (``rules.decide_policy``) from the task's
    permission mode and three signals: the model's declared effects, the independent
    risk verdict, and deterministic rules. A message or payment must state its terms
    (recipient and text; payee, amount and currency), which the risk check compares with
    the screenshot and the approval is bound to. In a task with several phones, an
    order or payment must be on the phone and terms the person chose
    (``rules.check_purchase``, D33). An input that would wait goes ahead
    when a grant of the task covers it (``rules.grant_covers``): the grant approves it,
    and the approval is bound and used like a person's. Raises Rejected when the
    proposal breaks a rule; nothing is stored in that case.
    """
    device = await devices.get(conn, device_id, lock=True)  # serializes admission per device
    if device is None:
        raise Rejected("unknown_device")
    lease = await devices.lease(conn, device_id)
    if lease is None or lease.task_id != task.id or lease.run_id != run.id or lease.stop_id:
        raise Rejected("no_device_lease", "this run does not control the device")
    observation = await execution.get_observation(conn, observation_id) if observation_id else None
    latest = await execution.latest_complete(conn, device_id)
    state = await execution.device_state(conn, device_id)
    rules.admit_input(
        payload=payload,
        device=device,
        observation=observation,
        latest_observation_id=latest.id if latest else None,
        lease_epoch=lease.epoch,
        last_input_at=state["last_input_at"],
        device_busy=state["busy"],
        unknown_pending=state["unknown"],
        now=now,
        max_age=limits.observation_max_age,
    )
    declared = rules.validate_effects(effects)
    rules.check_select(payload["kind"], declared)
    assert observation is not None and observation.screen is not None  # admit_input checked
    actions = await execution.task_actions(conn, task.id)
    if rules.repeats(
        [*await _recent_inputs(conn, actions, task.id, run.id, device_id), payload]
    ) >= (rules.LOOP_ASK):
        raise Rejected(
            "repeating",
            f"you repeated the same inputs {rules.LOOP_ASK} times without reaching the goal",
        )
    last = next((a for a in reversed(actions) if a.device_id == device_id), None)
    if last is not None and last.post_observation_id == observation.id:
        before = (
            await execution.get_observation(conn, last.observation_id)
            if last.observation_id
            else None
        )
        rules.check_changed(
            payload, last.input, before.sha256 if before else None, observation.sha256
        )
    effects_seen = set(declared) | set(risk.effects if risk else [])
    terms, notes = rules.check_intent(
        effects=effects_seen,
        declared=intent,
        seen=risk.seen if risk is not None and not risk.failed else None,
    )
    intents = await purchases.for_task(conn, task.id)
    purchase_id = rules.check_purchase(
        effects=effects_seen,
        phones=len(task.device_ids),
        intent=intents[-1] if intents else None,
        device_id=device_id,
        terms=terms,
    )
    rank = None
    if payload["kind"] == "launch_app":
        searches = [
            a.found_apps
            for a in reversed(actions)
            if a.kind in ("find_app", "launch_app") and a.device_id == device_id and a.found_apps
        ]
        # By query it opens the best match, rank 0: it goes ahead, as the best match does.
        rank = rules.found_rank(payload["app_id"], searches) if payload["app_id"] else 0
    if task.plan_mode:
        rules.check_only_looking(
            kind=payload["kind"],
            declared_effects=declared,
            risk=risk,
            app_sensitive=observation.screen.foreground_app in device.sensitive_apps,
            text_flags=rules.text_flags(payload),
        )
    rules.check_permission_prompt(mode=task.permission_mode, risk=risk)
    policy = rules.decide_policy(
        mode=task.permission_mode,
        kind=payload["kind"],
        declared_effects=declared,
        risk=risk,
        app_sensitive=observation.screen.foreground_app in device.sensitive_apps,
        text_flags=rules.text_flags(payload),
        found_rank=rank,
    )
    policy = replace(policy, reasons=policy.reasons + notes)
    needs_approval = policy.decision == "ask"
    previous = [a for a in actions if a.device_id == device_id]
    retry = rules.retry_of(previous)
    grant = None
    if needs_approval:
        grant = await _covering_grant(
            conn,
            task=task,
            policy=policy,
            device_id=device_id,
            app=observation.screen.foreground_app,
            terms=terms,
            retry=retry is not None,
            now=now,
        )
    if grant is not None:
        policy = replace(
            policy,
            reasons=[
                *policy.reasons,
                f"approved by a standing approval ({grant.uses + 1} of {grant.max_uses})",
            ],
        )
    action_id = uuid4()
    action = Action(
        id=action_id,
        tenant_id=task.tenant_id,
        task_id=task.id,
        run_id=run.id,
        device_id=device_id,
        tool_call_id=call.id,
        epoch=lease.epoch,
        control_revision=task.control_revision,
        observation_id=observation_id,
        kind=payload["kind"],
        input=payload,
        target=target,
        expected_effect=expected_effect,
        effects=declared,
        digest=rules.action_digest(action_id, device_id, lease.epoch, payload, terms),
        status=ActionStatus.AWAITING_APPROVAL
        if needs_approval and grant is None
        else ActionStatus.QUEUED,
        effect=Effect.NOT_ATTEMPTED,
        reason=None,
        post_observation_id=uuid4(),
        created_at=now,
        policy=policy,
        intent=terms,
        retry_of=retry,
        purchase_intent_id=purchase_id,
        changes=changes,
    )
    await execution.insert_action(conn, action)
    if grant is not None:
        await execution.approve_by_grant(
            conn,
            action_id=action.id,
            tenant_id=task.tenant_id,
            digest=action.digest,
            expires_at=now + limits.approval_ttl,
            grant=grant,
        )
        await execution.use_grant(conn, grant.id)
    elif needs_approval:
        await execution.insert_approval(
            conn,
            action_id=action.id,
            tenant_id=task.tenant_id,
            digest=action.digest,
            expires_at=now + limits.approval_ttl,
        )
    await runs.link_call(conn, call.id, action_id=action.id)
    await tasks.emit(
        conn,
        task,
        "action.proposed",
        action_id=action.id,
        status=action.status.value,
        kind=action.kind,
        effects=action.effects,
        reasons=policy.reasons,
    )
    return action


async def _recent_inputs(
    conn: AsyncConnection,
    actions: list[Action],
    task_id: UUID,
    run_id: UUID,
    device_id: UUID,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    """The run's inputs on a device since the user's last message, oldest first (up to
    ``until``), among ``actions``, the task's: what rules.repeats looks at. Inputs that
    never ran do not count. Only the user's messages this run reads count: not the
    main agent's words, nor an answer to another agent."""
    run = await runs.get(conn, run_id)
    own = None if run is None or run.parent_run_id is None else run_id
    since = max(
        (
            i["created_at"]
            for i in await tasks.inputs(conn, task_id)
            if i["kind"] != "agent" and i["run_id"] == own
        ),
        default=None,
    )
    return [
        a.input
        for a in actions
        if a.run_id == run_id
        and a.device_id == device_id
        and a.status not in (ActionStatus.REJECTED, ActionStatus.DENIED, ActionStatus.CANCELLED)
        and (since is None or a.created_at > since)
        and (until is None or a.created_at <= until)
    ]


async def _covering_grant(
    conn: AsyncConnection,
    *,
    task: Task,
    policy: Policy,
    device_id: UUID,
    app: str,
    terms: dict[str, str] | None,
    retry: bool,
    now: datetime,
) -> Grant | None:
    """The oldest grant of the task that covers this held input, locked so that two
    inputs never count as the same use."""
    for grant in await execution.task_grants(conn, task.id, open_only=True, lock=True):
        if rules.grant_covers(
            grant,
            policy=policy,
            device_id=device_id,
            app=app,
            terms=terms,
            retry=retry,
            control_revision=task.control_revision,
            now=now,
        ):
            return grant
    return None


# ---------------------------------------------------------------- delivery and results


async def take_deliverable(
    conn: AsyncConnection, device_ids: list[UUID], *, limits: Limits, now: datetime
) -> list[tuple[Action, Observation]]:
    """Queued actions that pass the final checks, marked sent. Returned with the
    observation their coordinates refer to. Failing actions are rejected here."""
    ready: list[tuple[Action, Observation]] = []
    for queued in await execution.actions_with_status(conn, device_ids, ActionStatus.QUEUED):
        action = await execution.get_action(conn, queued.id, lock=True)
        if action is None or action.status != ActionStatus.QUEUED:
            continue
        task = await tasks.get(conn, action.task_id)
        lease = await devices.lease(conn, action.device_id)
        needs_approval = rules.needs_approval(action)
        approval = (
            await execution.get_approval(conn, action.id, lock=True) if needs_approval else None
        )
        assert task is not None and action.observation_id is not None
        try:
            if lease is not None and lease.stop_id is not None:
                raise Rejected("stopping")
            rules.check_deliverable(
                action,
                approval=approval,
                lease_epoch=lease.epoch if lease else None,
                control_revision=task.control_revision,
                now=now,
            )
        except Rejected as error:
            await execution.update_action(
                conn, action.id, status=ActionStatus.REJECTED, reason=error.code
            )
            await tasks.emit(conn, task, "action.updated", action_id=action.id, status="rejected")
            await resolve_action_call(conn, action.id)
            continue
        if needs_approval and not await execution.use_approval(conn, action.id):
            continue
        await execution.update_action(conn, action.id, status=ActionStatus.SENT, sent_at=now)
        await execution.request_observation(
            conn,
            id=action.post_observation_id,
            tenant_id=action.tenant_id,
            device_id=action.device_id,
            task_id=action.task_id,
            epoch=action.epoch,
        )
        await execution.mark_observation_sent(conn, action.post_observation_id)
        await tasks.emit(conn, task, "action.updated", action_id=action.id, status="sent")
        observation = await execution.get_observation(conn, action.observation_id)
        assert observation is not None
        ready.append((action, observation))
    return ready


async def record_result(
    conn: AsyncConnection,
    action: Action,
    *,
    outcome: str,
    reason: str | None,
    finished_at: datetime,
    apps: list[dict[str, Any]] | None = None,
    field_text: str | None = None,
    field_name: str | None = None,
    field_box: list[float] | None = None,
) -> None:
    """Applies a Gateway's dispatch outcome. Repeated deliveries are ignored."""
    locked = await execution.get_action(conn, action.id, lock=True)
    if locked is None or locked.status != ActionStatus.SENT:
        return
    effect = rules.result_effect(outcome, locked.kind)
    found: dict[str, Any] = {}
    if outcome == "returned" and locked.kind == "find_app":
        found["found_apps"] = apps or []
    if outcome == "returned" and locked.kind == "launch_app" and apps:  # opened by query
        found["found_apps"] = apps
    if outcome == "returned" and locked.kind in ("type_text", "select_option") and field_text:
        found["field_text"] = field_text
    if locked.kind == "type_text" and (field_name is not None or field_box is not None):
        found["field_info"] = {"name": field_name or "", "box": field_box}
    await execution.update_action(
        conn,
        action.id,
        status=ActionStatus.DONE,
        effect=effect,
        reason=reason,
        finished_at=finished_at,
        **found,
    )
    if effect == Effect.NOT_ATTEMPTED:
        # No input happened, so the Gateway takes no follow-up screenshot.
        await execution.fail_observation(conn, action.post_observation_id, "not_captured")
    await tasks.emit(
        conn, action.task_id, "action.updated", action_id=action.id, status="done", effect=effect
    )
    await resolve_action_call(conn, action.id)


async def resolve_action_call(conn: AsyncConnection, action_id: UUID) -> None:
    """Resolves the tool call behind an action once its outcome is final for the model."""
    action = await execution.get_action(conn, action_id)
    assert action is not None
    call = await runs.call_by_ref(conn, "action_id", action.id)
    if call is None or call.output is not None:
        return
    output: dict[str, Any] = {"action_id": str(action.id), "status": action.status.value}
    if action.status == ActionStatus.DONE:
        post = await execution.get_observation(conn, action.post_observation_id)
        assert post is not None
        if action.effect != Effect.NOT_ATTEMPTED and post.status not in (
            ObservationStatus.COMPLETE,
            ObservationStatus.FAILED,
        ):
            return  # wait for the follow-up screenshot
        output.update(
            effect=action.effect.value,
            note={
                Effect.NOT_ATTEMPTED: "The input was not sent to the device.",
                Effect.UNVERIFIED: "Input was sent. Check the new screenshot for its effect.",
                Effect.UNKNOWN: "Input may or may not have reached the device. No further "
                "input is possible on it until a user reconciles this action.",
            }.get(action.effect, ""),
        )
        if post.status == ObservationStatus.COMPLETE:
            output["observation"] = observation_output(post)
        typed = str(action.input.get("text", ""))
        if (
            action.kind == "type_text"
            and action.field_text is not None
            and not rules.typed_text_shown(typed, action.field_text)
        ):
            output["field_shows"] = action.field_text
            output["note"] = (
                "The field does not hold what you typed: read back, it shows the text in "
                "field_shows. A character was lost or changed. Fix the field (type the text "
                "again with replace true) before you go on; never treat the two as the same "
                "value."
            )
        if action.field_info and action.field_info.get("name") and action.reason != "wrong_field":
            output["typed_into"] = action.field_info["name"]
        if action.kind == "type_text" and action.effect == Effect.UNVERIFIED:
            note = rules.typing_note(
                str(action.input.get("method") or "set"),
                action.reason,
                action.field_text is not None or bool(action.field_info),
            )
            if note:
                output["note"] = note
                if action.field_text is not None:
                    output["field_shows"] = action.field_text
        if action.kind == "select_option":
            if action.field_text:
                output["selected"] = action.field_text
                output["note"] = (
                    "The phone tapped the option it found showing exactly that text. Check "
                    "on the new screenshot that it is the one you meant."
                )
            elif action.reason in ("ambiguous", "not_found"):
                # Any other reason: the phone could not be read before it scrolled, and
                # nothing was touched; the reason says why.
                output["note"] = {
                    "ambiguous": "More than one option shows that text, so nothing was "
                    "tapped. Scroll to the one you mean and tap it yourself.",
                    "not_found": "No option shows exactly that text; the list was scrolled "
                    "through and left where it ended. Look at the new screenshot, and use the "
                    "text the list really shows, or scroll and tap yourself.",
                }[action.reason]
        if action.found_apps is not None and action.kind == "launch_app":
            output["apps"] = action.found_apps
            output["note"] = (
                "The first of these installed apps, the best match, was opened. If it is not "
                "the app the user meant, open the right one with launch_app by its app_id; "
                "in ask mode the user confirms it."
            )
        elif action.found_apps is not None:
            output["apps"] = action.found_apps
            output["note"] = (
                "These installed apps match, best first. Open the one the user meant with "
                "launch_app, whichever place it has; in ask mode the user confirms it."
                if action.found_apps
                else NOT_FOUND
            )
    if action.reason == "app_not_found":  # launch_app by query: nothing was opened
        output["note"] = NOT_FOUND
    if action.reason == "wrong_field":
        output["focused_field"] = action.field_info or {}
        output["note"] = (
            "Nothing was typed: the focused field is not where you pointed (focused_field "
            "is the one that has the focus, its box in screenshot pixels). Tap the field you "
            "mean, check on the new screenshot that it is focused, then type again with a "
            "point inside it."
        )
    if action.reason:
        output["reason"] = action.reason
    if action.status == ActionStatus.DONE:
        repeated = rules.repeats(
            await _recent_inputs(
                conn,
                await execution.task_actions(conn, action.task_id),
                action.task_id,
                action.run_id,
                action.device_id,
                action.created_at,
            )
        )
        if repeated >= rules.LOOP_WARN:
            output["warning"] = (
                f"You have now repeated the same inputs {repeated} times without reaching "
                "the goal. Stop and look at the screen again: try a different way, or ask "
                f"the user. After {rules.LOOP_ASK} times the task stops for the user."
            )
    await resolve(conn, call, output)


def needs_web_approval(action: Action) -> bool:
    """Payments are approved on the Web app only (see rules.check_approval_channel)."""
    if action.policy is None:
        return True  # no recorded signals: treat it as the strictest case
    return rules.is_payment(action.policy.declared_effects, action.policy.risk)


async def decide(
    conn: AsyncConnection,
    *,
    action_id: UUID,
    decision: str,
    user_id: UUID,
    now: datetime,
    via: str = "web",
    client: str | None = None,
    similar: bool = False,
) -> Action:
    """A person approves or denies one held input. ``via`` says where they decided.
    ``similar`` approves later inputs of the same kind too: a grant (rules.grant_scope),
    given on the Web app only."""
    action = await execution.get_action(conn, action_id, lock=True)
    if action is None:
        raise Rejected("not_found")
    approval = await execution.get_approval(conn, action_id, lock=True)
    if action.status != ActionStatus.AWAITING_APPROVAL or approval is None:
        raise Rejected("not_awaiting_approval")
    if rules.approval_status(approval, now) != "pending":
        raise Rejected("approval_expired")
    if decision == "approve":  # denying is always allowed: it can only make things safer
        rules.check_approval_channel(payment=needs_web_approval(action), via=via)
    if decision == "approve" and similar:
        if via != "web":
            raise Rejected("web_approval_required", "standing approvals are given on the Web app")
        await _grant(conn, action, user_id=user_id, now=now)
    await execution.decide_approval(
        conn, action_id, decision=decision, user_id=user_id, via=via, client=client
    )
    status = ActionStatus.QUEUED if decision == "approve" else ActionStatus.DENIED
    await execution.update_action(conn, action_id, status=status)
    await tasks.emit(conn, action.task_id, "action.updated", action_id=action_id, status=status)
    if status == ActionStatus.DENIED:
        await resolve_action_call(conn, action_id)
    updated = await execution.get_action(conn, action_id)
    assert updated is not None
    return updated


async def _grant(conn: AsyncConnection, action: Action, *, user_id: UUID, now: datetime) -> None:
    effects, terms = rules.grant_scope(action)
    task = await tasks.get(conn, action.task_id)
    observation = (
        await execution.get_observation(conn, action.observation_id)
        if action.observation_id
        else None
    )
    assert task is not None and observation is not None and observation.screen is not None
    grant = Grant(
        id=uuid4(),
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        created_from=action.id,
        device_id=action.device_id,
        app=observation.screen.foreground_app,
        effects=effects,
        terms=terms,
        control_revision=task.control_revision,
        max_uses=rules.GRANT_MAX_USES,
        uses=0,
        created_by=user_id,
        created_at=now,
        expires_at=now + rules.GRANT_TTL,
    )
    await execution.insert_grant(conn, grant)
    await tasks.emit(conn, task, "grant.created", grant_id=grant.id, action_id=action.id)


async def revoke_grant(conn: AsyncConnection, *, grant_id: UUID, user_id: UUID) -> Grant:
    """Ends a grant at once. Inputs it approved that are not yet sent wait for the
    person again. Revoking a revoked grant changes nothing."""
    grant = await execution.get_grant(conn, grant_id, lock=True)
    if grant is None:
        raise Rejected("not_found")
    if await execution.revoke_grant(conn, grant_id, user_id):
        for action_id in await execution.unused_grant_approvals(conn, grant_id):
            action = await execution.get_action(conn, action_id, lock=True)
            if action is None or action.status != ActionStatus.QUEUED:
                continue  # sent meanwhile (the approval is used), or no longer pending
            await execution.reopen_approval(conn, action_id)
            await execution.update_action(conn, action_id, status=ActionStatus.AWAITING_APPROVAL)
            await tasks.emit(
                conn,
                action.task_id,
                "action.updated",
                action_id=action_id,
                status="awaiting_approval",
            )
        await tasks.emit(conn, grant.task_id, "grant.revoked", grant_id=grant_id)
    updated = await execution.get_grant(conn, grant_id)
    assert updated is not None
    return updated


async def revoke_grants(conn: AsyncConnection, task: Task, *, user_id: UUID) -> None:
    """Ends every grant of the task (the person changed what the task is for)."""
    for grant in await execution.task_grants(conn, task.id, open_only=True):
        await revoke_grant(conn, grant_id=grant.id, user_id=user_id)


async def expire_approvals(conn: AsyncConnection, run: Run, now: datetime) -> None:
    for action in await execution.task_actions(conn, run.task_id):
        if action.run_id != run.id or action.status != ActionStatus.AWAITING_APPROVAL:
            continue
        approval = await execution.get_approval(conn, action.id)
        if approval is not None and rules.approval_status(approval, now) == "expired":
            await execution.update_action(
                conn, action.id, status=ActionStatus.CANCELLED, reason="approval_expired"
            )
            await resolve_action_call(conn, action.id)


async def reconcile(
    conn: AsyncConnection, *, action_id: UUID, effect: str, observation_id: UUID, user_id: UUID
) -> Action:
    action = await execution.get_action(conn, action_id, lock=True)
    observation = await execution.get_observation(conn, observation_id)
    if action is None or observation is None:
        raise Rejected("not_found")
    rules.check_reconcile(action, observation)
    await execution.update_action(
        conn,
        action_id,
        effect=Effect(effect),
        reconciled_by=user_id,
        reconciled_with=observation_id,
    )
    await tasks.emit(
        conn, action.task_id, "action.updated", action_id=action_id, effect=effect, reconciled=True
    )
    updated = await execution.get_action(conn, action_id)
    assert updated is not None
    return updated


async def reapply_mode(
    conn: AsyncConnection, task: Task, mode: PermissionMode, *, limits: Limits, now: datetime
) -> None:
    """Re-judges unsent input after the user changed the task's permission mode.

    A stricter mode puts queued input on hold for approval. A looser mode never releases
    input that is already waiting: the user decides it explicitly.
    """
    for action in await execution.task_actions(conn, task.id):
        if action.status != ActionStatus.QUEUED or action.policy is None:
            continue
        locked = await execution.get_action(conn, action.id, lock=True)
        if locked is None or locked.status != ActionStatus.QUEUED or locked.policy is None:
            continue  # the hub sent it meanwhile
        if await execution.get_approval(conn, locked.id) is not None:
            continue  # the user already approved this exact input
        policy = rules.reapply_policy(locked.policy, mode, locked.kind)
        if policy.decision != "ask":
            continue
        await execution.update_action(
            conn, locked.id, status=ActionStatus.AWAITING_APPROVAL, policy=policy
        )
        await execution.insert_approval(
            conn,
            action_id=locked.id,
            tenant_id=locked.tenant_id,
            digest=locked.digest,
            expires_at=now + limits.approval_ttl,
        )
        await tasks.emit(
            conn, task, "action.updated", action_id=locked.id, status="awaiting_approval"
        )


async def cancel_unsent(
    conn: AsyncConnection, task_id: UUID, reason: str, run_id: UUID | None = None
) -> None:
    for action_id in await execution.cancel_unsent(conn, task_id, reason, run_id):
        await resolve_action_call(conn, action_id)
