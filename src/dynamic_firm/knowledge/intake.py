from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .models import AssetStatus, DerivedRepresentation, IntakeResult, ProcessingStatus
from .store import KnowledgeStore
from .vault import KnowledgeVault, MAX_ASSET_BYTES, MAX_REPRESENTATION_BYTES, sha256_file


_TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".html",
        ".htm",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".sql",
    }
)
_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/toml",
        "application/x-yaml",
    }
)
_DOCLING_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/html",
        "image/png",
        "image/jpeg",
        "image/tiff",
        "image/bmp",
        "image/webp",
    }
)
_REGISTERED_DOCLING_COMMIT = "9b51f4f857176cdd95cef53e2ec7f5f32ffbc6a5"
_REGISTERED_DOCLING_WHEEL_SHA256 = "da5b91e7c5c4028459c533596a4d8f1579212000673b564751c66e1daae0ddbd"
_MAX_WORKER_BYTES = 64 * 1024 * 1024
_WORKER_RESPONSE_OVERHEAD_BYTES = 64 * 1024


def _run_bounded_worker(
    command: Sequence[str | Path],
    request: bytes,
    *,
    timeout_seconds: float,
    environment: dict[str, str],
    max_output_bytes: int,
    working_directory: Path,
) -> tuple[int, bytes]:
    """Run a configured worker with bounded memory, time, and captured output."""

    with tempfile.TemporaryFile(prefix="noruct-docling-output-") as output:
        process = subprocess.Popen(
            [str(value) for value in command],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env=environment,
            cwd=working_directory,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        assert process.stdin is not None
        try:
            process.stdin.write(request)
            process.stdin.close()
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if os.fstat(output.fileno()).st_size > max_output_bytes:
                    process.kill()
                    process.wait()
                    raise ValueError("Docling worker response exceeds its output limit")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(" ".join(str(value) for value in command), timeout_seconds)
                time.sleep(min(0.02, remaining))
            size = os.fstat(output.fileno()).st_size
            if size > max_output_bytes:
                raise ValueError("Docling worker response exceeds its output limit")
            output.seek(0)
            return process.returncode, output.read(max_output_bytes + 1)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    markdown: str
    processor: str
    processor_version: str


class DocumentExtractor(Protocol):
    name: str

    def supports(self, source: Path, media_type: str) -> bool: ...

    def extract(
        self,
        source: Path,
        *,
        media_type: str,
        timeout_seconds: float,
    ) -> ExtractedDocument: ...


class PlainTextExtractor:
    name = "noruct-plain-text"
    version = "1"

    def supports(self, source: Path, media_type: str) -> bool:
        return (
            source.suffix.lower() in _TEXT_SUFFIXES
            or media_type.startswith("text/")
            or media_type in _TEXT_MEDIA_TYPES
        )

    def extract(
        self,
        source: Path,
        *,
        media_type: str,
        timeout_seconds: float,
    ) -> ExtractedDocument:
        del media_type, timeout_seconds
        if source.stat().st_size > MAX_REPRESENTATION_BYTES:
            raise ValueError("Text asset exceeds the bounded derived-representation limit")
        payload = source.read_bytes()
        decoded: str | None = None
        for encoding in ("utf-8-sig", "utf-16"):
            try:
                decoded = payload.decode(encoding)
                break
            except UnicodeError:
                continue
        if decoded is None:
            raise ValueError("Text asset encoding is unsupported; the original remains preserved")
        normalized = decoded.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError("Text asset has no extractable content")
        return ExtractedDocument(normalized, self.name, self.version)


class DoclingExtractor:
    """Adapter for an exact-pinned, separately locked Docling worker.

    The public in-process ``DocumentConverter`` path is intentionally not used:
    the registered commit differs from its nearest PyPI wheel, eagerly imports
    a large model closure, and can download model assets even when remote
    services/plugins are disabled.  A worker is admitted only with a local
    manifest binding source, dependency lock, assets, and offline policy.
    """

    name = "docling"

    def __init__(
        self,
        worker_command: str | Path | None = None,
        manifest_path: str | Path | None = None,
    ) -> None:
        configured = worker_command or os.environ.get("NORUCT_DOCLING_WORKER", "")
        self.worker_command = Path(configured).expanduser() if str(configured).strip() else None
        configured_manifest = manifest_path or os.environ.get("NORUCT_DOCLING_WORKER_MANIFEST", "")
        self.manifest_path = (
            Path(configured_manifest).expanduser()
            if str(configured_manifest).strip()
            else (
                self.worker_command.with_name(f"{self.worker_command.name}.manifest.json")
                if self.worker_command is not None
                else None
            )
        )

    def _manifest(self) -> dict[str, object]:
        if self.worker_command is None or self.manifest_path is None:
            raise ValueError("No exact-pinned Docling worker is configured")
        if self.worker_command.is_symlink() or not self.worker_command.is_file():
            raise ValueError("Docling worker command must be a regular non-symlink file")
        if not os.access(self.worker_command, os.X_OK):
            raise ValueError("Docling worker command is not executable")
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            raise ValueError("Docling worker manifest is missing or unsafe")
        if self.manifest_path.stat().st_size > 64 * 1024:
            raise ValueError("Docling worker manifest exceeds its size limit")
        value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Docling worker manifest must be a JSON object")
        required = {
            "schema_version": "noruct.docling-worker-manifest.v1",
            "source_commit": _REGISTERED_DOCLING_COMMIT,
            "offline_required": True,
            "remote_io_allowed": False,
            "trust_model": "operator-trusted-local-executable",
            "source_wheel_sha256": _REGISTERED_DOCLING_WHEEL_SHA256,
        }
        for key, expected in required.items():
            if value.get(key) != expected:
                raise ValueError(f"Docling worker manifest has an invalid {key}")
        for digest_name in (
            "worker_sha256",
            "dependency_lock_sha256",
            "asset_manifest_sha256",
        ):
            digest = str(value.get(digest_name) or "").lower()
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError(f"Docling worker manifest has an invalid {digest_name}")
        worker_hash, _ = sha256_file(self.worker_command, max_bytes=_MAX_WORKER_BYTES)
        if worker_hash != value["worker_sha256"]:
            raise ValueError("Docling worker executable does not match its manifest")
        profiles = value.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            raise ValueError("Docling worker manifest declares no conversion profile")
        return value

    @property
    def available(self) -> bool:
        try:
            self._manifest()
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return True

    @property
    def version(self) -> str:
        try:
            value = self._manifest()
        except (OSError, ValueError, json.JSONDecodeError):
            return "unavailable"
        return f"{value.get('worker_version', '1')}+{_REGISTERED_DOCLING_COMMIT[:12]}"

    def supports(self, source: Path, media_type: str) -> bool:
        del source
        return media_type in _DOCLING_MEDIA_TYPES

    def extract(
        self,
        source: Path,
        *,
        media_type: str,
        timeout_seconds: float,
    ) -> ExtractedDocument:
        manifest = self._manifest()
        assert self.worker_command is not None
        source_hash, _ = sha256_file(source, max_bytes=MAX_ASSET_BYTES)
        worker_hash = str(manifest["worker_sha256"])
        request = {
            "schema_version": "noruct.docling-worker-request.v1",
            "source_path": str(source),
            "source_sha256": source_hash,
            "media_type": media_type,
            "max_output_bytes": MAX_REPRESENTATION_BYTES,
            "network_policy": "DENY",
            "worker_sha256": worker_hash,
        }
        inherited_environment = os.environ
        environment = {
            key: inherited_environment[key]
            for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC")
            if inherited_environment.get(key)
        }
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "DOCLING_OFFLINE": "1",
                "NO_PROXY": "*",
                "no_proxy": "*",
            }
        )
        with tempfile.TemporaryDirectory(prefix="noruct-docling-worker-") as temporary:
            isolated = Path(temporary)
            environment.update({"HOME": str(isolated), "TMPDIR": str(isolated)})
            returncode, raw_response = _run_bounded_worker(
                (self.worker_command,),
                json.dumps(request, ensure_ascii=True).encode("utf-8"),
                timeout_seconds=timeout_seconds,
                environment=environment,
                max_output_bytes=MAX_REPRESENTATION_BYTES + _WORKER_RESPONSE_OVERHEAD_BYTES,
                working_directory=isolated,
            )
        if returncode != 0:
            raise ValueError(f"Docling worker failed with exit code {returncode}")
        response = json.loads(raw_response.decode("utf-8"))
        if not isinstance(response, dict) or response.get("schema_version") != "noruct.docling-worker-response.v1":
            raise ValueError("Docling worker returned an invalid response envelope")
        if (
            response.get("source_sha256") != source_hash
            or response.get("worker_sha256") != worker_hash
            or response.get("remote_io_performed") is not False
        ):
            raise ValueError("Docling worker source or offline receipt is invalid")
        if response.get("source_commit") != manifest["source_commit"]:
            raise ValueError("Docling worker response does not match its source manifest")
        if response.get("status") != "SUCCESS":
            raise ValueError(f"Docling worker did not fully convert the asset: {response.get('status')}")
        markdown = str(response.get("markdown") or "").strip()
        if not markdown:
            raise ValueError("Docling worker produced no extractable text")
        if len(markdown.encode("utf-8")) > MAX_REPRESENTATION_BYTES:
            raise ValueError("Docling worker representation exceeds its output limit")
        observed_worker_hash, _ = sha256_file(
            self.worker_command, max_bytes=_MAX_WORKER_BYTES
        )
        if observed_worker_hash != worker_hash:
            raise ValueError("Docling worker executable changed during conversion")
        return ExtractedDocument(markdown, self.name, self.version)


