#!/usr/bin/env python3
"""Export only the portable migration runner's dependencies from backend/uv.lock."""

import sys
import tomllib
from pathlib import Path


def requirements(lock_path: Path) -> str:
    packages = {
        item["name"]: item for item in tomllib.loads(lock_path.read_text())["package"]
    }
    pending = ["pydantic", "pyyaml"]
    selected = set()
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        package = packages[name]
        if package.get("source") != {"registry": "https://pypi.org/simple"}:
            raise ValueError(
                "Migration dependencies must come from the pinned registry"
            )
        selected.add(name)
        pending.extend(
            dependency["name"] for dependency in package.get("dependencies", [])
        )
    lines = []
    for name in sorted(selected):
        package = packages[name]
        distributions = [package["sdist"], *package.get("wheels", [])]
        hashes = sorted({item["hash"] for item in distributions})
        lines.append(
            f"{name}=={package['version']} "
            + " ".join(f"--hash={digest}" for digest in hashes)
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(requirements(Path(sys.argv[1])), end="")
