"""Models: which a task may use, falling back, and reasoning effort."""

from collections.abc import Collection, Sequence

from witx.domain.rules.common import Rejected


def check_model(model: str, catalog: Collection[str]) -> None:
    if model not in catalog:
        raise Rejected("unknown_model", f"{model} is not in the model catalog")


def allowed_providers(chosen: Sequence[str] | None, default_provider: str) -> frozenset[str]:
    """The providers a tenant's screenshots and text may go to: the
    owner's choice, or, until an owner chooses, only the default model's provider."""
    return frozenset(chosen) if chosen is not None else frozenset({default_provider})


def check_provider(provider: str, allowed: Collection[str]) -> None:
    """A model is used only if the tenant allows its provider: no task, switch, fallback,
    risk check or side chat sends anything to another one."""
    if provider not in allowed:
        raise Rejected(
            "provider_not_allowed", f"an owner has not allowed {provider} for this organisation"
        )


def fallback_model(
    *,
    current: str,
    failed: Collection[str],
    catalog: Sequence[tuple[str, str]],
    allowed: Collection[str],
) -> str | None:
    """The model a task falls back to when its model keeps failing: the first
    in the catalog's order, other than the current one and the ones that already failed
    since the run's last successful step, whose provider the tenant allows. None when
    there is none: the run then waits for a person. ``catalog`` is (name, provider)."""
    for name, provider in catalog:
        if name != current and name not in failed and provider in allowed:
            return name
    return None


def check_provider_choice(chosen: Sequence[str], known: Collection[str]) -> list[str]:
    """An owner's choice of providers: at least one, each used by the model catalog."""
    picked = sorted(set(chosen))
    if not picked:
        raise Rejected("invalid_providers", "allow at least one provider")
    unknown = set(picked) - set(known)
    if unknown:
        raise Rejected(
            "invalid_providers", "not in the model catalog: " + ", ".join(sorted(unknown))
        )
    return picked


# How hard the agent may think; never below medium (lower effort misjudged inputs).
# How hard a model thinks, least first. A model starts at LOWEST_EFFORT unless the
# deployment lets it go lower. The deployment's default, which the risk check also
# uses, never goes below it: lower efforts were no faster for the risk check and misjudged
# ordinary inputs.
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")


LOWEST_EFFORT = "medium"


def efforts_from(lowest: str) -> tuple[str, ...]:
    """The efforts of a model whose lowest is ``lowest``, least first."""
    return REASONING_EFFORTS[REASONING_EFFORTS.index(lowest) :]


def check_reasoning_effort(effort: str, lowest: str = LOWEST_EFFORT) -> None:
    allowed = efforts_from(lowest)
    if effort not in allowed:
        raise Rejected("invalid_effort", f"reasoning effort is one of {', '.join(allowed)}")


def effort_on_model(effort: str, lowest: str) -> str:
    """The effort a task keeps when it moves to a model whose lowest is ``lowest``: its
    own, raised to that lowest when it was below it."""
    return effort if effort in efforts_from(lowest) else lowest
