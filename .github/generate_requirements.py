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


def main():
    with open("Pipfile.lock") as f:
        lock = json.load(f)
    _write_locked_requirements(lock["default"], "requirements.txt")

    parser = configparser.ConfigParser()
    parser.read("Pipfile")
    with open("requirements_tests.txt", "w") as f:
        for key in parser["dev-packages"]:
            value = parser["dev-packages"][key]
            f.write(key + value.replace('"', "") + "\n")


if __name__ == "__main__":
    main()
