"""Mirror pip requirements into the optional uv development configuration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = "# BEGIN generated uv dependencies"
END = "# END generated uv dependencies"


def requirements(filename: str) -> list[str]:
    result = []
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        line = line.partition(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or line.endswith("\\"):
            raise ValueError(f"Unsupported requirement in {filename}: {line}")
        result.append(line)
    return result


def array(name: str, values: list[str]) -> str:
    items = "".join(f"    {json.dumps(value)},\n" for value in values)
    return f"{name} = [\n{items}]\n"


def generated_block() -> str:
    return "\n".join(
        [
            START,
            "# Edit requirements*.txt, then run python scripts/sync_uv_dependencies.py.",
            "[project]",
            'name = "openwhisper-dev"',
            "# Environment metadata only; the app version lives in _version.py.",
            'version = "0.0.0"',
            "# Match release builders; pip source installs also support Python 3.11.",
            'requires-python = ">=3.12,<3.13"',
            array("dependencies", requirements("requirements.txt")),
            "[project.optional-dependencies]",
            "# Alternative CUDA wheels for Linux or explicit developer testing.",
            array("gpu", requirements("requirements-gpu.txt")),
            "[dependency-groups]",
            'dev = [{ include-group = "build" }, "ruff==0.16.4"]',
            "# Component builders use pip's JSON dependency report.",
            array("build", requirements("requirements-build.txt") + ["pip>=24"]),
            "[tool.uv]",
            "package = false",
            "# Apply release pins to development without changing release builds.",
            array("constraint-dependencies", requirements("requirements-release-constraints.txt")),
            END,
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if pyproject.toml has drifted.")
    args = parser.parse_args()
    project = ROOT / "pyproject.toml"
    original = project.read_text(encoding="utf-8")
    generated = generated_block()
    if START in original and END in original:
        before, rest = original.split(START, 1)
        _, after = rest.split(END, 1)
        expected = before + generated + after
    elif START in original or END in original:
        raise ValueError("Incomplete generated dependency block in pyproject.toml")
    else:
        expected = generated + "\n\n" + original
    if expected == original:
        print("uv dependencies match the requirements and release constraints.")
        return 0
    if args.check:
        print("uv dependencies are stale. Run python scripts/sync_uv_dependencies.py, then uv lock.")
        return 1
    project.write_text(expected, encoding="utf-8", newline="\n")
    print("Updated pyproject.toml. Run uv lock to update uv.lock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
