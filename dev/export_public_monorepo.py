#!/usr/bin/env python3
"""Materialize the allow-listed Noruct public Core monorepo.

The private development checkout remains the source of the export.  This tool
copies tracked, explicitly allow-listed files into a new empty directory and
optionally overlays the already-public concept documentation from a separate
tracked checkout.  It never deletes or modifies either source checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tomllib
from typing import Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "public-monorepo.toml"


class PublicExportError(RuntimeError):
    """The requested public projection is unsafe or ambiguous."""


def _tracked_files(root: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        raise PublicExportError("source root is not a readable Git checkout")
    return tuple(
        item.decode("utf-8")
        for item in completed.stdout.split(b"\0")
        if item
    )


def _clean_git_identity(root: Path) -> tuple[str, str]:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if status.returncode or status.stdout:
        raise PublicExportError("public documentation checkout must be clean")
    values = []
    for revision in ("HEAD", "HEAD^{tree}"):
        completed = subprocess.run(
            ["git", "rev-parse", revision],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        value = completed.stdout.strip()
        if completed.returncode or len(value) != 40:
            raise PublicExportError("public documentation Git identity is unavailable")
        values.append(value)
    return values[0], values[1]


def _manifest(path: Path = MANIFEST) -> Mapping[str, object]:
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if value.get("schema_version") != 1:
        raise PublicExportError("unsupported public-monorepo manifest schema")
    if value.get("profile") != "noruct-public-core":
        raise PublicExportError("unexpected public-monorepo profile")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PublicExportError(f"{label} must be an array of paths")
    return tuple(value)


def selected_files(
    tracked: Iterable[str], manifest: Mapping[str, object]
) -> tuple[str, ...]:
    exact = frozenset(_strings(manifest.get("include_exact"), "include_exact"))
    prefixes = _strings(manifest.get("include_prefixes"), "include_prefixes")
    private_exact = frozenset(_strings(manifest.get("private_exact"), "private_exact"))
    private_prefixes = _strings(manifest.get("private_prefixes"), "private_prefixes")
    selected: list[str] = []
    for relative in tracked:
        if relative in private_exact or relative.startswith(private_prefixes):
            continue
        if relative in exact or relative.startswith(prefixes):
            selected.append(relative)
    missing = sorted(path for path in exact if path not in selected)
    if missing:
        raise PublicExportError(
            "public allow-list names missing tracked files: " + ", ".join(missing)
        )
    return tuple(sorted(selected))


def _empty_destination(path: Path) -> Path:
    destination = path.expanduser().resolve()
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise PublicExportError("destination must be absent or an empty directory")
    else:
        destination.mkdir(parents=True)
    return destination


def _copy_tracked(root: Path, destination: Path, files: Iterable[str]) -> None:
    for relative in files:
        source = root / relative
        if source.is_symlink() or not source.is_file():
            raise PublicExportError(f"public export accepts regular files only: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _overlay_public_docs(public_docs_root: Path, destination: Path) -> tuple[str, ...]:
    root = public_docs_root.expanduser().resolve()
    copied: list[str] = []
    for relative in _tracked_files(root):
        if not relative.startswith("docs/"):
            continue
        source = root / relative
        if source.is_symlink() or not source.is_file():
            raise PublicExportError(f"public documentation must be a regular file: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(relative)
    if not copied:
        raise PublicExportError("public documentation checkout contains no tracked docs/")
    return tuple(sorted(copied))


def _tree_digest(destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        relative = path.relative_to(destination).as_posix()
        payload = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def export(
    *,
    project_root: Path,
    destination: Path,
    public_docs_root: Path | None = None,
) -> Mapping[str, object]:
    root = project_root.expanduser().resolve()
    manifest = _manifest(root / "public-monorepo.toml")
    target = _empty_destination(destination)
    files = selected_files(_tracked_files(root), manifest)
    _copy_tracked(root, target, files)
    docs: tuple[str, ...] = ()
    docs_commit: str | None = None
    docs_tree: str | None = None
    if public_docs_root is not None:
        docs_root = public_docs_root.expanduser().resolve()
        docs_commit, docs_tree = _clean_git_identity(docs_root)
        docs = _overlay_public_docs(docs_root, target)
    count, digest = _tree_digest(target)
    return {
        "schema": "noruct.public-monorepo-export.v1",
        "profile": manifest["profile"],
        "publication_state": manifest["publication_state"],
        "tracked_core_files": len(files),
        "public_document_files": len(docs),
        "public_document_commit": docs_commit,
        "public_document_tree": docs_tree,
        "output_files": count,
        "output_tree_sha256": digest,
        "hosted_service_included": False,
        "external_write": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--public-docs-root", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        receipt = export(
            project_root=args.project_root,
            destination=args.destination,
            public_docs_root=args.public_docs_root,
        )
    except PublicExportError as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True) if args.json else receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
