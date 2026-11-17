"""Tests for the release-notes generation in .github/generate_releasenotes.py.

The script talks to GitHub and reads sys.argv at import time. The pure helpers
and their constants are cut out of its source and run on their own; the two
call sites are run as a whole with a stub ``github`` module.
"""

import ast
import re
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "generate_releasenotes.py"
HELPERS = (
    "_version_key",
    "next_release_version",
    "last_integration_release",
    "get_integration_commits",
)
CONSTANTS = ("REPO_NAME", "NOCHANGE", "CHANGE", "_SKIP_MARKERS")
# The GitHub compare endpoint lists at most this many commits.
COMPARE_PAGE = 250
REPO_NAME = "kayl-codes/homeassistant-truenas"


def _load_helpers() -> dict:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in HELPERS:
            wanted.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in CONSTANTS
            for target in node.targets
        ):
            wanted.append(node)
    namespace = {"re": re}
    exec(  # noqa: S102 - our own repository file, never external input
        compile(ast.Module(body=wanted, type_ignores=[]), str(SCRIPT), "exec"),
        namespace,
    )
    return namespace


class _FakeRepo:
    def __init__(self, tags, shas, commits, default_branch, total=None):
        self._tags = tags
        self._shas = shas
        self._commits = commits
        self._total = total
        self.default_branch = default_branch
        self.compared = None
        self.issues = []
        self.release = None
        self.release_ref = None
        self.tag_reads = 0

    def get_tags(self):
        self.tag_reads += 1
        return [
            SimpleNamespace(
                name=tag,
                commit=SimpleNamespace(sha=self._shas.get(tag, f"sha-{tag}")),
            )
            for tag in self._tags
        ]

    def compare(self, base, head):
        self.compared = (base, head)
        # An iterator without __len__, like PyGithub's PaginatedList.
        total = len(self._commits) if self._total is None else self._total
        return SimpleNamespace(commits=iter(self._commits), total_commits=total)

    def get_release(self, tag):
        self.release_ref = tag
        return self.release

    def get_commit(self, ref):
        return SimpleNamespace(sha=f"sha-{ref}")

    def create_issue(self, **kwargs):
        self.issues.append(kwargs)


class _FakeGithub:
    def __init__(
        self, tags, shas=None, commits=(), default_branch="master", total=None
    ):
        self.repo = _FakeRepo(tags, shas or {}, list(commits), default_branch, total)

    def get_repo(self, name):
        assert name == REPO_NAME
        return self.repo


def _commit(sha, message, login="someone"):
    author = None if login is None else SimpleNamespace(login=login)
    return SimpleNamespace(
        sha=sha,
        html_url=f"https://example.invalid/commit/{sha}",
        commit=SimpleNamespace(message=message),
        author=author,
    )


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
BASE_131 = {"tag_name": "v2.13.1", "tag_sha": "sha-v2.13.1"}


@pytest.fixture(scope="module")
def helpers():
    return _load_helpers()


def test_version_key_drops_rc_suffix_and_pads(helpers):
    key = helpers["_version_key"]
    assert key("v2.13.2-rc2") == (2, 13, 2)
    assert key("v2.13") == key("v2.13.0") == (2, 13, 0)
    assert key("2.9.10") == (2, 9, 10)


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
    github = _FakeGithub(TAGS)
    with pytest.raises(SystemExit, match="Unrecognised release tag"):
        helpers["last_integration_release"](github, current)


def test_equal_versions_on_one_commit_pick_the_same_tag_in_any_order(helpers):
    shas = {"v2.13": "same", "v2.13.0": "same"}
    forward = helpers["last_integration_release"](
        _FakeGithub(["v2.13", "v2.13.0", "v2.14.0"], shas), "v2.14.0"
    )
    backward = helpers["last_integration_release"](
        _FakeGithub(["v2.14.0", "v2.13.0", "v2.13"], shas), "v2.14.0"
    )
    assert forward == backward == {"tag_name": "v2.13.0", "tag_sha": "same"}


def test_equal_versions_on_different_commits_are_an_error(helpers):
    github = _FakeGithub(["v2.13", "v2.13.0"])
    with pytest.raises(SystemExit, match="point to different commits"):
        helpers["last_integration_release"](github, "v2.14.0")