class LocalDocumentExtractor:
    """Built-in, local-only worker for OOXML, digital PDF, and image OCR.

    It has no downloadable model or network path.  The worker uses the Python
    standard library for DOCX and narrow digital-PDF fallback; on macOS it can
    use the already-installed PDFKit and Vision frameworks.  Other platforms
    may use a pre-existing local ``tesseract`` executable for images.
    """

    name = "local-document"
    version = "1"

    @staticmethod
    def _local_tesseract_available() -> bool:
        configured = os.environ.get("NORUCT_LOCAL_TESSERACT", "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            return candidate.is_absolute() and not candidate.is_symlink() and candidate.is_file()
        return shutil.which("tesseract") is not None

    @classmethod
    def capabilities(cls) -> dict[str, object]:
        """Return local extraction routes without probing a document or network."""

        macos_frameworks = platform.system() == "Darwin" and shutil.which("swift") is not None
        return {
            "schema_version": "noruct.local-document-capabilities.v1",
            "processor": cls.name,
            "processor_version": cls.version,
            "execution_scope": "local-process-only",
            "network_access": "none-by-design",
            "docx": "builtin-stdlib",
            "pptx": "builtin-stdlib",
            "xlsx": "builtin-stdlib",
            "pdf": "macos-pdfkit" if macos_frameworks else "bounded-digital-pdf-fallback",
            "image_ocr": (
                "macos-vision" if macos_frameworks else ("local-tesseract" if cls._local_tesseract_available() else "not-installed")
            ),
            "image_metadata": "builtin-png-text",
            "limitations": (
                "Scanned or encrypted PDFs need a local OCR-capable route; image OCR requires macOS Vision or a local tesseract executable."
            ),
        }

    def supports(self, source: Path, media_type: str) -> bool:
        del source
        document_types = {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        if media_type in document_types:
            return True
        if media_type == "image/png":
            return True  # bounded embedded text is available without an OCR engine
        return media_type in {"image/jpeg", "image/tiff", "image/bmp", "image/webp"} and (
            (platform.system() == "Darwin" and shutil.which("swift") is not None)
            or self._local_tesseract_available()
        )

    def extract(
        self,
        source: Path,
        *,
        media_type: str,
        timeout_seconds: float,
    ) -> ExtractedDocument:
        source_hash, _ = sha256_file(source, max_bytes=MAX_ASSET_BYTES)
        request = {
            "schema_version": "noruct.local-document-worker-request.v1",
            "source_path": str(source),
            "source_sha256": source_hash,
            "media_type": media_type,
            "timeout_seconds": min(timeout_seconds, 600.0),
        }
        inherited = os.environ
        environment = {
            key: inherited[key]
            for key in ("PATH", "PYTHONPATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "NORUCT_LOCAL_TESSERACT")
            if inherited.get(key)
        }
        if environment.get("PYTHONPATH"):
            environment["PYTHONPATH"] = os.pathsep.join(
                str(Path(entry).expanduser().resolve())
                for entry in environment["PYTHONPATH"].split(os.pathsep)
                if entry
            )
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "NO_PROXY": "*",
                "no_proxy": "*",
            }
        )
        with tempfile.TemporaryDirectory(prefix="noruct-local-document-worker-") as temporary:
            isolated = Path(temporary)
            environment.update({"HOME": str(isolated), "TMPDIR": str(isolated), "TEMP": str(isolated), "TMP": str(isolated)})
            returncode, raw_response = _run_bounded_worker(
                (sys.executable, "-m", "dynamic_firm.knowledge.local_worker"),
                json.dumps(request, ensure_ascii=True).encode("utf-8"),
                timeout_seconds=min(timeout_seconds + 10.0, 610.0),
                environment=environment,
                max_output_bytes=MAX_REPRESENTATION_BYTES + _WORKER_RESPONSE_OVERHEAD_BYTES,
                working_directory=isolated,
            )
        if returncode != 0:
            raise ValueError(f"Local document worker failed with exit code {returncode}")
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Local document worker returned invalid JSON") from exc
        if (
            not isinstance(response, dict)
            or response.get("schema_version") != "noruct.local-document-worker-response.v1"
            or response.get("local_only") is not True
        ):
            raise ValueError("Local document worker returned an invalid response envelope")
        if response.get("status") != "SUCCESS":
            detail = str(response.get("error") or "unknown local extraction failure")
            raise ValueError(f"Local document worker did not extract the asset: {detail}")
        if response.get("source_sha256") != source_hash:
            raise ValueError("Local document worker source receipt is invalid")
        if response.get("processor") != "noruct-local-document-worker":
            raise ValueError("Local document worker processor identity is invalid")
        markdown = str(response.get("markdown") or "").strip()
        if not markdown or len(markdown.encode("utf-8")) > MAX_REPRESENTATION_BYTES:
            raise ValueError("Local document worker returned invalid extracted text")
        profile = str(response.get("profile") or "unknown")
        if len(profile.encode("utf-8")) > 256:
            raise ValueError("Local document worker profile is invalid")
        observed_hash, _ = sha256_file(source, max_bytes=MAX_ASSET_BYTES)
        if observed_hash != source_hash:
            raise ValueError("Knowledge Asset changed during local document extraction")
        return ExtractedDocument(markdown, self.name, f"{self.version}+{profile}")


def detect_media_type(path: Path) -> str:
    known = {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".md": "text/markdown",
    }
    return known.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _chunks(content: str, *, maximum_chars: int = 2400) -> tuple[dict[str, object], ...]:
    if maximum_chars < 256:
        raise ValueError("Knowledge chunk bound is too small")
    chunks: list[dict[str, object]] = []
    cursor = 0
    length = len(content)
    while cursor < length:
        proposed = min(length, cursor + maximum_chars)
        end = proposed
        if proposed < length:
            paragraph = content.rfind("\n\n", cursor + 1, proposed)
            newline = content.rfind("\n", cursor + 1, proposed)
            boundary = paragraph + 2 if paragraph >= cursor + maximum_chars // 2 else newline + 1
            if boundary > cursor:
                end = boundary
        value = content[cursor:end].strip()
        if value:
            leading = len(content[cursor:end]) - len(content[cursor:end].lstrip())
            trailing_end = end - (len(content[cursor:end]) - len(content[cursor:end].rstrip()))
            start = cursor + leading
            actual_end = max(start, trailing_end)
            chunks.append(
                {
                    "content": value,
                    "content_hash": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                    "char_start": start,
                    "char_end": actual_end,
                    "location": {
                        "representation_char_start": start,
                        "representation_char_end": actual_end,
                    },
                }
            )
        cursor = max(end, cursor + 1)
    return tuple(chunks)


class KnowledgeIntakeService:
    def __init__(
        self,
        store: KnowledgeStore,
        vault: KnowledgeVault,
        *,
        extractors: tuple[DocumentExtractor, ...] | None = None,
    ) -> None:
        self.store = store
        self.vault = vault
        self.extractors = extractors or (
            PlainTextExtractor(),
            LocalDocumentExtractor(),
            DoclingExtractor(),
        )

    def ingest(
        self,
        source_path: str | Path,
        *,
        title: str = "",
        origin: str = "local-file",
        access_scope: str = "private",
        labels: tuple[str, ...] = (),
        parent_asset_id: str | None = None,
        processor: str = "auto",
        timeout_seconds: float = 120.0,
    ) -> IntakeResult:
        if timeout_seconds <= 0:
            raise ValueError("Knowledge processing timeout must be positive")
        source, content_hash, byte_size = self.vault.inspect_source(source_path)
        normalized_scope = access_scope.strip() or "private"
        existing = self.store.asset_by_hash(content_hash, normalized_scope)
        if existing is not None:
            current_representation = self.store.latest_representation(existing.asset_id)
            if current_representation is not None and existing.status == AssetStatus.READY:
                return IntakeResult(
                    asset=existing,
                    representation=current_representation,
                    processing_status=ProcessingStatus.READY,
                    duplicate=True,
                    messages=("Identical content already exists; the existing immutable asset was reused.",),
                )
        stored = self.vault.store_source(
            source,
            content_hash=content_hash,
            byte_size=byte_size,
            access_scope=normalized_scope,
        )
        try:
            asset, duplicate = self.store.create_asset(
                content_hash=content_hash,
                original_name=source.name,
                title=title.strip() or source.stem,
                media_type=detect_media_type(source),
                byte_size=byte_size,
                vault_relative_path=stored.relative_path,
                origin=origin.strip() or "local-file",
                access_scope=normalized_scope,
                labels=labels,
                parent_asset_id=parent_asset_id,
            )
        except BaseException:
            if stored.created and self.store.asset_by_hash(content_hash, normalized_scope) is None:
                self.vault.remove_if_matches(stored)
            raise
        existing = self.store.latest_representation(asset.asset_id)
        if duplicate and existing is not None and asset.status == AssetStatus.READY:
            return IntakeResult(
                asset=asset,
                representation=existing,
                processing_status=ProcessingStatus.READY,
                duplicate=True,
                messages=("Identical content already exists; the existing immutable asset was reused.",),
            )
        return self.process(
            asset.asset_id,
            processor=processor,
            timeout_seconds=timeout_seconds,
            duplicate=duplicate,
        )

    def process(
        self,
        asset_id: str,
        *,
        processor: str = "auto",
        timeout_seconds: float = 120.0,
        duplicate: bool = False,
    ) -> IntakeResult:
        if timeout_seconds <= 0:
            raise ValueError("Knowledge processing timeout must be positive")
        asset = self.store.asset(asset_id)
        if asset is None:
            raise ValueError(f"Knowledge Asset was not found: {asset_id}")
        source = self.vault.resolve(asset.vault_relative_path)
        try:
            observed_hash, observed_size = sha256_file(source, max_bytes=MAX_ASSET_BYTES)
            if observed_hash != asset.content_hash or observed_size != asset.byte_size:
                raise ValueError("Knowledge Asset Vault integrity check failed")
        except (OSError, ValueError) as exc:
            updated = self.store.set_asset_processing(
                asset_id,
                status=AssetStatus.PROCESSING_FAILED,
                processor="integrity-check",
                error=str(exc),
            )
            return IntakeResult(
                asset=updated,
                representation=None,
                processing_status=ProcessingStatus.FAILED,
                duplicate=duplicate,
                messages=(str(exc), "The unsafe Vault object was not processed."),
            )
        selected: DocumentExtractor | None = None
        normalized = processor.strip().lower()
        if normalized not in {"auto", "stored-only", "plain-text", "local-document", "docling"}:
            raise ValueError(
                "Knowledge processor must be auto, stored-only, plain-text, local-document, or docling"
            )
        if normalized != "stored-only":
            for extractor in self.extractors:
                selector = "plain-text" if isinstance(extractor, PlainTextExtractor) else extractor.name
                if normalized not in {"auto", selector}:
                    continue
                if extractor.supports(source, asset.media_type):
                    if normalized == "auto" and isinstance(extractor, DoclingExtractor) and not extractor.available:
                        continue
                    selected = extractor
                    break
        if selected is None:
            updated = self.store.set_asset_processing(
                asset_id,
                status=AssetStatus.STORED_UNPROCESSED,
                error="No installed offline processor supports this asset type",
            )
            hint = (
                "Original preserved without semantic extraction. For DOCX/PDF/images, run "
                "`noruct knowledge process ASSET_ID --processor local-document`; image OCR "
                "requires macOS Vision or an already-installed local tesseract executable."
            )
            return IntakeResult(
                asset=updated,
                representation=None,
                processing_status=ProcessingStatus.STORED_UNPROCESSED,
                duplicate=duplicate,
                messages=(hint,),
            )
        self.store.set_asset_processing(
            asset_id,
            status=AssetStatus.PROCESSING,
            processor=selected.name,
            processor_version=getattr(selected, "version", ""),
        )
        try:
            extracted = selected.extract(
                source,
                media_type=asset.media_type,
                timeout_seconds=timeout_seconds,
            )
            final_hash, final_size = sha256_file(source, max_bytes=MAX_ASSET_BYTES)
            if final_hash != asset.content_hash or final_size != asset.byte_size:
                raise ValueError("Knowledge Asset changed during document extraction")
            derived = self.vault.write_representation(asset_id, extracted.markdown)
            chunks = _chunks(extracted.markdown)
            try:
                representation = self.store.create_representation(
                    asset_id=asset_id,
                    kind="normalized_markdown",
                    media_type="text/markdown",
                    content_hash=derived.content_hash,
                    byte_size=derived.byte_size,
                    vault_relative_path=derived.relative_path,
                    processor=extracted.processor,
                    processor_version=extracted.processor_version,
                    chunks=chunks,
                )
            except BaseException:
                if derived.created:
                    self.vault.remove_if_matches(derived)
                raise
        except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as exc:
            updated = self.store.set_asset_processing(
                asset_id,
                status=AssetStatus.PROCESSING_FAILED,
                processor=selected.name,
                processor_version=getattr(selected, "version", ""),
                error=str(exc),
            )
            return IntakeResult(
                asset=updated,
                representation=None,
                processing_status=ProcessingStatus.FAILED,
                duplicate=duplicate,
                messages=(str(exc), "Original asset remains preserved and can be reprocessed."),
            )
        updated = self.store.set_asset_processing(
            asset_id,
            status=AssetStatus.READY,
            processor=extracted.processor,
            processor_version=extracted.processor_version,
        )
        return IntakeResult(
            asset=updated,
            representation=representation,
            processing_status=ProcessingStatus.READY,
            duplicate=duplicate,
            messages=(f"Derived {len(chunks)} bounded searchable chunk(s).",),
        )
