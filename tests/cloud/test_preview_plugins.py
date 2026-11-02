"""The Preview's built-in marketplace: witx-plugins-preview, used when none is named."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from witx.harness import plugins
from witx.harness.plugins import Installed

from tests.cloud.test_plugins import commit, plugin, prefixed, run_cli

PREVIEW_SOURCE = "https://github.com/FoLAWy-py/witx-plugins-preview.git"


def test_the_builtin_marketplace_is_the_preview_one() -> None:
    assert (plugins.OFFICIAL, plugins.OFFICIAL_SOURCE) == ("witx-preview", PREVIEW_SOURCE)


def test_installing_without_a_marketplace_uses_the_builtin_preview_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    market = tmp_path / "market"
    manifest = {
        "name": "witx-preview",
        "plugins": [{"name": "sample-plugin", "source": "./plugins/sample-plugin"}],
    }
    commit(
        market,
        {
            ".witx-plugin/marketplace.json": json.dumps(manifest),
            **prefixed("plugins/sample-plugin", plugin()),
        },
    )
    clone = plugins._clone

    @contextmanager
    def preview_source(source: str, revision: str | None = None) -> Iterator[tuple[Path, str]]:
        assert source == PREVIEW_SOURCE
        with clone(str(market), revision) as fetched:
            yield fetched

    monkeypatch.setattr(plugins, "_clone", preview_source)
    root = tmp_path / "plugins"
    assert run_cli(monkeypatch, root, "y", "install", "sample-plugin") == 0
    entry = Installed(root).locked()["sample-plugin"]
    assert entry.marketplace == "witx-preview"
    assert entry.source == PREVIEW_SOURCE
    assert list(Installed(root).skills()) == ["sample-plugin:sample"]