def test_equal_versions_below_the_newest_group_are_not_checked(helpers):
    # v2.12 and v2.12.0 differ, but they are not the baseline group.
    github = _FakeGithub(
        ["v2.12", "v2.12.0", "v2.13.0"], shas={"v2.12": "a", "v2.12.0": "b"}
    )
    result = helpers["last_integration_release"](github, "v2.14.0")
    assert result == {"tag_name": "v2.13.0", "tag_sha": "sha-v2.13.0"}


def test_rc_and_stable_of_one_version_are_not_compared(helpers):
    # An rc is never a candidate, so its own commit cannot make the baseline ambiguous.
    github = _FakeGithub(
        ["v2.13.2-rc1", "v2.13.2", "v2.13.1"], shas={"v2.13.2-rc1": "rc-sha"}
    )
    result = helpers["last_integration_release"](github, "v2.13.2")
    assert result == {"tag_name": "v2.13.1", "tag_sha": "sha-v2.13.1"}


def test_no_older_stable_tag_returns_no_baseline(helpers):
    result = helpers["last_integration_release"](
        _FakeGithub(["v1.0.0-rc1", "v1.0.0"]), "v1.0.0"
    )
    assert result == {"tag_name": None, "tag_sha": None}


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


def test_changes_are_compared_up_to_the_release_tag(helpers):
    github = _FakeGithub(TAGS, commits=[_commit("a1", "feat: x")])
    helpers["get_integration_commits"](github, BASE_131, "v2.13.2-rc2")
    # Commits merged after the release must not reach its notes.
    assert github.repo.compared == ("sha-v2.13.1", "sha-v2.13.2-rc2")


def test_changes_default_to_the_default_branch_without_a_release(helpers):
    github = _FakeGithub(
        TAGS, commits=[_commit("a1", "feat: x")], default_branch="trunk-x"
    )
    base = {"tag_name": "v2.13.2", "tag_sha": "sha-v2.13.2"}
    helpers["get_integration_commits"](github, base)
    assert github.repo.compared == ("sha-v2.13.2", "sha-trunk-x")


def test_no_commits_in_range_is_no_change(helpers):
    github = _FakeGithub(TAGS, commits=[])
    result = helpers["get_integration_commits"](github, BASE_131, "v2.13.2")
    assert result == helpers["NOCHANGE"]


def test_commit_lines_are_rendered_when_commits_exist(helpers):
    # Guards the list() around the lazy iterator: without it the empty check
    # above would never see an empty range.
    github = _FakeGithub(TAGS, commits=[_commit("a1", "feat: x")])
    result = helpers["get_integration_commits"](github, BASE_131, "v2.13.2")
    assert result == "- [feat: x](https://example.invalid/commit/a1) @someone\n"


@pytest.mark.parametrize("marker", ["flake", " workflow", " test", "docs"])
def test_skip_marker_in_subject_excludes_the_commit(helpers, marker):
    github = _FakeGithub(
        TAGS,
        commits=[
            _commit("skip", f"feat: x {marker.strip()} y"),
            _commit("keep", "feat: kept"),
        ],
    )
    result = helpers["get_integration_commits"](github, BASE_131, "v2.13.2")
    assert "feat: kept" in result
    assert "skip" not in result


@pytest.mark.parametrize(
    "marker", ["Merge branch ", "Merge pull request ", "dev debug"]
)
def test_remaining_skip_markers_exclude_the_commit(helpers, marker):
    github = _FakeGithub(
        TAGS, commits=[_commit("skip", f"{marker}feature"), _commit("keep", "feat: k")]
    )
    result = helpers["get_integration_commits"](github, BASE_131, "v2.13.2")
    assert "feat: k" in result
    assert marker.strip() not in result


def test_only_the_first_line_is_rendered(helpers):
    github = _FakeGithub(
        TAGS, commits=[_commit("a1", "feat: keep this\n\nlong body", "alice")]
    )
    result = helpers["get_integration_commits"](github, BASE_131, "v2.13.2")
    assert result == ("- [feat: keep this](https://example.invalid/commit/a1) @alice\n")


def test_all_commits_skipped_is_no_change(helpers):
    # Otherwise the issue path would open a release issue with an empty list.
    github = _FakeGithub(
        TAGS, commits=[_commit("d1", "docs: only"), _commit("t1", "fix: test x")]
    )
    result = helpers["get_integration_commits"](github, BASE_131, "v2.13.2")
    assert result == helpers["NOCHANGE"]


