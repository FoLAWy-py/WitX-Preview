"""What the agent keeps beyond a task: long-term memory (§8.2) and the person's own skills."""

import re
from collections.abc import Collection, Iterable

from witx.domain.rules.common import Rejected
from witx.domain.rules.inputs import has_card_number, text_flags

MEMORY_MAX = 300


MEMORY_KEPT_MAX = 50  # preferences and corrections; each task's first request carries them


MEMORY_APP_MAX = 30  # notes about one app; given when the agent first sees it


MEMORY_APPS_MAX = 300  # notes about all apps together


MEMORY_KINDS = ("user", "feedback", "app")


_APP_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,199}$")


# What memory never holds: secrets and one-time permissions.
_SECRET_WORDS = re.compile(
    r"密码|口令|验证码|校验码|支付密码|(pin|安全|解锁|动态|短信|登录|提取|取件|门禁)码|密钥|"
    r"cvv|cvc|(?<![a-z])pin(?![a-z])|password|passcode|one-time code|otp|"
    r"verification code|security code|door code|unlock code|secret|token|api[ _-]?key",
    re.IGNORECASE,
)


def check_memory(text: str, kept: int = 0, *, app: str | None = None) -> str:
    """An entry for long-term memory: short, and never a password, code or card number.
    ``kept``: the entries of its kind there are already (of that app, for an app note)."""
    cleaned = " ".join(text.split())
    if not 1 <= len(cleaned) <= MEMORY_MAX:
        raise Rejected("invalid_memory", f"1 to {MEMORY_MAX} characters")
    if _SECRET_WORDS.search(cleaned) or "card_number" in text_flags(
        {"kind": "type_text", "text": cleaned}
    ):
        raise Rejected(
            "memory_not_allowed", "passwords, codes and payment details are never remembered"
        )
    limit = MEMORY_APP_MAX if app else MEMORY_KEPT_MAX
    if kept >= limit:
        raise Rejected(
            "memory_full", f"at most {limit} entries; bring one up to date with replaces"
        )
    return cleaned


def check_memory_kind(kind: str, app: str | None) -> str | None:
    """An entry's kind, and the app an "app" note is about: its package or bundle ID, as
    the screenshot names the foreground app. Other kinds name no app."""
    if kind not in MEMORY_KINDS:
        raise Rejected("invalid_memory", "kind is user, feedback or app")
    if kind != "app":
        return None
    name = app.strip() if isinstance(app, str) else ""
    if not _APP_ID.match(name):
        raise Rejected(
            "invalid_memory", "an app note names the app's package, as on the screenshot"
        )
    return name


# A skill may say how to handle a password or a code ("hand the payment password to the
# user"); it never holds one: a secret word followed by a value with a digit in it.
_SECRET_VALUE = re.compile(
    rf"(?:{_SECRET_WORDS.pattern})\s*(?:是|为|[:\uff1a=]|is)\s*(?=\S*\d)[A-Za-z0-9!@#$%^&*._\-]{{4,}}",
    re.IGNORECASE,
)


def check_skill(description: str, apps: Iterable[str], body: str) -> tuple[str, tuple[str, ...]]:
    """A skill the person saves in this WitX: its one-line description and the apps
    it is about (packages or bundle IDs). It holds no password, code or card number."""
    cleaned = " ".join(description.split())
    named = tuple(dict.fromkeys(a.strip() for a in apps if a.strip()))
    for app in named:
        if not _APP_ID.match(app):
            raise Rejected("invalid_skill", f"{app!r} is not an app's package or bundle ID")
    text = f"{cleaned}\n{body}"
    if _SECRET_VALUE.search(text) or has_card_number(text):
        raise Rejected(
            "skill_not_allowed", "a skill never holds passwords, codes or payment details"
        )
    return cleaned, named


SKILL_COMMAND = "/skill"


def skill_command(text: str) -> str | None:
    """The ``/skill …`` command in a message from the user, None for any other
    message: the agent drafts a skill, which only the person saves. In a task, ``/skill``
    alone keeps how this task was done, and what follows it says what to keep in mind;
    as a task's first message, what follows says what the skill is for."""
    head, *rest = text.strip().split(maxsplit=1)
    if head.lower() != SKILL_COMMAND:
        return None
    return rest[0].strip()[:4000] if rest else ""


# ``$name`` or ``$plugin:name`` in a user's message, starting with a letter: not
# part of a Latin word (a$b) and not an amount ($5); right after Chinese text it counts
# ("用$open-wifi").
_NAMED_SKILL = re.compile(
    r"(?<![A-Za-z0-9_$])\$([a-z][a-z0-9]*(?:-[a-z0-9]+)*(?::[a-z0-9]+(?:-[a-z0-9]+)*)?)"
    r"(?![A-Za-z0-9_-])"
)


def named_skills(text: str) -> list[str]:
    """The skills a message from the user names, as written, each once."""
    return list(dict.fromkeys(_NAMED_SKILL.findall(text)))


def named_skill(name: str, skills: Collection[str]) -> str | None:
    """The full name (``plugin:skill``) of a skill named ``name``: itself, or the one skill
    with that name in any plugin; None when there is no such skill, or several."""
    if name in skills:
        return name
    found = [s for s in skills if s.split(":", 1)[-1] == name]
    return found[0] if len(found) == 1 else None
