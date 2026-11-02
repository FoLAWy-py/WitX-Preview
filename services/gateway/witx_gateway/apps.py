"""Finding an installed app by name, for when the agent guessed an app ID that is not
installed. The list of installed apps stays on this machine: only the few best matches
go to the cloud."""

import re
from urllib.parse import quote

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
    main app comes before its plug-ins. An app without a display name (Android lists
    none) is named by the last part of its ID: "contacts" names com.google.android.contacts,
    not com.android.contactspicker. Empty when nothing matches."""
    words = _words(query)
    scored = []
    for app_id, name in installed.items():
        text = f"{app_id} {name or ''}".lower()
        score = sum(1 for w in words if w in text)
        named = name.split("·") if name else [app_id.rsplit(".", 1)[-1]]
        if score and {n.strip().lower() for n in named} & words:
            score += 1
        if score:
            scored.append((-score, len(app_id), app_id, name))
    scored.sort()
    return [(app_id, name) for _, _, app_id, name in scored[:LIMIT]]


# The WitX app's ID starts with this; the rest makes it the person's own (their team).
WITX_APP = "com.witx.app."


def witx_app(installed: dict[str, str | None]) -> str | None:
    """The WitX app among the installed apps, if it is there."""
    return next((app_id for app_id in sorted(installed) if app_id.startswith(WITX_APP)), None)


def download_link(url: str, sha256: str) -> str:
    """The deep link that asks the WitX app to download ``url`` and keep it only if its
    SHA-256 is ``sha256`` (apps/ios)."""
    return f"witx://download?url={quote(url, safe='')}&sha256={sha256}"
