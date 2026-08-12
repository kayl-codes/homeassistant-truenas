"""Regenerate a Core-shaped copy of the integration from the HACS fork.

Copies ``custom_components/truenas_ce`` into a
``homeassistant/components/truenas_ce`` layout under
``core_integrations/homeassistant-core-draft/`` and applies the divergences a
``home-assistant/core`` submission needs (manifest fields, exact requirement
pins, missing quality-scale rules) without touching the HACS fork itself. Run
this again any time the fork changes to refresh the draft; it does not touch
git, fork, or open a PR (see the AI_POLICY notes in the roadmap memory).

Usage: ``python3 core_integrations/build_core_copy.py``
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

SOURCE = Path("custom_components/truenas_ce")
DEST = Path(
    "core_integrations/homeassistant-core-draft/homeassistant/components/truenas_ce"
)

DOCUMENTATION_URL = "https://www.home-assistant.io/integrations/truenas_ce"
EXTRA_LOGGERS = ["aiotruenas"]
DROPPED_MANIFEST_KEYS = ("version", "issue_tracker")
DROPPED_REQUIREMENT_PREFIXES = ("websockets",)

# anchor -> block to insert right after it, keyed by the rule name it adds.
# Both are official Bronze quality-scale rules missing from our yaml; the
# integration has no device_automation triggers/conditions, so both are exempt.
_MISSING_BRONZE_RULES = {
    "docs-conditions": (
        "  docs-actions: done\n",
        "  docs-conditions:\n"
        "    status: exempt\n"
        "    comment: This integration does not have any conditions.\n",
    ),
    "docs-triggers": (
        '    comment: README has a dedicated "Removing the integration" section.\n',
        "  docs-triggers:\n"
        "    status: exempt\n"
        "    comment: This integration does not have any triggers.\n",
    ),
}


def _copy_tree(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "brand", "translations"),
    )


def _pin_requirement(requirement: str) -> str | None:
    if requirement.startswith(DROPPED_REQUIREMENT_PREFIXES):
        return None
    match = re.match(r"^([A-Za-z0-9_.-]+)>=([\w.]+)$", requirement)
    if not match:
        return requirement
    name, version = match.groups()
    print(
        f"  requirements: pinned {name} to =={version} -- verify this is "
        "actually the latest released version before submitting"
    )
    return f"{name}=={version}"


def _transform_manifest(dest: Path) -> None:
    manifest_path = dest / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for key in DROPPED_MANIFEST_KEYS:
        manifest.pop(key, None)

    manifest["documentation"] = DOCUMENTATION_URL
    manifest.setdefault("integration_type", "device")
    manifest.setdefault("loggers", EXTRA_LOGGERS)

    pinned = (_pin_requirement(req) for req in manifest.get("requirements", []))
    manifest["requirements"] = [req for req in pinned if req is not None]

    ordered = {
        "domain": manifest.pop("domain"),
        "name": manifest.pop("name"),
    } | {key: manifest[key] for key in sorted(manifest)}
    manifest_path.write_text(json.dumps(ordered, indent=4) + "\n", encoding="utf-8")


def _transform_quality_scale(dest: Path) -> None:
    path = dest / "quality_scale.yaml"
    content = path.read_text(encoding="utf-8")

    for rule_name, (anchor, block) in _MISSING_BRONZE_RULES.items():
        if f"{rule_name}:" in content:
            continue
        if anchor not in content:
            print(
                f"  quality_scale.yaml: anchor for {rule_name} not found "
                "-- insert by hand"
            )
            continue
        content = content.replace(anchor, anchor + block, 1)

    path.write_text(content, encoding="utf-8")


def main() -> None:
    if not SOURCE.is_dir():
        sys.exit(f"source directory not found: {SOURCE}")

    print(f"Copying {SOURCE} -> {DEST}")
    _copy_tree(SOURCE, DEST)

    print("Transforming manifest.json for Core conventions")
    _transform_manifest(DEST)

    print("Adding missing Bronze quality-scale rules")
    _transform_quality_scale(DEST)

    print(
        "\nDone. Still needs manual work before this is PR-ready:\n"
        "  - port tests/ to Core conventions (MockConfigEntry, syrupy snapshots)\n"
        "  - add truenas_ce to Core's .strict-typing\n"
        "  - run `python3 -m script.hassfest` and "
        "`python3 -m script.gen_requirements_all` inside an actual "
        "home-assistant/core checkout\n"
        "  - brand icons come from the separate home-assistant/brands PR "
        "(#10948), not from this copy"
    )


if __name__ == "__main__":
    main()
