# MIT License
#
# Copyright (c) 2019 Joakim Sørensen @ludeeus
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import re
import sys

from github import Github

BADGE = (
    "[![Downloads for this release]"
    "(https://img.shields.io/github/downloads/"
    "kayl-codes/homeassistant-truenas/{version}/total.svg)]"
    "(https://github.com/kayl-codes/homeassistant-truenas/releases/{version})"
)

# Stable substring used to detect a badge already present in a release body,
# so re-runs on publish never duplicate it.
BADGE_MARKER = "img.shields.io/github/downloads"

BODY = """
{badge}

{changes}
"""

CHANGES = """
## Changes

{integration_changes}

"""

CHANGE = "- [{line}]({link}) @{author}\n"
NOCHANGE = "_No changes in this release._"
REPO_NAME = "kayl-codes/homeassistant-truenas"

# Commit-message markers that exclude a commit from the generated changelog.
_SKIP_MARKERS = (
    "flake",
    " workflow",
    " test",
    "docs",
    "dev debug",
    "Merge branch ",
    "Merge pull request ",
)

GITHUB = Github(sys.argv[2])


def new_commits(repo, sha):
    """Get new commits in repo."""
    from datetime import datetime

    dateformat = "%a, %d %b %Y %H:%M:%S GMT"
    release_commit = repo.get_commit(sha)
    since = datetime.strptime(release_commit.last_modified, dateformat)
    commits = list(repo.get_commits(since=since))
    if len(commits) <= 1:
        return False
    return reversed(commits[:-1])


def _version_key(tag_name):
    """Numeric sort key of a tag name such as ``v2.13.1`` or ``v2.13.2-rc2``.

    A pre-release suffix is dropped, so ``v2.13.2-rc2`` sorts as ``v2.13.2``.
    Missing components are padded with 0, so the abbreviated ``v2.13`` equals
    ``v2.13.0`` instead of comparing as a shorter, different tuple.
    """
    parts = [int(part) for part in tag_name.split("-")[0].lstrip("v").split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def last_integration_release(github, current=None):
    """Return the newest stable release older than the one being published.

    ``current`` is the tag name of the release being published (or None for no
    limit). It is matched by exact name, and only stable tags below its version
    are candidates, so publishing an older release after a newer one still
    gets its own baseline. The result is the highest such version by number,
    so the order of the tag list does not matter. Pre-release tags (``-rcN``)
    are never returned as the baseline.
    """
    repo = github.get_repo(REPO_NAME)
    stable_reg = r"^v?\d+(\.\d+){0,2}$"
    versioned = current and re.match(r"^v?\d+(\.\d+){0,2}(-rc\d+)?$", current)
    limit = _version_key(current) if versioned else None
    candidates = [
        tag
        for tag in repo.get_tags()
        if tag.name != current
        and re.match(stable_reg, tag.name)
        and (limit is None or _version_key(tag.name) < limit)
    ]
    if not candidates:
        return {"tag_name": None, "tag_sha": None}
    # Equal keys (v2.13 and v2.13.0) are tie-broken by the full name, so the
    # result does not depend on the tag-list order.
    best = max(candidates, key=lambda tag: (_version_key(tag.name), tag.name))
    return {"tag_name": best.name, "tag_sha": best.commit.sha}


def get_integration_commits(github, current=None):
    repo = github.get_repo(REPO_NAME)
    base_sha = last_integration_release(github, current)["tag_sha"]
    if not base_sha:
        return NOCHANGE
    commits = new_commits(repo, base_sha)
    if not commits:
        return NOCHANGE

    changes = ""
    for commit in commits:
        msg = repo.get_git_commit(commit.sha).message
        if any(marker in msg for marker in _SKIP_MARKERS):
            continue
        msg = msg.split("\n", 1)[0]
        ath = commit.author.login if commit.author else "Unknown"
        changes += CHANGE.format(line=msg, link=commit.html_url, author=ath)

    return changes


# Update release notes:
UPDATERELEASE = str(sys.argv[4])
REPO = GITHUB.get_repo(REPO_NAME)
if UPDATERELEASE == "yes":
    VERSION = str(sys.argv[6]).replace("refs/tags/", "")
    RELEASE = REPO.get_release(VERSION)
    # Keep the raw body so curated formatting (leading/trailing blank lines) is
    # preserved; only use a stripped copy to decide whether notes actually exist.
    existing_body = RELEASE.body or ""
    badge = BADGE.format(version=VERSION)
    if existing_body.strip():
        # Curated notes are already present: keep them untouched and prepend the
        # download badge, unless one is already there (BADGE_MARKER) so re-runs
        # never duplicate it.
        if BADGE_MARKER in existing_body:
            new_body = existing_body
        else:
            new_body = f"{badge}\n\n{existing_body}"
    else:
        # No curated notes: fall back to the auto-generated badge + commit list.
        new_body = BODY.format(
            badge=badge,
            changes=CHANGES.format(
                integration_changes=get_integration_commits(GITHUB, VERSION),
            ),
        )
    # Preserve the existing draft/prerelease flags: update_release() defaults
    # both to False, so omitting them would silently flip a pre-release into a
    # normal "Latest" release when the notes are regenerated on publish.
    RELEASE.update_release(
        name=f"TrueNAS {VERSION}",
        message=new_body,
        draft=RELEASE.draft,
        prerelease=RELEASE.prerelease,
    )
else:
    integration_changes = get_integration_commits(GITHUB)
    if integration_changes != NOCHANGE:
        parts = list(_version_key(last_integration_release(GITHUB)["tag_name"]))
        parts[-1] += 1
        VERSION = "v" + ".".join(str(part) for part in parts)
        REPO.create_issue(
            title=f"Create release {VERSION}?",
            labels=["New release"],
            assignee="kayl-codes",
            body=CHANGES.format(
                integration_changes=integration_changes,
            ),
        )
    else:
        print("Not enough changes for a release.")
