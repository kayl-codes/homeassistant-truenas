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


def last_integration_release(github, skip=True):
    """Return last release."""
    repo = github.get_repo(REPO_NAME)
    tag_sha = None
    data = {}
    tags = list(repo.get_tags())
    reg = r"^v?\d+(\.\d+){0,2}$"
    if tags:
        for tag in tags:
            tag_name = tag.name
            if re.match(reg, tag_name):
                tag_sha = tag.commit.sha
                if skip:
                    skip = False
                    continue
                break
    data["tag_name"] = tag_name
    data["tag_sha"] = tag_sha
    return data


def get_integration_commits(github, skip=True):
    repo = github.get_repo(REPO_NAME)
    commits = new_commits(repo, last_integration_release(github, skip)["tag_sha"])
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
                integration_changes=get_integration_commits(GITHUB),
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
    integration_changes = get_integration_commits(GITHUB, False)
    if integration_changes != NOCHANGE:
        VERSION = last_integration_release(GITHUB, False)["tag_name"]
        VERSION = f"{VERSION[:-1]}{int(VERSION[-1]) + 1}"
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
