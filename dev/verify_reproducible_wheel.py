#!/usr/bin/env python3
"""Build two isolated Noruct wheels and fail unless their bytes are identical."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path


def _wheel_in(directory: Path) -> Path:
    wheels = sorted(directory.glob("noruct-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one Noruct wheel, found {len(wheels)}")
    return wheels[0]


def _archive_names(path: Path) -> tuple[str, ...]:
    with zipfile.ZipFile(path) as archive:
        return tuple(archive.namelist())


def compare(first: Path, second: Path) -> dict[str, object]:
    """Compare wheel archive member order and complete artifact bytes."""

    first_names = _archive_names(first)
    second_names = _archive_names(second)
    if first_names != second_names:
        raise RuntimeError("wheel archive member lists differ")
    first_sha = hashlib.sha256(first.read_bytes()).hexdigest()
    second_sha = hashlib.sha256(second.read_bytes()).hexdigest()
    if first_sha != second_sha:
        raise RuntimeError("wheel artifact bytes differ")
    return {"member_count": len(first_names), "sha256": first_sha}


def build(project_root: Path, python: str) -> dict[str, object]:
    """Run two deterministic wheel builds from the same source checkout."""

    environment = dict(os.environ)
    environment.update({"PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "1784300000"})
    with tempfile.TemporaryDirectory(prefix="noruct-wheel-a-") as raw_first, tempfile.TemporaryDirectory(
        prefix="noruct-wheel-b-"
    ) as raw_second:
        first_dir, second_dir = Path(raw_first), Path(raw_second)
        for output in (first_dir, second_dir):
            subprocess.run(
                [python, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "--wheel-dir", str(output)],
                cwd=project_root,
                env=environment,
                check=True,
            )
        return compare(_wheel_in(first_dir), _wheel_in(second_dir))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--python", default=os.fspath(Path(os.sys.executable)))
    args = parser.parse_args()
    result = build(args.project_root.resolve(), args.python)
    print(f"reproducible Noruct wheel: members={result['member_count']} sha256={result['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