def test_commit_without_author_is_rendered_as_unknown(helpers):
    github = _FakeGithub(TAGS, commits=[_commit("a1", "fix: anonymous", None)])
    result = helpers["get_integration_commits"](github, BASE_131, "v2.13.2")
    assert result.endswith("@Unknown\n")


def test_missing_baseline_is_an_error_not_no_change(helpers):
    github = _FakeGithub(TAGS, commits=[_commit("a1", "feat: x")])
    base = {"tag_name": None, "tag_sha": None}
    with pytest.raises(
        SystemExit, match="No older stable release tag found before HEAD"
    ):
        helpers["get_integration_commits"](github, base)
    assert github.repo.compared is None


# --- the two call sites, run as the script -------------------------------------


def _run_script(monkeypatch, github, update, ref="x"):
    # Positions the script reads: [2] token, [4] UPDATERELEASE, [6] tag ref.
    stub = SimpleNamespace(Github=lambda token: github)
    monkeypatch.setitem(sys.modules, "github", stub)
    argv = ["generate_releasenotes.py", "x", "token", "x", update, "x", ref]
    monkeypatch.setattr(sys, "argv", argv)
    runpy.run_path(str(SCRIPT), run_name="__main__")


class _FakeRelease:
    def __init__(self, body):
        self.body = body
        self.draft = False
        self.prerelease = True
        self.updated = None

    def update_release(self, **kwargs):
        self.updated = kwargs


def _release(body):
    return _FakeRelease(body)


def test_publish_with_curated_notes_never_reads_the_baseline(monkeypatch):
    github = _FakeGithub(TAGS, commits=[_commit("a1", "feat: x")])
    github.repo.release = _release("## Curated\n\nText.")
    _run_script(monkeypatch, github, "yes", "refs/tags/v2.13.2-rc2")
    assert github.repo.release_ref == "v2.13.2-rc2"
    assert github.repo.compared is None
    assert github.repo.tag_reads == 0
    updated = github.repo.release.updated["message"]
    assert updated.startswith("[![Downloads")
    # The curated body follows the badge unchanged: same heading, text and spacing.
    assert updated.endswith("\n\n## Curated\n\nText.")


def test_publish_with_empty_body_compares_up_to_the_release_tag(monkeypatch):
    github = _FakeGithub(TAGS, commits=[_commit("a1", "feat: x")])
    github.repo.release = _release("")
    _run_script(monkeypatch, github, "yes", "refs/tags/v2.13.2-rc2")
    assert github.repo.release_ref == "v2.13.2-rc2"
    assert github.repo.compared == ("sha-v2.13.1", "sha-v2.13.2-rc2")
    assert github.repo.tag_reads == 1
    assert "feat: x" in github.repo.release.updated["message"]


def test_issue_path_title_and_commits_share_one_baseline(monkeypatch):
    github = _FakeGithub(TAGS, commits=[_commit("a1", "feat: x")])
    _run_script(monkeypatch, github, "no")
    # The newest stable tag is v2.13.11: the title proposes the next patch and the
    # commit list starts at the same tag.
    assert github.repo.compared == ("sha-v2.13.11", "sha-master")
    assert github.repo.tag_reads == 1
    assert github.repo.issues[0]["title"] == "Create release v2.13.12?"
    # The issue body carries the commit line generated from the same range.
    assert "- [feat: x]" in github.repo.issues[0]["body"]


def test_truncated_compare_page_is_an_error_not_partial_notes(helpers):
    # The range holds one commit more than the endpoint lists.
    commits = [_commit(f"c{i}", "feat: x") for i in range(COMPARE_PAGE)]
    github = _FakeGithub(TAGS, commits=commits, total=COMPARE_PAGE + 1)
    with pytest.raises(SystemExit, match="notes would be incomplete"):
        helpers["get_integration_commits"](github, BASE_131, "v2.13.2")


def test_complete_compare_page_of_exactly_250_is_used(helpers):
    # total_commits equals the listed count, so nothing is cut off.
    commits = [_commit(f"c{i}", "feat: x") for i in range(COMPARE_PAGE)]
    github = _FakeGithub(TAGS, commits=commits)
    notes = helpers["get_integration_commits"](github, BASE_131, "v2.13.2")
    # Every commit link appears exactly once: no omission, no duplicate.
    links = re.findall(r"\((https://example\.invalid/commit/[^)]+)\)", notes)
    expected = [f"https://example.invalid/commit/c{i}" for i in range(COMPARE_PAGE)]
    assert sorted(links) == sorted(expected)
