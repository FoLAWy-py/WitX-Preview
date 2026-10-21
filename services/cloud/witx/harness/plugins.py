"""Installed plugins: where the agent's skills come from.

The plugins folder (``WITX_PLUGINS_DIR``) holds::

    marketplaces.json     the marketplaces added besides the official one: name -> source
    local/                this WitX's own marketplace, a git repository: the plugin ``my``
                          holds the skills the person writes here
    plugins.lock          each installed plugin: where it came from, at which commit, its
                          version and the SHA-256 of its files
    installed/<plugin>/   its files

A marketplace is a git repository with ``.witx-plugin/marketplace.json``, which lists its
plugins: a folder of the same repository (``"source": "./plugins/x"``), or another
repository pinned to a commit (``"source": {"git": url, "commit": sha, "path": ...}``).

Nothing is fetched while tasks run, and nothing changes by itself: a plugin is installed,
updated or removed only when a person does it, and a plugin whose files no longer match
the lock, or that names a tool or effect this build lacks, is not loaded. A skill the
person saves here is committed to ``local`` and installed from it like any other plugin.
"""

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from witx.domain import rules
from witx.domain.model import EFFECT_DEFINITIONS
from witx.harness import skills, tools
from witx.harness.skills import Plugin, Skill

log = logging.getLogger(__name__)

OFFICIAL = "witx-preview"
OFFICIAL_SOURCE = "https://github.com/FoLAWy-py/witx-plugins-preview.git"
MARKETPLACE = ".witx-plugin/marketplace.json"
LOCAL = "local"  # the marketplace in the plugins folder itself
MINE = "my"  # its plugin: the person's own skills, ``my:<name>``
_MINE = f"plugins/{MINE}"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Locked:
    name: str
    marketplace: str
    source: str  # the repository its files came from
    path: str  # its folder in that repository ("" for the root)
    commit: str
    version: str
    sha256: str


@dataclass(frozen=True)
class Version:
    """A saved version of one of the person's skills."""

    commit: str
    saved_at: datetime
    skill: Skill


@dataclass(frozen=True)
class Fetched:
    plugin: Plugin
    folder: Path  # a temporary copy, gone when the fetch ends
    locked: Locked


@dataclass(frozen=True)
class Offer:
    """A plugin a marketplace lists now, as a person reads it before installing it:
    its files' text, and what keeps it from being installed."""

    name: str
    locked: Locked | None  # None when it could not be read
    plugin: Plugin | None
    files: dict[str, str]
    trouble: list[str]


def _tool_names() -> set[str]:
    return {t["name"] for t in tools.TOOLS}


def problems(plugin: Plugin) -> list[str]:
    """What the plugin relies on that this build lacks."""
    return skills.problems(plugin, _tool_names(), EFFECT_DEFINITIONS)


def check(folder: Path) -> list[str]:
    """Every problem of the plugin in ``folder``, or of each plugin of the marketplace in
    ``folder`` (those in other repositories: only how they are listed)."""
    if not (folder / MARKETPLACE).is_file():
        return _check_plugin(folder, None)
    try:
        _, entries = _entries(folder)
    except ValueError as e:
        return [f"{MARKETPLACE}: {e}"]
    found = []
    for plugin, source in entries.items():
        if isinstance(source, str):
            found += _check_plugin(folder / source, plugin)
    return found


def _check_plugin(folder: Path, listed: str | None) -> list[str]:
    """``listed``: the name the marketplace lists it under."""
    try:
        plugin = skills.read(folder)
    except ValueError as e:
        return [f"{listed or folder.name}: {e}"]
    if listed and plugin.name != listed:
        return [f"{listed}: its plugin.json names it {plugin.name}"]
    return problems(plugin)


