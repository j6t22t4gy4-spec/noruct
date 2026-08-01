"""Portable, read-only distribution bundles for public Evolution Blueprints.

The bundle deliberately contains releases and their evidence digests only.  It
has no Capsule intake endpoint, credential, tenant state, or worker capability.
That keeps the first network-shaped artifact useful for static hosting and
cross-installation verification without turning the local Evolution database
into a customer-data service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.runtime.models import utc_now


REGISTRY_BUNDLE_SCHEMA = "noruct.public-blueprint-registry-bundle.v1"
REGISTRY_BUNDLE_SIGNING_SCHEMA = "noruct.public-blueprint-registry-signing-payload.v1"
MAX_BUNDLE_BYTES = 1_048_576


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _open_without_redirects(request: Request):
    return build_opener(_NoRedirectHandler()).open(request, timeout=10)


def build_registry_bundle(
    releases: Sequence[Mapping[str, Any]], *, registry_id: str
) -> Mapping[str, Any]:
    """Build a portable catalog from local signed-release records.

    Candidate or Capsule data is never included.  Release entries retain enough
    information for a receiving installation to verify the immutable manifest
    digest before an operator decides whether to trust the registry signer.
    """
    _safe_registry_id(registry_id)
    entries = tuple(_release_entry(release) for release in releases)
    if len({entry["release_id"] for entry in entries}) != len(entries):
        raise ValueError("Registry bundle cannot contain duplicate release_id values")
    return {
        "schema": REGISTRY_BUNDLE_SCHEMA,
        "registry_id": registry_id,
        "generated_at": utc_now().isoformat(),
        "releases": entries,
        "bundle_digest": content_digest(_unsigned_bundle(registry_id, entries)),
        "distribution": "PUBLIC_READ_ONLY_NO_CAPSULE_INTAKE",
    }


def validate_registry_bundle(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Registry bundle must be a JSON object")
    expected = {
        "schema", "registry_id", "generated_at", "releases", "bundle_digest", "distribution"
    }
    if set(value) != expected:
        raise ValueError("Registry bundle has unsupported or missing fields")
    if value["schema"] != REGISTRY_BUNDLE_SCHEMA:
        raise ValueError("Unsupported Registry bundle schema")
    registry_id = _safe_registry_id(value["registry_id"])
    if not isinstance(value["generated_at"], str) or not value["generated_at"]:
        raise ValueError("Registry bundle generated_at must be a non-empty timestamp")
    if value["distribution"] != "PUBLIC_READ_ONLY_NO_CAPSULE_INTAKE":
        raise ValueError("Registry bundle distribution policy is invalid")
    if not isinstance(value["releases"], list) and not isinstance(value["releases"], tuple):
        raise ValueError("Registry bundle releases must be a list")
    if len(value["releases"]) > 512:
        raise ValueError("Registry bundle may contain at most 512 releases")
    releases = tuple(_validate_release_entry(entry) for entry in value["releases"])
    if len({entry["release_id"] for entry in releases}) != len(releases):
        raise ValueError("Registry bundle cannot contain duplicate release_id values")
    digest = value["bundle_digest"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("Registry bundle digest must be a SHA-256 hex value")
    if digest != content_digest(_unsigned_bundle(registry_id, releases)):
        raise ValueError("Registry bundle digest does not match its immutable release entries")
    return {
        "schema": REGISTRY_BUNDLE_SCHEMA,
        "registry_id": registry_id,
        "generated_at": value["generated_at"],
        "releases": releases,
        "bundle_digest": digest,
        "distribution": value["distribution"],
    }


def registry_bundle_signing_payload(bundle: Mapping[str, Any]) -> bytes:
    """Return canonical bytes to sign externally with the existing OpenSSH flow."""
    checked = validate_registry_bundle(bundle)
    return canonical_json(
        {
            "schema": REGISTRY_BUNDLE_SIGNING_SCHEMA,
            "registry_id": checked["registry_id"],
            "bundle_digest": checked["bundle_digest"],
            "release_ids": tuple(entry["release_id"] for entry in checked["releases"]),
        }
    ).encode("utf-8")


def read_registry_bundle(path: Path) -> Mapping[str, Any]:
    if not path.is_file() or path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError("Registry bundle must be an existing JSON file up to 1 MiB")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Registry bundle must be valid UTF-8 JSON") from exc
    return validate_registry_bundle(value)


def fetch_registry_bundle(url: str, *, allow_insecure_loopback: bool = False) -> Mapping[str, Any]:
    """Fetch a read-only bundle without credentials, cookies, redirects, or uploads."""
    parsed = urlparse(url)
    if parsed.scheme == "https":
        pass
    elif (
        parsed.scheme == "http"
        and allow_insecure_loopback
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        pass
    else:
        raise ValueError("Registry fetch requires HTTPS; HTTP is allowed only for explicit loopback integration tests")
    if parsed.username or parsed.password or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("Registry URL must not include credentials, query parameters, or fragments")
    request = Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "noruct-registry-reader/1"})
    try:
        with _open_without_redirects(request) as response:  # nosec B310: scheme/host policy above
            raw = response.read(MAX_BUNDLE_BYTES + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("Registry fetch does not follow redirects") from None
        raise ValueError("Registry bundle fetch failed") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Registry bundle fetch failed") from exc
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("Registry bundle exceeds the 1 MiB limit")
    try:
        return validate_registry_bundle(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Registry response must be valid UTF-8 JSON") from exc


def _unsigned_bundle(registry_id: str, entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    return {"schema": REGISTRY_BUNDLE_SCHEMA, "registry_id": registry_id, "releases": tuple(entries)}


def _release_entry(release: Mapping[str, Any]) -> Mapping[str, Any]:
    if release.get("status") != "PUBLISHED_LOCAL":
        raise ValueError("Only PUBLISHED_LOCAL registry releases may be distributed")
    return _validate_release_entry(
        {
            "release_id": release.get("release_id"),
            "blueprint_id": release.get("blueprint_id"),
            "version": release.get("version"),
            "manifest": release.get("manifest"),
            "manifest_digest": release.get("manifest_digest"),
            "published_at": release.get("published_at"),
        }
    )


def _validate_release_entry(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Registry release entry must be an object")
    expected = {"release_id", "blueprint_id", "version", "manifest", "manifest_digest", "published_at"}
    if set(value) != expected:
        raise ValueError("Registry release entry has unsupported or missing fields")
    release_id = _safe_registry_id(value["release_id"])
    blueprint_id = _safe_registry_id(value["blueprint_id"])
    version = value["version"]
    if not isinstance(version, str) or not version or len(version) > 64:
        raise ValueError("Registry release version is invalid")
    if not isinstance(value["manifest"], Mapping):
        raise ValueError("Registry release manifest must be an object")
    if value["manifest"].get("blueprint_id") != blueprint_id or value["manifest"].get("version") != version:
        raise ValueError("Registry release manifest identity does not match its entry")
    digest = value["manifest_digest"]
    if not isinstance(digest, str) or digest != content_digest(value["manifest"]):
        raise ValueError("Registry release manifest digest does not match manifest content")
    if not isinstance(value["published_at"], str) or not value["published_at"]:
        raise ValueError("Registry release published_at must be a non-empty timestamp")
    return {
        "release_id": release_id,
        "blueprint_id": blueprint_id,
        "version": version,
        "manifest": dict(value["manifest"]),
        "manifest_digest": digest,
        "published_at": value["published_at"],
    }


def _safe_registry_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError("Registry identifier must be a non-empty value up to 80 characters")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value):
        raise ValueError("Registry identifier must contain only lower-case letters, numbers, underscores, or hyphens")
    return value
