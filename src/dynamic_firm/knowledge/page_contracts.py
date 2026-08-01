"""Shared fail-closed contracts for user-owned Knowledge Markdown pages."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath


PAGE_SCHEMA = "noruct.knowledge-page.v1"
MAX_PAGE_BYTES = 96_000
MAX_PAGE_PATH_BYTES = 512


def normalize_page_title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Knowledge page title must be text")
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized.encode("utf-8")) > 256
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("Knowledge page title must be bounded single-line text")
    return normalized


def normalize_relative_page_path(value: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError("Knowledge page path must be text")
    normalized = value.strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or len(normalized.encode("utf-8")) > MAX_PAGE_PATH_BYTES
        or normalized.startswith("/")
        or "\\" in normalized
        or "\x00" in normalized
        or len(path.parts) < 2
        or len(path.parts) > 8
        or path.parts[0] != "pages"
        or path.suffix.casefold() != ".md"
        or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
    ):
        raise ValueError(
            "Knowledge page path must be pages/<name>.md without hidden or parent segments"
        )
    return path


def render_candidate_markdown(
    *,
    title: str,
    accepted_date: str,
    knowledge_kind: str,
    candidate_id: str,
    record_id: str,
    job_id: str,
    evidence_pack_id: str | None,
    access_scope: str,
    statement: str,
) -> str:
    lines = [
        "---",
        f"schema: {PAGE_SCHEMA}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"created: {accepted_date[:10]}",
        f"updated: {accepted_date[:10]}",
        "type: synthesis",
        "review_status: accepted",
        f"knowledge_kind: {json.dumps(knowledge_kind, ensure_ascii=False)}",
        f"source_candidate_id: {json.dumps(candidate_id, ensure_ascii=False)}",
        f"source_record_id: {json.dumps(record_id, ensure_ascii=False)}",
        f"source_job_id: {json.dumps(job_id, ensure_ascii=False)}",
        (
            "evidence_pack_id: "
            + json.dumps(evidence_pack_id, ensure_ascii=False)
            if evidence_pack_id is not None
            else "evidence_pack_id: null"
        ),
        f"access_scope: {json.dumps(access_scope, ensure_ascii=False)}",
        "---",
        "",
        f"# {title}",
        "",
        statement.rstrip(),
        "",
        "## Provenance",
        "",
        f"- Accepted Knowledge candidate: `{candidate_id}`",
        f"- Accepted Knowledge record: `{record_id}`",
        f"- Source Job: `{job_id}`",
    ]
    if evidence_pack_id is not None:
        lines.append(f"- Evidence Pack: `{evidence_pack_id}`")
    return "\n".join(lines).rstrip() + "\n"


def page_payload_identity(markdown: str) -> tuple[bytes, str, int]:
    payload = markdown.encode("utf-8")
    if len(payload) > MAX_PAGE_BYTES:
        raise ValueError("Knowledge page exceeds the bounded Markdown size")
    return payload, hashlib.sha256(payload).hexdigest(), len(payload)


def verify_published_file(
    *,
    root_path: str,
    relative_path: str,
    expected_payload: bytes,
) -> None:
    """Verify the exact file without accepting symlinks in its path.

    The page remains user-owned after the receipt is appended.  This check
    proves only that the deterministic accepted bytes existed at publication.
    """

    root = Path(root_path)
    if root.is_symlink():
        raise ValueError("Knowledge page Folder root must not be a symbolic link")
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise ValueError("Knowledge page Folder root must be an existing directory")
    relative = normalize_relative_page_path(relative_path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if os.open in os.supports_dir_fd:
        try:
            root_fd = os.open(resolved_root, os.O_RDONLY | directory | nofollow)
        except OSError as error:
            raise ValueError("Knowledge page Folder root is unavailable") from error
        parent_fd = root_fd
        opened: list[int] = []
        try:
            for part in relative.parts[:-1]:
                try:
                    child_fd = os.open(
                        part,
                        os.O_RDONLY | directory | nofollow,
                        dir_fd=parent_fd,
                    )
                except OSError as error:
                    raise ValueError(
                        "Knowledge page parent must be a non-symlink directory"
                    ) from error
                opened.append(child_fd)
                parent_fd = child_fd
            try:
                descriptor = os.open(
                    relative.parts[-1],
                    os.O_RDONLY | nofollow,
                    dir_fd=parent_fd,
                )
            except OSError as error:
                raise ValueError(
                    "Knowledge page publication file is unavailable"
                ) from error
            try:
                _verify_descriptor_payload(descriptor, expected_payload)
            finally:
                os.close(descriptor)
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
            os.close(root_fd)
        return

    current = resolved_root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise ValueError("Knowledge page parent is unavailable") from error
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError("Knowledge page parent must be a non-symlink directory")
    target = current / relative.parts[-1]
    if target.is_symlink():
        raise ValueError("Knowledge page publication target must not be a symbolic link")
    try:
        descriptor = os.open(target, os.O_RDONLY | nofollow)
    except OSError as error:
        raise ValueError("Knowledge page publication file is unavailable") from error
    try:
        _verify_descriptor_payload(descriptor, expected_payload)
    finally:
        os.close(descriptor)


def _verify_descriptor_payload(descriptor: int, expected_payload: bytes) -> None:
    observed_stat = os.fstat(descriptor)
    if not stat.S_ISREG(observed_stat.st_mode):
        raise ValueError("Knowledge page publication target must be a regular file")
    if observed_stat.st_size != len(expected_payload):
        raise ValueError("Knowledge page publication file identity does not match")
    chunks: list[bytes] = []
    remaining = len(expected_payload) + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if b"".join(chunks) != expected_payload:
        raise ValueError("Knowledge page publication file identity does not match")
