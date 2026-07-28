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


# homeassistant's own transitive dependency tree is huge and moves fast (e.g.
# habluetooth/bluetooth-data-tools). Pinning it to one exact resolved version without
# also hash-locking its full transitive graph can force pip into a fresh resolve that
# has no valid solution, even though the loose range resolves fine. Leave it floating.
_UNPINNED_DEV_PACKAGES = {"homeassistant"}


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
    with open("requirements_tests.txt", "w") as f:
        for key in parser["dev-packages"]:
            resolved = develop.get(key)
            if key not in _UNPINNED_DEV_PACKAGES and resolved is not None:
                if "version" not in resolved:
                    raise ValueError(
                        f"Pipfile.lock's develop entry for {key!r} has no "
                        f"'version' field: {resolved!r}"
                    )
                value = resolved["version"]
            else:
                value = parser["dev-packages"][key].replace('"', "")
            f.write(key + value + "\n")


if __name__ == "__main__":
    main()
