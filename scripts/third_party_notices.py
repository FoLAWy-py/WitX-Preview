"""Generate the web dependency notices from installed, locked packages.

Run after pnpm install and whenever the dependency lock changes. --check verifies
that the committed notices match the installed runtime dependency graph.
"""

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
FALLBACK = {"rehype-katex": "remark-math.LICENSE", "remark-math": "remark-math.LICENSE"}


def packages() -> dict[Path, dict[str, Any]]:
    found: dict[Path, dict[str, Any]] = {}

    def visit(path: Path) -> None:
        path = path.resolve()
        if path in found:
            return
        manifest = path / "package.json"
        if not manifest.is_file():
            raise RuntimeError(f"Dependency not installed: {path.name}; run pnpm install first")
        meta = json.loads(manifest.read_text())
        found[path] = meta
        for name in meta.get("dependencies", {}):
            for parent in (path, *path.parents):
                dependency = parent / "node_modules" / name
                if (dependency / "package.json").is_file():
                    visit(dependency)
                    break
            else:
                raise RuntimeError(f"Missing dependency {name} of {meta['name']}")

    meta = json.loads((WEB / "package.json").read_text())
    for name in meta["dependencies"]:
        visit(WEB / "node_modules" / name)
    return found


def render() -> str:
    sections = [
        "WitX Web — third-party dependency notices\n\n"
        "The following installed runtime dependencies retain their own copyrights\n"
        "and licenses. This list includes their transitive and type dependencies;\n"
        "not every listed file is necessarily included in the browser bundle.\n"
        "Regenerate with: python scripts/third_party_notices.py\n"
    ]
    for folder, meta in sorted(
        packages().items(), key=lambda item: (item[1]["name"], item[1]["version"])
    ):
        files = sorted(
            p
            for p in folder.iterdir()
            if p.is_file()
            and any(word in p.name.lower() for word in ("license", "licence", "copying", "notice"))
        )
        if not files:
            fallback = FALLBACK.get(meta["name"])
            if fallback is None:
                raise RuntimeError(f"No license text found for {meta['name']}")
            files = [WEB / "licenses" / fallback]
        heading = (
            f"{meta['name']} {meta['version']}\nLicense: {meta.get('license', 'see text below')}"
        )
        texts = [f"--- {p.name} ---\n{p.read_text().strip()}" for p in files]
        sections.append("=" * 72 + "\n" + heading + "\n\n" + "\n\n".join(texts) + "\n")
    return "\n".join(sections)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = WEB / "public" / "THIRD_PARTY_NOTICES.txt"
    expected = render()
    if args.check:
        if not output.is_file() or output.read_text() != expected:
            raise SystemExit(
                "Third-party notices are stale; run python scripts/third_party_notices.py"
            )
    else:
        output.write_text(expected)


if __name__ == "__main__":
    main()
