"""Portable, signed, read-only Evolution Artifact registry bundles."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dynamic_firm.company.models import canonical_json, content_digest
from dynamic_firm.runtime.models import utc_now

from .score_contract import evolution_content_digest
from .signing import MAX_OPENSSH_SIGNATURE_BYTES


ARTIFACT_REGISTRY_BUNDLE_SCHEMA = "noruct.public-evolution-artifact-registry-bundle.v1"
ARTIFACT_REGISTRY_SIGNING_SCHEMA = "noruct.public-evolution-artifact-registry-signing-payload.v1"
MAX_ARTIFACT_BUNDLE_BYTES = 1_048_576
MAX_ARTIFACT_SIGNATURE_BYTES = MAX_OPENSSH_SIGNATURE_BYTES
MAX_ARTIFACT_REGISTRY_INDEX_BYTES = 65_536
ARTIFACT_REGISTRY_INDEX_SCHEMA = "noruct.public-evolution-artifact-registry-index.v1"


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects before urllib can construct a follow-up request."""

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


def _validated_manifest(value: object) -> Mapping[str, Any]:
    # Avoid a module cycle: service imports the existing Blueprint registry
    # helpers, while this artifact-only bundle needs its strict manifest parser.
    from .service import validate_evolution_artifact

    return validate_evolution_artifact(value)


def build_artifact_registry_bundle(artifacts: Sequence[Mapping[str, Any]], *, registry_id: str) -> Mapping[str, Any]:
    _safe_id(registry_id, "registry_id")
    # The staged local catalog reads entries in stable id/version order.  Make
    # that order part of the signed bundle itself so a multi-artifact release
    # has one digest before and after its SQLite round trip.
    entries = tuple(
        sorted(
            (_entry(item) for item in artifacts),
            key=lambda item: (str(item["artifact_id"]), str(item["version"])),
        )
    )
    if len({(item["artifact_id"], item["version"]) for item in entries}) != len(entries):
        raise ValueError("Artifact registry bundle cannot contain duplicate id/version entries")
    unsigned = {"schema": ARTIFACT_REGISTRY_BUNDLE_SCHEMA, "registry_id": registry_id, "artifacts": entries}
    return {**unsigned, "generated_at": utc_now().isoformat(), "bundle_digest": evolution_content_digest(unsigned), "distribution": "PUBLIC_READ_ONLY_NO_CAPSULE_INTAKE"}