class Installed:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cached: tuple[tuple[int, int, int], dict[str, Skill]] | None = None

    @property
    def _lock(self) -> Path:
        return self.root / "plugins.lock"

    def locked(self) -> dict[str, Locked]:
        if not self._lock.is_file():
            return {}
        entries = json.loads(self._lock.read_text(encoding="utf-8"))["plugins"]
        return {name: Locked(name=name, **entry) for name, entry in entries.items()}

    def folder(self, name: str) -> Path:
        return self.root / "installed" / name

    def load(self) -> dict[str, Plugin | str]:
        """Each installed plugin, or why it is not loaded."""
        found: dict[str, Plugin | str] = {}
        for name, entry in self.locked().items():
            try:
                plugin = skills.read(self.folder(name))
            except (OSError, ValueError) as e:
                found[name] = str(e)
                continue
            if plugin.digest != entry.sha256:
                found[name] = "its files changed since it was installed: install it again"
            elif plugin.name != name:
                found[name] = f"its plugin.json names it {plugin.name}"
            else:
                trouble = problems(plugin)
                found[name] = "; ".join(trouble) if trouble else plugin
        return found

    def skills(self) -> dict[str, Skill]:
        """The skills the agent may load, by name; read again when the lock changes."""
        try:
            stat = self._lock.stat()
            # Written by replacing the file: a new inode each time, however close in time.
            key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            return {}
        if self._cached is None or self._cached[0] != key:
            found = {}
            for name, plugin in self.load().items():
                if isinstance(plugin, str):
                    log.warning("plugin %s is not loaded: %s", name, plugin)
                    continue
                found.update({s.name: s for s in plugin.skills})
            self._cached = (key, found)
        return self._cached[1]

    # ------------------------------------------------------------------ marketplaces

    def marketplaces(self) -> dict[str, str]:
        path = self.root / "marketplaces.json"
        added = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return {OFFICIAL: OFFICIAL_SOURCE, LOCAL: str(self._local), **added}

    def add_marketplace(self, source: str) -> str:
        """Adds the marketplace at ``source`` (a git URL or a local repository) under the
        name its marketplace.json gives; returns the name."""
        if Path(source).expanduser().exists():
            source = str(Path(source).expanduser().resolve())
        with _clone(source) as (folder, _):
            name, _ = _entries(folder)
        known = self.marketplaces()
        if name in known and known[name] != source:
            raise ValueError(f"a marketplace named {name} is already added from {known[name]}")
        added = {k: v for k, v in known.items() if k not in (OFFICIAL, LOCAL)}
        _write(self.root / "marketplaces.json", {**added, name: source})
        return name

    def remove_marketplace(self, name: str) -> None:
        if name in (OFFICIAL, LOCAL):
            raise ValueError(f"the {name} marketplace cannot be removed")
        added = {k: v for k, v in self.marketplaces().items() if k not in (OFFICIAL, LOCAL)}
        if name not in added:
            raise ValueError(f"no marketplace named {name}")
        users = [p.name for p in self.locked().values() if p.marketplace == name]
        if users:
            raise ValueError(f"plugins from {name} are installed: {', '.join(users)}")
        del added[name]
        _write(self.root / "marketplaces.json", added)

    # ------------------------------------------------------------------ plugins

    @contextmanager
    def fetch(self, name: str, marketplace: str) -> Iterator[Fetched]:
        """The plugin as the marketplace lists it now, in a temporary folder."""
        source = self._source(marketplace)
        with _clone(source) as (market, market_commit):
            _, entries = _entries(market)
            if name not in entries:
                raise ValueError(f"{marketplace} has no plugin named {name}")
            with _entry(name, marketplace, source, market, market_commit, entries[name]) as got:
                yield got

    def offers(self, marketplace: str) -> list[Offer]:
        """Every plugin the marketplace lists now, read from where it lives; nothing is
        installed. Not the person's own marketplace: its skills are written in WitX."""
        if marketplace == LOCAL:
            raise ValueError(f"the {LOCAL} marketplace holds your own skills")
        source = self._source(marketplace)
        found = []
        with _clone(source) as (market, market_commit):
            _, entries = _entries(market)
            for name, entry in entries.items():
                try:
                    with _entry(name, marketplace, source, market, market_commit, entry) as got:
                        texts = {
                            f: (got.folder / f).read_text("utf-8") for f in skills.files(got.folder)
                        }
                        trouble = problems(got.plugin) + _taken(got)
                        found.append(Offer(name, got.locked, got.plugin, texts, trouble))
                except ValueError as e:
                    found.append(Offer(name, None, None, {}, [str(e)]))
        return found

    def install_seen(self, name: str, marketplace: str, commit: str, sha256: str) -> Locked:
        """Installs the plugin a person has read: only if the marketplace still
        offers exactly that, at that commit with that digest, and it fits this build."""
        with self._writing(), self.fetch(name, marketplace) as fetched:
            if (fetched.locked.commit, fetched.locked.sha256) != (commit, sha256):
                raise rules.Rejected(
                    "plugin_changed", f"{name} changed since you read it: read it again"
                )
            trouble = problems(fetched.plugin) + _taken(fetched)
            if trouble:
                raise rules.Rejected("plugin_not_fit", "; ".join(trouble))
            self.install(fetched)
            return fetched.locked

    def uninstall(self, name: str) -> None:
        """Removes a plugin installed from a marketplace; the person's own are deleted
        skill by skill."""
        if name == MINE:
            raise ValueError("your own skills are deleted one by one")
        with self._writing():
            self.remove(name)

    def _source(self, marketplace: str) -> str:
        source = self.marketplaces().get(marketplace)
        if source is None:
            raise ValueError(f"no marketplace named {marketplace}")
        return source

    def install(self, fetched: Fetched) -> None:
        """Puts the fetched plugin in place of any installed one, files first, then the
        lock: a worker reading in between sees files that do not match and skips them."""
        taken = _taken(fetched)
        if taken:
            raise ValueError(taken[0])
        target = self.folder(fetched.plugin.name)
        staging = target.with_name(f".{target.name}.new")
        shutil.rmtree(staging, ignore_errors=True)
        for name in skills.files(fetched.folder):
            (staging / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fetched.folder / name, staging / name)
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        self._write_lock({**self.locked(), fetched.plugin.name: fetched.locked})

    def remove(self, name: str) -> None:
        entries = self.locked()
        if name not in entries:
            raise ValueError(f"no plugin named {name} is installed")
        del entries[name]
        self._write_lock(entries)
        shutil.rmtree(self.folder(name), ignore_errors=True)

    def _write_lock(self, entries: dict[str, Locked]) -> None:
        plugins = {}
        for name, entry in sorted(entries.items()):
            fields = asdict(entry)
            del fields["name"]
            plugins[name] = fields
        _write(self._lock, {"plugins": plugins})

    # ------------------------------------------------------------------ the person's skills

    @property
    def _local(self) -> Path:
        return self.root / LOCAL

    def save(
        self, name: str, description: str, apps: list[str], body: str, invoke: str = "auto"
    ) -> Skill:
        """Saves the person's skill ``my:<name>``, new or changed, and installs it: each
        save is a commit of ``local`` and a new version of ``my``. Raises Rejected or
        ValueError, changing nothing, when the skill is not fit to load."""
        text, skill = draft(name, description, apps, body, invoke)
        with self._writing():
            folder = self._repository() / _MINE
            version = _next_version(folder)
            path = folder / "skills" / name / "SKILL.md"
            if not (path.is_file() and path.read_text("utf-8") == text and MINE in self.locked()):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, "utf-8")
                self._commit(folder, version, f"{name}: saved")
            mine = skills.read(self.folder(MINE))
        return next(s for s in mine.skills if s.name == skill.name)

    def delete(self, name: str, purge: bool = False) -> None:
        """Deletes the person's skill ``my:<name>``. Its versions stay in ``local``, unless
        ``purge``: then they go too, and one deleted before can be purged."""
        with self._writing():
            folder = self._repository() / _MINE
            present = skills.NAME.match(name) and (folder / "skills" / name).is_dir()
            if not present and not (purge and skills.NAME.match(name) and self._kept(name)):
                raise ValueError(f"no skill named {MINE}:{name}")
            if present:
                shutil.rmtree(folder / "skills" / name)
                if any((folder / "skills").iterdir()):
                    self._commit(folder, _next_version(folder), f"{name}: deleted")
                else:
                    _git("add", "-A", cwd=self._local)
                    _git_commit(self._local, f"{name}: deleted")
                    if MINE in self.locked():
                        self.remove(MINE)
            if purge:
                self._purge(name)

    def _kept(self, name: str) -> bool:
        """Whether a commit of ``local`` holds ``my:<name>``."""
        where = f"{_MINE}/skills/{name}"
        return bool(_git("log", "--all", "-1", "--format=%H", "--", where, cwd=self._local))

    def _purge(self, name: str) -> None:
        """Rewrites ``local`` without ``my:<name>``: its files leave every commit and its
        name every commit message, and git drops what it kept of the old commits. ``my`` is
        installed again from the rewritten commit, with the same files."""
        where = f"{_MINE}/skills/{name}"
        _git(
            "filter-branch",
            "--force",
            "--prune-empty",
            "--index-filter",
            f"git rm -r --cached --quiet --ignore-unmatch -- {where}",
            "--msg-filter",
            f"sed 's/^{name}: /a deleted skill: /'",
            "--",
            "--all",
            cwd=self._local,
        )
        for ref in _git(
            "for-each-ref", "--format=%(refname)", "refs/original/", cwd=self._local
        ).split():
            _git("update-ref", "-d", ref, cwd=self._local)
        _git("reflog", "expire", "--expire=now", "--all", cwd=self._local)
        _git("gc", "--prune=now", "--quiet", cwd=self._local)
        if MINE in self.locked():
            with self.fetch(MINE, LOCAL) as fetched:
                self.install(fetched)

    def history(self, name: str, limit: int = 20) -> list[Version]:
        """The saved versions of ``my:<name>``, newest first."""
        if not skills.NAME.match(name) or not (self._local / ".git").is_dir():
            return []
        where = f"{_MINE}/skills/{name}/SKILL.md"
        log = _git("log", f"-{limit}", "--format=%H %cI", "--", where, cwd=self._local)
        found = []
        for line in log.splitlines():
            commit, saved_at = line.split(" ", 1)
            try:
                text = _git("show", f"{commit}:{where}", cwd=self._local)
            except ValueError:
                continue  # the commit that deleted it
            meta = json.loads(_git("show", f"{commit}:{_MINE}/{skills.MANIFEST}", cwd=self._local))
            skill = skills.parse(text, name, MINE, str(meta["version"]), "")
            found.append(Version(commit, datetime.fromisoformat(saved_at), skill))
        return found

    def _repository(self) -> Path:
        """``local``, made the first time a skill is saved."""
        if not (self._local / ".git").is_dir():
            self._local.mkdir(parents=True, exist_ok=True)
            manifest = {
                "name": LOCAL,
                "description": "The skills written in this WitX.",
                "plugins": [{"name": MINE, "source": f"./{_MINE}"}],
            }
            _write(self._local / MARKETPLACE, manifest)
            _git("init", "--quiet", "--initial-branch", "main", cwd=self._local)
        return self._local

    def _commit(self, folder: Path, version: str, message: str) -> None:
        """Commits ``my`` at ``version`` and installs that commit."""
        manifest = {"name": MINE, "version": version, "description": "My own skills."}
        _write(folder / skills.MANIFEST, manifest)
        _git("add", "-A", cwd=self._local)
        _git_commit(self._local, message)
        with self.fetch(MINE, LOCAL) as fetched:
            self.install(fetched)

    @contextmanager
    def _writing(self) -> Iterator[None]:
        """One writer at a time: the API and the command line both save."""
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".writing", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


