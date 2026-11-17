"""Tests for the release-notes baseline choice in .github/generate_releasenotes.py.

The script runs against GitHub at import time, so the two pure helpers are cut
out of its source and executed on their own.
"""

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "generate_releasenotes.py"
HELPERS = ("_version_key", "next_release_version", "last_integration_release")
REPO_NAME = "kayl-codes/homeassistant-truenas"


def _load_helpers() -> dict:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    wanted = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in HELPERS
    ]
    assert [node.name for node in wanted] == list(HELPERS)
    namespace = {"re": re, "REPO_NAME": REPO_NAME}
    exec(  # noqa: S102 - our own repository file, never external input
        compile(ast.Module(body=wanted, type_ignores=[]), str(SCRIPT), "exec"),
        namespace,
    )
    return namespace


class _FakeGithub:
    def __init__(self, tags):
        self._tags = tags

    def get_repo(self, name):
        assert name == REPO_NAME
        return SimpleNamespace(
            get_tags=lambda: [
                SimpleNamespace(name=tag, commit=SimpleNamespace(sha=f"sha-{tag}"))
                for tag in self._tags
            ]
        )


@pytest.fixture(scope="module")
def helpers():
    return _load_helpers()


def test_version_key_drops_rc_suffix_and_pads(helpers):
    key = helpers["_version_key"]
    assert key("v2.13.2-rc2") == (2, 13, 2)
    assert key("v2.13") == key("v2.13.0") == (2, 13, 0)
    assert key("2.9.10") == (2, 9, 10)


TAGS = [
    "v2.13.2-rc2",
    "v2.13.2-rc1",
    "v2.13.2",
    "v2.13.1",
    "v2.13.1-rc1",
    "v2.13.0",
    "v2.12.9",
    "v2.13.10",
    "v2.13.11",
    "v2.9.0",
]


@pytest.mark.parametrize("tags", [TAGS, TAGS[::-1]], ids=["listed", "reversed"])
@pytest.mark.parametrize(
    ("current", "expected"),
    [
        # An rc is compared with the stable tags only.
        ("v2.13.2-rc2", "v2.13.1"),
        ("v2.13.2", "v2.13.1"),
        # Two-digit patch numbers sort numerically, not as text.
        ("v2.13.11", "v2.13.10"),
        # Publishing an older release must not pick a newer tag as baseline.
        ("v2.13.0", "v2.12.9"),
        # No release given (issue creation): the newest stable tag, never an rc.
        (None, "v2.13.11"),
    ],
)
def test_baseline_is_newest_older_stable_tag(helpers, tags, current, expected):
    result = helpers["last_integration_release"](_FakeGithub(tags), current)
    assert result == {"tag_name": expected, "tag_sha": f"sha-{expected}"}


@pytest.mark.parametrize("current", ["v2.13.2-beta1", "v2.13.2-rc.1", "latest"])
def test_unrecognised_current_tag_is_an_error(helpers, current):
    with pytest.raises(SystemExit, match="Unrecognised release tag"):
        helpers["last_integration_release"](_FakeGithub(TAGS), current)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v2.13.1", "v2.13.2"),
        ("v2.13.9", "v2.13.10"),
        ("v2.13", "v2.13.1"),
        ("2.9.10", "v2.9.11"),
    ],
)
def test_next_release_version(helpers, tag, expected):
    assert helpers["next_release_version"](tag) == expected


def test_equal_versions_pick_the_same_tag_in_any_order(helpers):
    forward = helpers["last_integration_release"](
        _FakeGithub(["v2.13", "v2.13.0", "v2.14.0"]), "v2.14.0"
    )
    backward = helpers["last_integration_release"](
        _FakeGithub(["v2.14.0", "v2.13.0", "v2.13"]), "v2.14.0"
    )
    assert forward == backward


def test_no_older_stable_tag_returns_no_baseline(helpers):
    result = helpers["last_integration_release"](
        _FakeGithub(["v1.0.0-rc1", "v1.0.0"]), "v1.0.0"
    )
    assert result == {"tag_name": None, "tag_sha": None}
