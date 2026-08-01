"""Explicit, local-only import for one public HTTPS Knowledge source.

This is intentionally not an OAuth connector or a sync service.  A user
chooses one URL, confirms the fetch, and the resulting immutable Asset is
ingested through the normal local Knowledge boundary.  No credential, cookie,
redirect, polling cursor, or remote source authority is retained.
"""

from __future__ import annotations

import ipaddress
import hashlib
import hmac
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_REMOTE_KNOWLEDGE_BYTES = 16 * 1024 * 1024
_CONTENT_TYPE_SUFFIX = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class RemoteKnowledgeFetchError(ValueError):
    """Safe remote-fetch failure without a body, header, or credential."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


@dataclass(frozen=True, slots=True)
class RemoteKnowledgeDownload:
    source_url: str
    content_type: str
    downloaded_bytes: int
    temporary_path: Path
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteKnowledgeNotModified:
    """A conditional public source check that produced no replacement Asset."""

    source_url: str
    etag: str | None = None
    last_modified: str | None = None


def normalize_expected_sha256(expected_sha256: str | None) -> str | None:
    """Validate an optional one-shot content expectation before network I/O."""

    if expected_sha256 is None:
        return None
    if not isinstance(expected_sha256, str):
        raise RemoteKnowledgeFetchError(
            "Knowledge remote fetch expected SHA-256 must be a string"
        )
    expected = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RemoteKnowledgeFetchError(
            "Knowledge remote fetch expected SHA-256 must be exactly 64 lowercase or uppercase hexadecimal characters"
        )
    return expected


def verify_download_sha256(
    download: RemoteKnowledgeDownload,
    expected_sha256: str | None,
) -> str | None:
    """Optionally bind one explicit remote import to a known content digest.

    The digest is a caller-supplied, one-shot integrity expectation.  It is
    deliberately not stored as a remote-sync policy: a later refresh remains
    an explicit user decision and can provide a different expectation.
    """

    expected = normalize_expected_sha256(expected_sha256)
    if expected is None:
        return None
    with download.temporary_path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise RemoteKnowledgeFetchError(
            "Knowledge remote fetch content SHA-256 did not match the expected digest"
        )
    return actual


def validate_public_https_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(url.encode("utf-8")) > 2_048
    ):
        raise RemoteKnowledgeFetchError("Knowledge remote fetch requires a bounded public HTTPS URL")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise RemoteKnowledgeFetchError("Knowledge remote fetch rejects local network hosts")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return parsed.geturl()
    if not address.is_global:
        raise RemoteKnowledgeFetchError("Knowledge remote fetch rejects non-public IP hosts")
    return parsed.geturl()


def _safe_suffix(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or ""):
        return suffix
    return _CONTENT_TYPE_SUFFIX.get(content_type.split(";", 1)[0].strip().lower(), ".bin")


def _safe_response_header(value: str | None) -> str | None:
    """Keep only bounded, header-injection-safe public cache validators."""

    if value is None:
        return None
    normalized = value.strip()
    if not normalized or "\r" in normalized or "\n" in normalized:
        return None
    if len(normalized.encode("utf-8")) > 512:
        return None
    return normalized


def _optional_request_header(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    normalized = _safe_response_header(value)
    if normalized is None:
        raise RemoteKnowledgeFetchError(f"Knowledge remote refresh {label} validator is invalid")
    return normalized


def conditional_download_public_https_asset(
    url: str,
    *,
    timeout_seconds: float = 20.0,
    if_none_match: str | None = None,
    if_modified_since: str | None = None,
) -> RemoteKnowledgeDownload | RemoteKnowledgeNotModified:
    """Fetch one bounded response without redirect, authentication, or persistence.

    The caller owns the returned temporary file and must remove it after the
    ordinary local ingest has copied it into the Knowledge Vault.
    """

    source_url = validate_public_https_url(url)
    if not isinstance(timeout_seconds, (int, float)) or not 1 <= timeout_seconds <= 120:
        raise RemoteKnowledgeFetchError("Knowledge remote fetch timeout must be between 1 and 120 seconds")
    headers = {
        "Accept": "text/plain,text/markdown,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,image/*;q=0.8,*/*;q=0.1",
        "User-Agent": "Noruct-Knowledge-Import/1",
    }
    etag = _optional_request_header(if_none_match, label="ETag")
    last_modified = _optional_request_header(if_modified_since, label="Last-Modified")
    if etag is not None:
        headers["If-None-Match"] = etag
    if last_modified is not None:
        headers["If-Modified-Since"] = last_modified
    request = Request(
        source_url,
        headers=headers,
        method="GET",
    )
    try:
        response = build_opener(_NoRedirectHandler()).open(request, timeout=float(timeout_seconds))
    except HTTPError as error:
        if error.code == 304:
            try:
                return RemoteKnowledgeNotModified(
                    source_url=source_url,
                    etag=_safe_response_header(error.headers.get("ETag")),
                    last_modified=_safe_response_header(error.headers.get("Last-Modified")),
                )
            finally:
                error.close()
        raise RemoteKnowledgeFetchError(f"Knowledge remote fetch was rejected: HTTP_{error.code}") from None
    except (OSError, URLError) as error:
        raise RemoteKnowledgeFetchError(f"Knowledge remote fetch failed: {type(error).__name__}") from None
    with response:
        final_url = response.geturl()
        # No redirect handler should leave this unchanged.  Re-validate so an
        # implementation change cannot silently turn a user URL into another
        # authority boundary.
        if final_url != source_url:
            raise RemoteKnowledgeFetchError("Knowledge remote fetch does not follow redirects")
        content_type = response.headers.get_content_type().lower()
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) < 0 or int(declared) > MAX_REMOTE_KNOWLEDGE_BYTES:
                    raise RemoteKnowledgeFetchError("Knowledge remote fetch exceeds the 16 MiB limit")
            except ValueError as error:
                raise RemoteKnowledgeFetchError("Knowledge remote fetch content length is invalid") from error
        descriptor, raw_path = tempfile.mkstemp(prefix="noruct-knowledge-remote-", suffix=_safe_suffix(source_url, content_type))
        downloaded = 0
        try:
            with os.fdopen(descriptor, "wb") as target:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_REMOTE_KNOWLEDGE_BYTES:
                        raise RemoteKnowledgeFetchError("Knowledge remote fetch exceeds the 16 MiB limit")
                    target.write(chunk)
        except BaseException:
            Path(raw_path).unlink(missing_ok=True)
            raise
    return RemoteKnowledgeDownload(
        source_url,
        content_type,
        downloaded,
        Path(raw_path),
        etag=_safe_response_header(response.headers.get("ETag")),
        last_modified=_safe_response_header(response.headers.get("Last-Modified")),
    )


def download_public_https_asset(url: str, *, timeout_seconds: float = 20.0) -> RemoteKnowledgeDownload:
    """Fetch one bounded public response for explicit first-time local intake."""

    result = conditional_download_public_https_asset(url, timeout_seconds=timeout_seconds)
    if isinstance(result, RemoteKnowledgeNotModified):
        raise RemoteKnowledgeFetchError("Knowledge remote fetch received an unexpected HTTP_304")
    return result
