"""Signed, review-only executable-plugin catalog intake.

The catalog is deliberately a discovery and staging surface, not a Python
plugin registry.  Every entry resolves to the existing exact-commit Git
installer, remains disabled, and still needs the ordinary dependency build and
explicit enable steps.  This keeps marketplace metadata from acquiring tool,
provider, or Company-state authority.

The shape follows the useful catalogue/discovery separation inspected in the
registered Hermes plugin CLI sources, while using Noruct's detached-signature
and out-of-process plugin boundaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from dynamic_firm.company.models import canonical_json
from dynamic_firm.evolution.signing import verify_openssh_signature_bytes

from .executable_plugins import ExecutablePlugin, ExecutablePluginStore, PluginLifecycleError


CATALOG_SCHEMA = "noruct.executable-plugin-catalog.v1"
CATALOG_SIGNING_NAMESPACE = "noruct-executable-plugin-catalog-v1"
CATALOG_SOURCES_SCHEMA = "noruct.executable-plugin-catalog-sources.v1"
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HTTPS_REPOSITORY = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]{1,5})?/[A-Za-z0-9._~:/-]+(?:\.git)?\Z")
_MAX_CATALOG_BYTES = 256_000
_MAX_SIGNATURE_BYTES = 32_000


class PluginCatalogError(ValueError):
    """A safe catalog-discovery error that never exposes response contents."""


@dataclass(frozen=True, slots=True)
class PluginCatalogEntry:
    plugin_id: str
    version: str
    description: str
    repository_url: str
    commit: str
    subdirectory: str


@dataclass(frozen=True, slots=True)
class PluginCatalog:
    catalog_id: str
    digest: str
    entries: tuple[PluginCatalogEntry, ...]
    source_url: str
    signature_url: str
    verified_at: str
    verification: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PluginCatalogCandidate:
    """A local, review-only catalog entry not present in the plugin store.

    This is deliberately not an update decision: catalog versions are opaque
    strings and the catalog never gains permission to replace a receipt.  It
    simply lets an operator see the exact staged entry that would require a
    separate disabled install and normal dependency/enable review.
    """

    catalog_id: str
    catalog_digest: str
    plugin_id: str
    candidate_version: str
    description: str
    installed_versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginCatalogSource:
    """One operator-registered signed catalog origin.

    The record is a refresh convenience, not an authority grant: refresh must
    still be explicitly confirmed, re-verifies the detached signature, and
    cannot install, enable, or execute a plugin.
    """

    catalog_id: str
    source_url: str
    signature_url: str
    allowed_signers_path: Path
    principal: str
    ssh_keygen: Path

    def validate(self) -> "PluginCatalogSource":
        if not isinstance(self.catalog_id, str) or not _IDENTIFIER.fullmatch(self.catalog_id):
            raise PluginCatalogError("Catalog id is invalid")
        source_url = _https_url(self.source_url, label="Catalog URL")
        signature_url = _https_url(self.signature_url, label="Catalog signature URL")
        if not isinstance(self.principal, str) or not self.principal or len(self.principal.encode("utf-8")) > 128 or "\x00" in self.principal:
            raise PluginCatalogError("Catalog signer principal is invalid")
        allowed = self.allowed_signers_path.expanduser()
        verifier = self.ssh_keygen.expanduser()
        if not allowed.is_absolute() or allowed.is_symlink() or not allowed.is_file():
            raise PluginCatalogError("Catalog allowed-signers file must be an existing absolute regular file")
        if not verifier.is_absolute() or verifier.is_symlink() or not verifier.is_file() or not os.access(verifier, os.X_OK):
            raise PluginCatalogError("Catalog ssh-keygen verifier must be an existing absolute executable file")
        return PluginCatalogSource(self.catalog_id, source_url, signature_url, allowed.resolve(), self.principal, verifier.resolve())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise PluginCatalogError("Catalog endpoint must not redirect")


def _https_url(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"https://[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]{1,5})?/[A-Za-z0-9._~:/?=&%-]*", value):
        raise PluginCatalogError(f"{label} must be a credential-free HTTPS URL")
    return value


def _bounded_fetch(url: str, *, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json, application/octet-stream;q=0.9"}, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                raise PluginCatalogError("Catalog endpoint returned an unexpected status")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > limit):
                raise PluginCatalogError("Catalog response exceeds the size limit")
            body = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise PluginCatalogError("Catalog request failed") from exc
    if not body or len(body) > limit:
        raise PluginCatalogError("Catalog response exceeds the size limit")
    return body


def _entry(raw: Mapping[str, Any]) -> PluginCatalogEntry:
    required = {"plugin_id", "version", "description", "repository_url", "commit", "subdirectory"}
    if set(raw) != required:
        raise PluginCatalogError("Catalog entry shape is invalid")
    plugin_id = raw["plugin_id"]
    version = raw["version"]
    description = raw["description"]
    repository_url = raw["repository_url"]
    commit = raw["commit"]
    subdirectory = raw["subdirectory"]
    if not isinstance(plugin_id, str) or not _IDENTIFIER.fullmatch(plugin_id):
        raise PluginCatalogError("Catalog plugin id is invalid")
    if not isinstance(version, str) or not version or len(version.encode("utf-8")) > 64:
        raise PluginCatalogError("Catalog plugin version is invalid")
    if not isinstance(description, str) or len(description.encode("utf-8")) > 1000:
        raise PluginCatalogError("Catalog plugin description is invalid")
    if not isinstance(repository_url, str) or not _HTTPS_REPOSITORY.fullmatch(repository_url):
        raise PluginCatalogError("Catalog plugin repository URL is invalid")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise PluginCatalogError("Catalog plugin commit must be an exact lowercase SHA")
    if not isinstance(subdirectory, str) or len(subdirectory.encode("utf-8")) > 240 or "\x00" in subdirectory:
        raise PluginCatalogError("Catalog plugin subdirectory is invalid")
    parts = Path(subdirectory or ".").parts
    if Path(subdirectory or ".").is_absolute() or ".." in parts:
        raise PluginCatalogError("Catalog plugin subdirectory escaped its repository")
    return PluginCatalogEntry(plugin_id, version, description, repository_url, commit, Path(subdirectory or ".").as_posix())


def parse_catalog(raw: bytes, *, source_url: str, signature_url: str, signature: bytes, allowed_signers_path: Path, principal: str, ssh_keygen: Path) -> PluginCatalog:
    if not raw or len(raw) > _MAX_CATALOG_BYTES:
        raise PluginCatalogError("Catalog response exceeds the size limit")
    if not signature or len(signature) > _MAX_SIGNATURE_BYTES:
        raise PluginCatalogError("Catalog signature exceeds the size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginCatalogError("Catalog must be valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping) or set(value) != {"schema", "catalog_id", "entries"} or value.get("schema") != CATALOG_SCHEMA:
        raise PluginCatalogError("Catalog shape is invalid")
    catalog_id = value.get("catalog_id")
    entries = value.get("entries")
    if not isinstance(catalog_id, str) or not _IDENTIFIER.fullmatch(catalog_id) or not isinstance(entries, list) or not entries or len(entries) > 128:
        raise PluginCatalogError("Catalog identity or entries are invalid")
    parsed = tuple(_entry(item) for item in entries if isinstance(item, Mapping))
    if len(parsed) != len(entries) or len({(item.plugin_id, item.version) for item in parsed}) != len(parsed):
        raise PluginCatalogError("Catalog entries must be unique")
    canonical = canonical_json({"schema": CATALOG_SCHEMA, "catalog_id": catalog_id, "entries": [dict(item) for item in value["entries"]]}).encode("utf-8")
    if raw != canonical:
        raise PluginCatalogError("Catalog must use canonical JSON encoding")
    try:
        verification = verify_openssh_signature_bytes(canonical, signature=signature, allowed_signers_path=allowed_signers_path, principal=principal, command=ssh_keygen, namespace=CATALOG_SIGNING_NAMESPACE)
    except ValueError as exc:
        raise PluginCatalogError(str(exc)) from exc
    return PluginCatalog(catalog_id=catalog_id, digest=hashlib.sha256(canonical).hexdigest(), entries=parsed, source_url=source_url, signature_url=signature_url, verified_at=datetime.now(timezone.utc).isoformat(), verification=verification)


class PluginCatalogStore:
    """Durably stage verified catalog metadata under the managed plugin root."""

    def __init__(self, plugin_root: Path) -> None:
        self.root = plugin_root.expanduser().resolve() / "catalogs"

    @property
    def sources_path(self) -> Path:
        return self.root.parent / "catalog-sources.json"

    def list_sources(self) -> tuple[PluginCatalogSource, ...]:
        path = self.sources_path
        if not path.is_file():
            return ()
        if path.is_symlink():
            raise PluginCatalogError("Catalog source registry must be a regular file")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginCatalogError("Catalog source registry is unreadable") from exc
        if not isinstance(raw, Mapping) or set(raw) != {"schema", "sources"} or raw.get("schema") != CATALOG_SOURCES_SCHEMA or not isinstance(raw.get("sources"), list):
            raise PluginCatalogError("Catalog source registry is malformed")
        items: list[PluginCatalogSource] = []
        for item in raw["sources"]:
            if not isinstance(item, Mapping) or set(item) != {"catalog_id", "source_url", "signature_url", "allowed_signers_path", "principal", "ssh_keygen"}:
                raise PluginCatalogError("Catalog source registry entry is malformed")
            try:
                source = PluginCatalogSource(
                    catalog_id=str(item["catalog_id"]), source_url=str(item["source_url"]), signature_url=str(item["signature_url"]),
                    allowed_signers_path=Path(str(item["allowed_signers_path"])), principal=str(item["principal"]), ssh_keygen=Path(str(item["ssh_keygen"])),
                ).validate()
            except (TypeError, ValueError) as exc:
                raise PluginCatalogError("Catalog source registry entry is invalid") from exc
            items.append(source)
        if len({item.catalog_id for item in items}) != len(items):
            raise PluginCatalogError("Catalog source registry has duplicate catalog ids")
        return tuple(sorted(items, key=lambda item: item.catalog_id))

    def register_source(self, source: PluginCatalogSource) -> PluginCatalogSource:
        source = source.validate()
        sources = {item.catalog_id: item for item in self.list_sources()}
        sources[source.catalog_id] = source
        self._write_sources(tuple(sources[item] for item in sorted(sources)))
        return source

    def remove_source(self, catalog_id: str) -> bool:
        if not isinstance(catalog_id, str) or not _IDENTIFIER.fullmatch(catalog_id):
            raise PluginCatalogError("Catalog id is invalid")
        sources = {item.catalog_id: item for item in self.list_sources()}
        if catalog_id not in sources:
            return False
        del sources[catalog_id]
        self._write_sources(tuple(sources[item] for item in sorted(sources)))
        return True

    def refresh_source(self, catalog_id: str) -> PluginCatalog:
        sources = {item.catalog_id: item for item in self.list_sources()}
        source = sources.get(catalog_id)
        if source is None:
            raise PluginCatalogError("Catalog source is not registered")
        catalog = self.fetch_and_stage(
            source_url=source.source_url, signature_url=source.signature_url,
            allowed_signers_path=source.allowed_signers_path, principal=source.principal,
            ssh_keygen=source.ssh_keygen, expected_catalog_id=source.catalog_id,
        )
        return catalog

    def _write_sources(self, sources: tuple[PluginCatalogSource, ...]) -> None:
        destination = self.sources_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "schema": CATALOG_SOURCES_SCHEMA,
                "sources": [
                    {
                        "catalog_id": item.catalog_id, "source_url": item.source_url,
                        "signature_url": item.signature_url, "allowed_signers_path": str(item.allowed_signers_path),
                        "principal": item.principal, "ssh_keygen": str(item.ssh_keygen),
                    }
                    for item in sources
                ],
            }, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".noruct-plugin-catalog-sources-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary, 0o600); os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def fetch_and_stage(self, *, source_url: str, signature_url: str, allowed_signers_path: Path, principal: str, ssh_keygen: Path, expected_catalog_id: str | None = None) -> PluginCatalog:
        source_url = _https_url(source_url, label="Catalog URL")
        signature_url = _https_url(signature_url, label="Catalog signature URL")
        catalog = parse_catalog(_bounded_fetch(source_url, limit=_MAX_CATALOG_BYTES), source_url=source_url, signature_url=signature_url, signature=_bounded_fetch(signature_url, limit=_MAX_SIGNATURE_BYTES), allowed_signers_path=allowed_signers_path.expanduser().resolve(), principal=principal, ssh_keygen=ssh_keygen.expanduser().resolve())
        if expected_catalog_id is not None and catalog.catalog_id != expected_catalog_id:
            raise PluginCatalogError("Refreshed catalog identity did not match the registered source")
        destination = self.root / catalog.catalog_id / f"{catalog.digest}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"catalog": {"schema": CATALOG_SCHEMA, "catalog_id": catalog.catalog_id, "entries": [asdict(entry) for entry in catalog.entries]}, "digest": catalog.digest, "source_url": catalog.source_url, "signature_url": catalog.signature_url, "verified_at": catalog.verified_at, "verification": dict(catalog.verification)}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".noruct-plugin-catalog-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary, 0o600); os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return catalog

    def list(self) -> tuple[PluginCatalog, ...]:
        if not self.root.is_dir():
            return ()
        items: list[PluginCatalog] = []
        for path in sorted(self.root.glob("*/*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, Mapping) or set(raw) != {"catalog", "digest", "source_url", "signature_url", "verified_at", "verification"}:
                    continue
                catalog = raw["catalog"]
                if not isinstance(catalog, Mapping) or set(catalog) != {"schema", "catalog_id", "entries"} or catalog.get("schema") != CATALOG_SCHEMA:
                    continue
                catalog_id = catalog.get("catalog_id")
                if not isinstance(catalog_id, str) or not _IDENTIFIER.fullmatch(catalog_id) or path.parent.name != catalog_id:
                    continue
                entries_raw = catalog.get("entries")
                if not isinstance(entries_raw, list) or not entries_raw or len(entries_raw) > 128:
                    continue
                entries = tuple(_entry(item) for item in entries_raw if isinstance(item, Mapping))
                if len(entries) != len(entries_raw) or len({(item.plugin_id, item.version) for item in entries}) != len(entries):
                    continue
                canonical = canonical_json({"schema": CATALOG_SCHEMA, "catalog_id": catalog_id, "entries": [dict(item) for item in entries_raw]}).encode("utf-8")
                digest = raw["digest"]
                if not isinstance(digest, str) or digest != hashlib.sha256(canonical).hexdigest() or path.stem != digest:
                    continue
                source_url = _https_url(raw["source_url"], label="Catalog URL")
                signature_url = _https_url(raw["signature_url"], label="Catalog signature URL")
                verified_at = raw["verified_at"]
                if not isinstance(verified_at, str) or _catalog_timestamp(verified_at) is None:
                    continue
                verification = raw["verification"]
                if not isinstance(verification, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in verification.items()):
                    continue
                items.append(PluginCatalog(catalog_id=catalog_id, digest=digest, entries=entries, source_url=source_url, signature_url=signature_url, verified_at=verified_at, verification=dict(verification)))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return tuple(items)

    def latest(self, *, catalog_id: str | None = None) -> tuple[PluginCatalog, ...]:
        """Return the latest verified local snapshot per catalog identity.

        Historical snapshots remain on disk for audit, but neither candidate
        projection nor install should accidentally select one based on its
        digest's lexical ordering.  ``verified_at`` is the local successful
        verification timestamp, never a publisher-controlled release time.
        """

        if catalog_id is not None and (not isinstance(catalog_id, str) or not _IDENTIFIER.fullmatch(catalog_id)):
            raise PluginCatalogError("Catalog id is invalid")
        latest_by_id: dict[str, PluginCatalog] = {}
        for catalog in self.list():
            if catalog_id is not None and catalog.catalog_id != catalog_id:
                continue
            previous = latest_by_id.get(catalog.catalog_id)
            if previous is None or _catalog_order_key(catalog) > _catalog_order_key(previous):
                latest_by_id[catalog.catalog_id] = catalog
        return tuple(latest_by_id[item] for item in sorted(latest_by_id))

    def candidates(
        self,
        installed_plugins: tuple[ExecutablePlugin, ...],
        *,
        catalog_id: str | None = None,
    ) -> tuple[PluginCatalogCandidate, ...]:
        """Project staged catalog metadata against local receipts only.

        No version ordering, network request, installation, activation, or
        execution occurs here.  The exact (catalog id, digest, version) tuple
        is retained so a later install remains auditable even when a catalog
        publisher stages a newer snapshot.
        """

        if catalog_id is not None and (not isinstance(catalog_id, str) or not _IDENTIFIER.fullmatch(catalog_id)):
            raise PluginCatalogError("Catalog id is invalid")
        installed_by_id: dict[str, set[str]] = {}
        for plugin in installed_plugins:
            installed_by_id.setdefault(plugin.plugin_id, set()).add(plugin.version)
        candidates: list[PluginCatalogCandidate] = []
        for catalog in self.latest(catalog_id=catalog_id):
            for entry in catalog.entries:
                versions = tuple(sorted(installed_by_id.get(entry.plugin_id, set())))
                if entry.version in versions:
                    continue
                candidates.append(
                    PluginCatalogCandidate(
                        catalog_id=catalog.catalog_id,
                        catalog_digest=catalog.digest,
                        plugin_id=entry.plugin_id,
                        candidate_version=entry.version,
                        description=entry.description,
                        installed_versions=versions,
                    )
                )
        return tuple(candidates)

    def install(self, catalog_id: str, plugin_id: str, *, version: str | None, catalog_digest: str | None = None, plugin_store: ExecutablePluginStore) -> ExecutablePlugin:
        """Install one reviewed entry without a catalog-refresh race."""
        if not isinstance(catalog_id, str) or not _IDENTIFIER.fullmatch(catalog_id):
            raise PluginCatalogError("Catalog id is invalid")
        history = tuple(item for item in self.list() if item.catalog_id == catalog_id)
        if not history:
            raise PluginCatalogError("No verified catalog with that id is staged")
        if catalog_digest is not None:
            if not isinstance(catalog_digest, str) or not _DIGEST.fullmatch(catalog_digest):
                raise PluginCatalogError("Catalog digest is invalid")
            selected = tuple(item for item in history if item.digest == catalog_digest)
            if len(selected) != 1:
                raise PluginCatalogError("Reviewed catalog digest is not staged")
            catalog = selected[0]
        else:
            if len(history) > 1:
                raise PluginCatalogError("Multiple catalog snapshots are staged; specify the reviewed --catalog-digest")
            catalog = history[0]
        entries = [entry for entry in catalog.entries if entry.plugin_id == plugin_id and (version is None or entry.version == version)]
        if len(entries) != 1:
            raise PluginCatalogError("Catalog plugin version was not found")
        entry = entries[0]
        try:
            return plugin_store.install_git(
                entry.repository_url,
                entry.commit,
                subdirectory=entry.subdirectory,
                catalog_provenance={"catalog_id": catalog.catalog_id, "catalog_digest": catalog.digest},
            )
        except PluginLifecycleError as exc:
            raise PluginCatalogError(str(exc)) from exc


def _catalog_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _catalog_order_key(catalog: PluginCatalog) -> tuple[datetime, str]:
    timestamp = _catalog_timestamp(catalog.verified_at)
    assert timestamp is not None
    return timestamp, catalog.digest