def draft(
    name: str, description: str, apps: list[str], body: str, invoke: str = "auto"
) -> tuple[str, Skill]:
    """The SKILL.md of the person's skill ``my:<name>``, and the skill it is, checked as
    saving checks it; nothing is written. Raises Rejected or ValueError."""
    description, named = rules.check_skill(description, apps, body)
    if not skills.NAME.match(name):
        raise ValueError(
            "a skill's name is lowercase letters (a-z), digits and dashes, e.g. check-ios-version"
        )
    text = skills.render(name, description, named, body, _tool_names(), EFFECT_DEFINITIONS, invoke)
    skill = skills.parse(text, name, MINE, "0", "")
    trouble = problems(Plugin(MINE, "0", "", "", (skill,)))
    if trouble:
        raise ValueError("; ".join(trouble))
    return text, skill


def _next_version(folder: Path) -> str:
    """``my``'s version after one more change: 0.1.0 first, then the last part goes up."""
    path = folder / skills.MANIFEST
    if not path.is_file():
        return "0.1.0"
    parts = str(json.loads(path.read_text("utf-8"))["version"]).split(".")
    if len(parts) == 2:
        parts.append("0")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _git_commit(repo: Path, message: str) -> None:
    author = ("-c", "user.name=WitX", "-c", "user.email=witx@localhost")
    _git(*author, "commit", "--quiet", "--allow-empty", "-m", message, cwd=repo)


