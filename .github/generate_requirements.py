import configparser
import json


def _write_locked_requirements(packages: dict, path: str) -> None:
    """Write a pip requirements file pinned and hashed from a Pipfile.lock section.

    Every entry already carries its fully-resolved version and hashes, so the
    output is compatible with `pip install --require-hashes`.
    """
    with open(path, "w") as f:
        for name in sorted(packages):
            spec = packages[name]
            if extras := spec.get("extras"):
                name += f"[{','.join(sorted(extras))}]"
            line = f"{name}{spec['version']}"
            if markers := spec.get("markers"):
                line += f"; {markers}"
            for digest in spec["hashes"]:
                line += f" --hash={digest}"
            f.write(line + "\n")


# homeassistant pins its own transitive dependencies (aiohttp, fnv-hash-fast,
# lru-dict, ...) to exact versions that don't ship wheels for every release, so
# `--only-binary` can never be satisfied for it, independent of which homeassistant
# version is requested. It's written to its own unpinned, --only-binary-exempt
# requirements file instead (requirements_tests_only_binary_exempt.txt) - not a
# general "extra test deps" file, just an escape hatch for this constraint.
#
# This is a Pipfile dev-package, so it's handled here. The only other package with
# the same --only-binary constraint, pytest-homeassistant-custom-component (via its
# mock-open dependency), is CI-only tooling that's deliberately not a Pipfile
# dev-package, and is installed directly alongside this file's output in ci.yml's
# "Install homeassistant [+ pytest-homeassistant-custom-component]" steps.
_SEPARATE_DEV_PACKAGES = {"homeassistant"}


def main():
    with open("Pipfile.lock") as f:
        lock = json.load(f)
    _write_locked_requirements(lock["default"], "requirements.txt")

    # Pin each direct dev-dependency to its Pipfile.lock-resolved version instead of
    # the loose Pipfile range. Only the direct entries are pinned here (not the full
    # "develop" section) since that section also carries Windows-only/sdist-only
    # transitive packages that would break the Linux CI install if forced in.
    parser = configparser.ConfigParser()
    parser.read("Pipfile")
    develop = lock["develop"]
    with (
        open("requirements_tests.txt", "w") as tests_f,
        open("requirements_tests_only_binary_exempt.txt", "w") as exempt_f,
    ):
        for key in parser["dev-packages"]:
            if key in _SEPARATE_DEV_PACKAGES:
                value = parser["dev-packages"][key].replace('"', "")
                exempt_f.write(key + value + "\n")
                continue
            resolved = develop.get(key)
            if resolved is None:
                raise ValueError(
                    f"Pipfile.lock has no 'develop' entry for dev-package {key!r}; "
                    "Pipfile and Pipfile.lock appear to be out of sync."
                )
            if "version" not in resolved:
                raise ValueError(
                    f"Pipfile.lock's develop entry for {key!r} has no "
                    f"'version' field: {resolved!r}"
                )
            tests_f.write(key + resolved["version"] + "\n")


if __name__ == "__main__":
    main()
