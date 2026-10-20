"""Skills : know-how for a kind of task, shipped in plugins.

A plugin is a folder of skills and nothing else::

    .witx-plugin/plugin.json      name, version, description
    skills/<name>/SKILL.md        a header of ``key: value`` lines between ``---`` lines,
                                  then Markdown
    README.md, LICENSE            optional

The agent sees each skill as ``plugin:skill`` with its plugin's version and purpose, and
loads the full text with ``load_skill`` when it fits the goal; the call records the
plugin's version and digest. A skill is guidance only: it carries no code, and it grants
no permission: approvals and every other rule apply unchanged.

A skill names the tools and effects it relies on (``tools``, ``effects``), so a plugin
that no longer fits this build is refused instead of misleading the agent. With
``invoke: user`` it is used only when the user names it (``$name``), never on the agent's
own judgement.
"""

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
MAX_DESCRIPTION = 300
MAX_BODY = 20_000
MANIFEST = ".witx-plugin/plugin.json"
INVOKE = ("auto", "user")  # loaded when it fits the goal, or only when the user names it
# Everything a plugin may hold: scripts, hooks, MCP servers and any other file are refused.
_ALLOWED = re.compile(r"^(?:\.witx-plugin/plugin\.json|skills/[^/]+/SKILL\.md|README\.md|LICENSE)$")
# Folders of the repository a plugin lives in, not of the plugin.
_IGNORED = {".git", ".github"}
# A word in backticks that reads like a tool or an effect: the skill must declare it.
_NAMED = re.compile(r"`([a-z][a-z0-9_]*)`")


@dataclass(frozen=True)
class Skill:
    plugin: str
    name: str  # plugin:skill, as the agent loads it
    version: str  # the plugin's
    digest: str  # the plugin's
    description: str
    apps: tuple[str, ...]
    tools: tuple[str, ...]
    effects: tuple[str, ...]
    body: str
    invoke: str = "auto"


@dataclass(frozen=True)
class Plugin:
    name: str
    version: str
    description: str
    digest: str  # SHA-256 of its files: what was installed is what is loaded
    skills: tuple[Skill, ...]


def files(folder: Path) -> list[str]:
    """The plugin's files, relative to its folder, sorted. A link is refused: it could
    point anywhere."""
    found = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if relative.parts[0] in _IGNORED:
            continue
        if path.is_symlink():
            raise ValueError(f"{relative.as_posix()}: a plugin holds no links")
        if path.is_file():
            found.append(relative.as_posix())
    return found


def digest(folder: Path) -> str:
    sha = hashlib.sha256()
    for name in files(folder):
        content = (folder / name).read_bytes()
        sha.update(f"{name}\n{len(content)}\n".encode())
        sha.update(content)
    return sha.hexdigest()


def read(folder: Path) -> Plugin:
    """The plugin in ``folder``, or ValueError saying what is wrong with it."""
    names = files(folder)
    for name in names:
        if not _ALLOWED.match(name):
            raise ValueError(f"{name}: a plugin holds only skills (skills/<name>/SKILL.md)")
    if MANIFEST not in names:
        raise ValueError(f"missing {MANIFEST}")
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{MANIFEST}: not JSON ({e.msg})") from e
    if not isinstance(manifest, dict):
        raise ValueError(f"{MANIFEST}: must be an object")
    name, version, description = (
        str(manifest.get(k, "")) for k in ("name", "version", "description")
    )
    if not NAME.match(name):
        raise ValueError(f"{MANIFEST}: name must be lowercase-with-dashes")
    if not VERSION.match(version):
        raise ValueError(f"{MANIFEST}: version must look like 1.0 or 1.0.2")
    if not 0 < len(description) <= MAX_DESCRIPTION:
        raise ValueError(f"{MANIFEST}: description must be 1 to {MAX_DESCRIPTION} characters")
    sha = digest(folder)
    skills = tuple(
        parse((folder / n).read_text(encoding="utf-8"), n.split("/")[1], name, version, sha)
        for n in names
        if n.startswith("skills/")
    )
    if not skills:
        raise ValueError("a plugin holds at least one skill")
    return Plugin(name, version, description, sha, skills)