@contextmanager
def _entry(
    name: str,
    marketplace: str,
    source: str,
    market: Path,
    market_commit: str,
    entry: str | dict[str, str],
) -> Iterator[Fetched]:
    """A plugin a marketplace lists: a folder of the marketplace, or in a repository of
    its own at the commit the marketplace pins."""
    if isinstance(entry, str):
        yield _fetched(name, marketplace, source, market, entry, market_commit)
    else:
        with _clone(entry["git"], entry["commit"]) as (repo, commit):
            yield _fetched(name, marketplace, entry["git"], repo, entry["path"], commit)


def _taken(fetched: Fetched) -> list[str]:
    """`my` is the person's own skills: no plugin from elsewhere takes its place."""
    if fetched.plugin.name == MINE and fetched.locked.marketplace != LOCAL:
        return [f"{MINE} is the name of your own skills: a plugin from elsewhere cannot take it"]
    return []


def _fetched(
    name: str, marketplace: str, source: str, repo: Path, path: str, commit: str
) -> Fetched:
    folder = (repo / path).resolve()
    if not folder.is_relative_to(repo.resolve()):
        raise ValueError(f"{name}: its folder is outside its repository")
    plugin = skills.read(folder)
    if plugin.name != name:
        raise ValueError(f"{name}: its plugin.json names it {plugin.name}")
    return Fetched(
        plugin,
        folder,
        Locked(
            name=name,
            marketplace=marketplace,
            source=source,
            path=path.removeprefix("./").strip("/"),
            commit=commit,
            version=plugin.version,
            sha256=plugin.digest,
        ),
    )