def validate_artifact_registry_bundle(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Artifact registry bundle must be a JSON object")
    expected = {"schema", "registry_id", "generated_at", "artifacts", "bundle_digest", "distribution"}
    if set(value) != expected or value.get("schema") != ARTIFACT_REGISTRY_BUNDLE_SCHEMA:
        raise ValueError("Unsupported or malformed Artifact registry bundle")
    registry_id = _safe_id(value.get("registry_id"), "registry_id")
    if value.get("distribution") != "PUBLIC_READ_ONLY_NO_CAPSULE_INTAKE" or not isinstance(value.get("generated_at"), str):
        raise ValueError("Artifact registry bundle distribution metadata is invalid")
    raw = value.get("artifacts")
    if not isinstance(raw, (list, tuple)) or len(raw) > 512:
        raise ValueError("Artifact registry bundle must contain at most 512 artifacts")
    artifacts = tuple(_entry(item) for item in raw)
    if len({(item["artifact_id"], item["version"]) for item in artifacts}) != len(artifacts):
        raise ValueError("Artifact registry bundle cannot contain duplicate id/version entries")
    if tuple((item["artifact_id"], item["version"]) for item in artifacts) != tuple(
        sorted((item["artifact_id"], item["version"]) for item in artifacts)
    ):
        raise ValueError("Artifact registry bundle entries must use stable id/version order")
    unsigned = {"schema": ARTIFACT_REGISTRY_BUNDLE_SCHEMA, "registry_id": registry_id, "artifacts": artifacts}
    digest = value.get("bundle_digest")
    if not isinstance(digest, str) or digest != evolution_content_digest(unsigned):
        raise ValueError("Artifact registry bundle digest does not match immutable entries")
    return {**unsigned, "generated_at": value["generated_at"], "bundle_digest": digest, "distribution": value["distribution"]}


def artifact_registry_bundle_signing_payload(bundle: Mapping[str, Any]) -> bytes:
    checked = validate_artifact_registry_bundle(bundle)
    return canonical_json({"schema": ARTIFACT_REGISTRY_SIGNING_SCHEMA, "registry_id": checked["registry_id"], "bundle_digest": checked["bundle_digest"], "artifact_versions": tuple(f"{item['artifact_id']}@{item['version']}" for item in checked["artifacts"])}).encode("utf-8")


def read_artifact_registry_bundle(path: Path) -> Mapping[str, Any]:
    if not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BUNDLE_BYTES:
        raise ValueError("Artifact registry bundle must be an existing JSON file up to 1 MiB")
    try:
        return validate_artifact_registry_bundle(json.loads(path.read_text(encoding="utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Artifact registry bundle must be valid UTF-8 JSON") from exc


def _bearer_header(token: str | None) -> dict[str, str]:
    """Return one bounded bearer header without persisting its value.

    Network callers resolve a private Registry token only immediately before
    I/O.  Redirects are rejected by this module, preventing that header from
    being forwarded to another origin.
    """

    if token is None:
        return {}
    if not isinstance(token, str) or not token or len(token) > 512 or "\r" in token or "\n" in token:
        raise ValueError("Artifact registry bearer token is invalid")
    return {"Authorization": f"Bearer {token}"}


def fetch_artifact_registry_bundle(
    url: str,
    *,
    allow_insecure_loopback: bool = False,
    bearer_token: str | None = None,
) -> Mapping[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and allow_insecure_loopback and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("Artifact registry fetch requires HTTPS; HTTP is allowed only for explicit loopback tests")
    if parsed.username or parsed.password or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("Artifact registry URL must not include credentials, query parameters, or fragments")
    request = Request(url, method="GET", headers={
        "Accept": "application/json",
        "User-Agent": "noruct-artifact-registry-reader/1",
        **_bearer_header(bearer_token),
    })
    try:
        with _open_without_redirects(request) as response:  # nosec B310: scheme/host policy above
            raw = response.read(MAX_ARTIFACT_BUNDLE_BYTES + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("Artifact registry fetch does not follow redirects") from None
        raise ValueError("Artifact registry fetch failed") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Artifact registry fetch failed") from exc
    if len(raw) > MAX_ARTIFACT_BUNDLE_BYTES:
        raise ValueError("Artifact registry bundle exceeds the 1 MiB limit")
    try:
        return validate_artifact_registry_bundle(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Artifact registry response must be valid UTF-8 JSON") from exc


def discover_artifact_registries(
    origin: str,
    *,
    allow_insecure_loopback: bool = False,
    bearer_token: str | None = None,
) -> tuple[Mapping[str, str], ...]:
    """List a bounded public registry index without trusting or staging it.

    Discovery is intentionally separate from bundle fetch/stage.  The index
    supplies only immutable pointer identities and digests; callers must still
    fetch the chosen bundle and detached signature over the independently
    checked URLs, then verify the signature against a local trust root.
    """

    parsed = urlparse(origin.strip())
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and allow_insecure_loopback and loopback
    ):
        raise ValueError("Artifact registry discovery requires HTTPS; HTTP is allowed only for explicit loopback tests")
    if (
        parsed.username
        or parsed.password
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Artifact registry discovery origin must not include credentials, path, query parameters, or fragments")
    endpoint = f"{parsed.scheme}://{parsed.netloc}/v1/artifact-registries"
    request = Request(
        endpoint,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "noruct-artifact-registry-reader/1",
            **_bearer_header(bearer_token),
        },
    )
    try:
        with _open_without_redirects(request) as response:  # nosec B310: scheme/host policy above
            raw = response.read(MAX_ARTIFACT_REGISTRY_INDEX_BYTES + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("Artifact registry discovery does not follow redirects") from None
        raise ValueError("Artifact registry discovery failed") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Artifact registry discovery failed") from exc
    if len(raw) > MAX_ARTIFACT_REGISTRY_INDEX_BYTES:
        raise ValueError("Artifact registry discovery response exceeds the 64 KiB limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Artifact registry discovery response must be valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping) or set(value) != {"schema", "registries"} or value.get("schema") != ARTIFACT_REGISTRY_INDEX_SCHEMA:
        raise ValueError("Artifact registry discovery response has an unsupported shape")
    rows = value.get("registries")
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Artifact registry discovery result must contain at most 100 entries")
    discovered: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "registry_id", "bundle_digest", "signature_digest", "published_at"
        }:
            raise ValueError("Artifact registry discovery entry has an unsupported shape")
        registry_id = _safe_id(row.get("registry_id"), "registry_id")
        bundle_digest = row.get("bundle_digest")
        signature_digest = row.get("signature_digest")
        published_at = row.get("published_at")
        if (
            registry_id in seen
            or not isinstance(bundle_digest, str)
            or len(bundle_digest) != 64
            or any(character not in "0123456789abcdef" for character in bundle_digest)
            or not isinstance(signature_digest, str)
            or len(signature_digest) != 64
            or any(character not in "0123456789abcdef" for character in signature_digest)
            or not isinstance(published_at, str)
            or not published_at
            or len(published_at) > 80
        ):
            raise ValueError("Artifact registry discovery entry is invalid")
        seen.add(registry_id)
        discovered.append(
            {
                "registry_id": registry_id,
                "bundle_digest": bundle_digest,
                "signature_digest": signature_digest,
                "published_at": published_at,
            }
        )
    return tuple(discovered)


def fetch_discovered_artifact_registry(
    origin: str,
    registry_id: str,
    *,
    allow_insecure_loopback: bool = False,
    bearer_token: str | None = None,
) -> tuple[Mapping[str, str], Mapping[str, Any], bytes]:
    """Resolve one public index pointer into its exact bundle and signature.

    This is deliberately a *fetch* primitive, not a trust or installation
    primitive.  It turns the public Worker route convention into one bounded
    operation while preserving the important order of checks:

    ``index -> exact bundle/signature digest -> caller-owned signer verify -> stage``.

    The public index cannot choose an arbitrary URL; after the same strict
    origin validation used by discovery, the two resource paths are derived
    from the selected registry identifier.  A changed index, bundle, or
    signature therefore fails before a local trust root is consulted.
    """

    parsed = urlparse(origin.strip())
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and allow_insecure_loopback and loopback
    ):
        raise ValueError(
            "Artifact registry discovery requires HTTPS; HTTP is allowed only for explicit loopback tests"
        )
    if (
        parsed.username
        or parsed.password
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "Artifact registry discovery origin must not include credentials, path, query parameters, or fragments"
        )
    checked_registry_id = _safe_id(registry_id, "registry_id")
    pointer = next(
        (
            item
            for item in discover_artifact_registries(
                origin,
                allow_insecure_loopback=allow_insecure_loopback,
                bearer_token=bearer_token,
            )
            if item["registry_id"] == checked_registry_id
        ),
        None,
    )
    if pointer is None:
        raise ValueError("Requested Artifact registry is not present in the public index")

    base = f"{parsed.scheme}://{parsed.netloc}"
    bundle = fetch_artifact_registry_bundle(
        f"{base}/v1/artifact-registries/{checked_registry_id}/bundle",
        allow_insecure_loopback=allow_insecure_loopback,
        bearer_token=bearer_token,
    )
    if bundle["registry_id"] != checked_registry_id or bundle["bundle_digest"] != pointer["bundle_digest"]:
        raise ValueError("Artifact registry bundle does not match its discovered public pointer")
    signature = fetch_artifact_registry_signature(
        f"{base}/v1/artifact-registries/{checked_registry_id}/signature",
        allow_insecure_loopback=allow_insecure_loopback,
        bearer_token=bearer_token,
    )
    if hashlib.sha256(signature).hexdigest() != pointer["signature_digest"]:
        raise ValueError("Artifact registry signature does not match its discovered public pointer")
    return pointer, bundle, signature


def fetch_private_network_artifact_registry(
    origin: str,
    registry_id: str,
    *,
    allow_insecure_loopback: bool = False,
    bearer_token: str,
) -> tuple[Mapping[str, str], Mapping[str, Any], bytes]:
    """Fetch one credential-gated PRIVATE_TEAM Registry without an index.

    Private registry identifiers are supplied by an operator's local source
    configuration, never enumerated or searched remotely.  The same detached
    signature and bundle validation path applies after this bounded download.
    """

    parsed = urlparse(origin.strip())
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and allow_insecure_loopback and loopback
    ):
        raise ValueError("Private Network registry fetch requires HTTPS; HTTP is allowed only for explicit loopback tests")
    if (
        parsed.username or parsed.password or not parsed.hostname or parsed.query
        or parsed.fragment or parsed.path not in {"", "/"}
    ):
        raise ValueError("Private Network registry origin must be a bare origin")
    checked_registry_id = _safe_id(registry_id, "registry_id")
    base = f"{parsed.scheme}://{parsed.netloc}/v1/network/registries/{checked_registry_id}"
    bundle = fetch_artifact_registry_bundle(
        f"{base}/bundle",
        allow_insecure_loopback=allow_insecure_loopback,
        bearer_token=bearer_token,
    )
    if bundle["registry_id"] != checked_registry_id:
        raise ValueError("Private Network registry bundle identity does not match the configured registry")
    signature = fetch_artifact_registry_signature(
        f"{base}/signature",
        allow_insecure_loopback=allow_insecure_loopback,
        bearer_token=bearer_token,
    )
    return (
        {
            "registry_id": checked_registry_id,
            "bundle_digest": str(bundle["bundle_digest"]),
            "signature_digest": hashlib.sha256(signature).hexdigest(),
            "published_at": "PRIVATE_AUTHENTICATED_NO_INDEX",
        },
        bundle,
        signature,
    )


def fetch_artifact_registry_signature(
    url: str,
    *,
    allow_insecure_loopback: bool = False,
    bearer_token: str | None = None,
) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and allow_insecure_loopback and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("Artifact registry signature fetch requires HTTPS; HTTP is allowed only for explicit loopback tests")
    if parsed.username or parsed.password or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("Artifact registry signature URL must not include credentials, query parameters, or fragments")
    request = Request(url, method="GET", headers={
        "Accept": "application/ssh-signature, text/plain",
        "User-Agent": "noruct-artifact-registry-reader/1",
        **_bearer_header(bearer_token),
    })
    try:
        with _open_without_redirects(request) as response:  # nosec B310: scheme/host policy above
            content_type = response.headers.get_content_type()
            if content_type not in {"application/ssh-signature", "text/plain"}:
                raise ValueError("Artifact registry signature response has an unsupported content type")
            raw = response.read(MAX_ARTIFACT_SIGNATURE_BYTES + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise ValueError("Artifact registry signature fetch does not follow redirects") from None
        raise ValueError("Artifact registry signature fetch failed") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Artifact registry signature fetch failed") from exc
    if not raw or len(raw) > MAX_ARTIFACT_SIGNATURE_BYTES:
        raise ValueError("Artifact registry signature must contain up to 32 KiB")
    if not raw.startswith(b"-----BEGIN SSH SIGNATURE-----") or not raw.rstrip().endswith(b"-----END SSH SIGNATURE-----"):
        raise ValueError("Artifact registry signature has an unsupported format")
    return raw


def _entry(value: Mapping[str, Any]) -> Mapping[str, Any]:
    manifest = value.get("manifest", value)
    checked = _validated_manifest(manifest)
    return {"artifact_id": checked["artifact_id"], "version": checked["version"], "kind": checked["kind"], "release_channel": checked["release_channel"], "manifest": checked, "manifest_digest": evolution_content_digest(checked)}


def _safe_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 80 or any(item not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for item in value):
        raise ValueError(f"{name} must be a lower-case identifier")
    return value
