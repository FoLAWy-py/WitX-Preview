"""Finding an installed app by name, for when the agent guessed an app ID that is not
installed. The list of installed apps stays on this machine: only the few best matches
go to the cloud."""

import re

LIMIT = 5
# Words in almost every app ID; matching on them would match everything.
_COMMON = {"com", "org", "net", "android", "apple", "google", "app", "apps", "mobile", "ios"}


def _words(text: str) -> set[str]:
    return {
        w for w in re.split(r"[^0-9a-z一-鿿]+", text.lower()) if len(w) >= 2 and w not in _COMMON
    }


def match(query: str, installed: dict[str, str | None]) -> list[tuple[str, str | None]]:
    """The installed apps (ID to display name) whose ID or name contains a word of the
    query, best first: more matching words first, an app named exactly by one of them
    counting one more ("Notes" before "simNote" for "notes"), then the shorter ID, so a
    main app comes before its plug-ins. Empty when nothing matches."""
    words = _words(query)
    scored = []
    for app_id, name in installed.items():
        text = f"{app_id} {name or ''}".lower()
        score = sum(1 for w in words if w in text)
        if score and {n.strip().lower() for n in (name or "").split("·")} & words:
            score += 1
        if score:
            scored.append((-score, len(app_id), app_id, name))
    scored.sort()
    return [(app_id, name) for _, _, app_id, name in scored[:LIMIT]]