def _entries(folder: Path) -> tuple[str, dict[str, str | dict[str, str]]]:
    """A marketplace's name and its plugins: a folder of the marketplace's repository,
    or ``{"git", "commit", "path"}`` for one in another repository."""
    path = folder / MARKETPLACE
    if not path.is_file():
        raise ValueError(f"not a marketplace: no {MARKETPLACE}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    name = str(manifest.get("name", ""))
    if not skills.NAME.match(name):
        raise ValueError("its name must be lowercase-with-dashes")
    entries: dict[str, str | dict[str, str]] = {}
    for item in manifest.get("plugins", []):
        plugin, source = str(item.get("name", "")), item.get("source")
        if not skills.NAME.match(plugin) or plugin in entries:
            raise ValueError(f"plugin {plugin!r}: names are lowercase-with-dashes, each once")
        if isinstance(source, str) and source.startswith("./"):
            entries[plugin] = source
        elif (
            isinstance(source, dict)
            and isinstance(source.get("git"), str)
            and _COMMIT.match(str(source.get("commit", "")))
        ):
            entries[plugin] = {
                "git": source["git"],
                "commit": source["commit"],
                "path": str(source.get("path", "")),
            }
        else:
            raise ValueError(
                f"plugin {plugin}: source is a folder (./...) or "
                '{"git": url, "commit": a full commit SHA}'
            )
    return name, entries


@contextmanager
def _clone(source: str, commit: str | None = None) -> Iterator[tuple[Path, str]]:
    """A checkout of ``source`` at ``commit`` (the default branch without one), and the
    commit it is at."""
    with tempfile.TemporaryDirectory(prefix="witx-plugin-") as tmp:
        folder = Path(tmp) / "repo"
        depth = [] if commit else ["--depth", "1"]
        _git("clone", "--quiet", *depth, "--", source, str(folder))
        if commit:
            _git("checkout", "--quiet", "--detach", commit, cwd=folder)
        yield folder, _git("rev-parse", "HEAD", cwd=folder).strip()


def _git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "FILTER_BRANCH_SQUELCH_WARNING": "1"},
    )
    if done.returncode != 0:
        raise ValueError(f"git {args[0]}: {done.stderr.strip() or 'failed'}")
    return done.stdout


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    temporary.replace(path)