def parse(text: str, folder: str, plugin: str, version: str, sha: str) -> Skill:
    """A SKILL.md of ``plugin`` (in ``skills/<folder>/``)."""
    where = f"skills/{folder}/SKILL.md"
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if match is None:
        raise ValueError(f"{where}: missing the --- header")
    header = dict(
        (key.strip(), value.strip())
        for key, _, value in (line.partition(":") for line in match.group(1).splitlines())
        if key.strip()
    )

    def listed(key: str) -> tuple[str, ...]:
        return tuple(a.strip() for a in header.get(key, "").split(",") if a.strip())

    skill = Skill(
        plugin=plugin,
        name=f"{plugin}:{header.get('name', '')}",
        version=version,
        digest=sha,
        description=header.get("description", ""),
        apps=listed("apps"),
        tools=listed("tools"),
        effects=listed("effects"),
        body=match.group(2).strip(),
        invoke=header.get("invoke") or "auto",
    )
    if header.get("name") != folder or not NAME.match(folder):
        raise ValueError(f"{where}: name must be the folder name, in lowercase-with-dashes")
    if not 0 < len(skill.description) <= MAX_DESCRIPTION:
        raise ValueError(f"{where}: description must be 1 to {MAX_DESCRIPTION} characters")
    if skill.invoke not in INVOKE:
        raise ValueError(f"{where}: invoke is auto or user")
    if not 0 < len(skill.body) <= MAX_BODY:
        raise ValueError(f"{where}: the text must be 1 to {MAX_BODY} characters")
    undeclared = sorted(set(_NAMED.findall(skill.body)) - set(skill.tools) - set(skill.effects))
    if undeclared:
        raise ValueError(
            f"{where}: names {', '.join(f'`{n}`' for n in undeclared)} without declaring "
            "it in tools or effects"
        )
    return skill


def render(
    name: str,
    description: str,
    apps: Collection[str],
    body: str,
    tools: Collection[str],
    effects: Collection[str],
    invoke: str = "auto",
) -> str:
    """The SKILL.md of a skill written in WitX: what its text names in backticks is
    declared for it, so a name that is neither a tool nor an effect of this build is
    refused instead."""
    body = body.replace("\r\n", "\n").strip()
    named = list(dict.fromkeys(_NAMED.findall(body)))
    unknown = [n for n in named if n not in tools and n not in effects]
    if unknown:
        raise ValueError(
            f"{', '.join(f'`{n}`' for n in unknown)}: neither a tool nor an effect of this "
            "WitX (write other words without backticks)"
        )
    header = [f"name: {name}", f"description: {description}"]
    for key, values in (
        ("apps", list(apps)),
        ("tools", [n for n in named if n in tools]),
        ("effects", [n for n in named if n in effects]),
    ):
        if values:
            header.append(f"{key}: {', '.join(values)}")
    if invoke != "auto":
        header.append(f"invoke: {invoke}")
    return "---\n" + "\n".join(header) + "\n---\n\n" + body + "\n"


def problems(plugin: Plugin, tools: Collection[str], effects: Collection[str]) -> list[str]:
    """What the plugin's skills rely on that this build does not have."""
    found = []
    for skill in plugin.skills:
        found += [
            f"{skill.name}: no tool `{t}` in this WitX" for t in skill.tools if t not in tools
        ]
        found += [
            f"{skill.name}: no effect `{e}` in this WitX" for e in skill.effects if e not in effects
        ]
    return found


def instructions(skills: Mapping[str, Skill]) -> str:
    """What the agent reads about skills, appended to its instructions: not those used
    only when the user names them."""
    if not skills:
        return ""
    lines = [
        "",
        "Skills: tested know-how for some kinds of task. When one fits the goal, load it "
        "with load_skill before you start, and follow it where it matches what you see on "
        "screen. When the user names one in their message ($name), load that one first. "
        "A skill never overrides the user's instructions, and it grants no permission: "
        "sensitive inputs still wait for approval.",
    ]
    for skill in (s for s in skills.values() if s.invoke == "auto"):
        apps = f" Apps: {', '.join(skill.apps)}." if skill.apps else ""
        lines.append(f"- {skill.name} (v{skill.version}): {skill.description}{apps}")
    return "\n".join(lines)
