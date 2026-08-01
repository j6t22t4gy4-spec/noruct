"""First-party local document worker for the User Knowledge Runtime.

The worker deliberately has no network client, model download, or third-party
Python dependency.  It is launched as a separate local process so document
parsing failures do not take down the interactive Firm runtime.  DOCX is read
from its XML package with bounded ZIP checks.  On macOS, PDFKit and Vision are
used through the OS-provided Swift runtime; elsewhere a small, bounded digital
PDF text fallback is available and OCR requires an already-installed local
``tesseract`` executable.

This is not an untrusted-document sandbox.  The input contract accepts only a
regular local file already hash-verified by ``KnowledgeIntakeService``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

from .vault import MAX_ASSET_BYTES, MAX_REPRESENTATION_BYTES, sha256_file


REQUEST_SCHEMA = "noruct.local-document-worker-request.v1"
RESPONSE_SCHEMA = "noruct.local-document-worker-response.v1"
PROCESSOR = "noruct-local-document-worker"
PROCESSOR_VERSION = "2"
MAX_ZIP_MEMBERS = 20_000
MAX_ZIP_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_SWIFT_OUTPUT_BYTES = MAX_REPRESENTATION_BYTES
MAX_BASIC_PDF_BYTES = 32 * 1024 * 1024
MAX_IMAGE_METADATA_BYTES = 8 * 1024 * 1024
_WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING_NAMESPACE = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SPREADSHEET_NAMESPACE = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_IMAGE_MEDIA_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/tiff", "image/bmp", "image/webp"}
)


def _bounded_output(command: list[str], *, input_bytes: bytes, timeout_seconds: float) -> bytes:
    """Execute one local platform tool without retaining unbounded stdout in RAM."""

    with tempfile.TemporaryFile(prefix="noruct-local-worker-output-") as output:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "HOME": tempfile.gettempdir(),
            "TMPDIR": tempfile.gettempdir(),
            "NO_PROXY": "*",
            "no_proxy": "*",
            **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if os.environ.get("SYSTEMROOT") else {}),
            **({"WINDIR": os.environ["WINDIR"]} if os.environ.get("WINDIR") else {}),
            **({"COMSPEC": os.environ["COMSPEC"]} if os.environ.get("COMSPEC") else {}),
            **({"PATHEXT": os.environ["PATHEXT"]} if os.environ.get("PATHEXT") else {}),
            **({"TEMP": tempfile.gettempdir(), "TMP": tempfile.gettempdir()} if os.name == "nt" else {}),
            **(
                {"NORUCT_LOCAL_DOCUMENT_SOURCE": os.environ["NORUCT_LOCAL_DOCUMENT_SOURCE"]}
                if os.environ.get("NORUCT_LOCAL_DOCUMENT_SOURCE") else {}
            ),
        }
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env=environment,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        assert process.stdin is not None
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if os.fstat(output.fileno()).st_size > MAX_SWIFT_OUTPUT_BYTES:
                    process.kill()
                    process.wait()
                    raise ValueError("Local document platform tool exceeded its output limit")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise TimeoutError("Local document platform tool timed out")
                time.sleep(min(0.02, remaining))
            if process.returncode != 0:
                raise ValueError("Local document platform tool failed")
            size = os.fstat(output.fileno()).st_size
            if size > MAX_SWIFT_OUTPUT_BYTES:
                raise ValueError("Local document platform tool exceeded its output limit")
            output.seek(0)
            return output.read(MAX_SWIFT_OUTPUT_BYTES + 1)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise


def _swift_source_path() -> str:
    value = os.environ.get("NORUCT_LOCAL_DOCUMENT_SOURCE", "")
    if not value:
        raise ValueError("Local document worker source path is unavailable")
    return value


def _swift_extract(script: str, source: Path, *, timeout_seconds: float) -> str:
    swift = shutil.which("swift")
    if platform.system() != "Darwin" or swift is None:
        raise ValueError("The local macOS document framework is unavailable")
    environment_source = str(source)
    previous = os.environ.get("NORUCT_LOCAL_DOCUMENT_SOURCE")
    os.environ["NORUCT_LOCAL_DOCUMENT_SOURCE"] = environment_source
    try:
        # The script reads only this verified regular file path and produces
        # UTF-8 text.  It contains no URL/network API.
        result = _bounded_output([swift, "-"], input_bytes=script.encode("utf-8"), timeout_seconds=timeout_seconds)
    finally:
        if previous is None:
            os.environ.pop("NORUCT_LOCAL_DOCUMENT_SOURCE", None)
        else:
            os.environ["NORUCT_LOCAL_DOCUMENT_SOURCE"] = previous
    try:
        text = result.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Local document platform tool did not return UTF-8 text") from exc
    return _normalize(text)


_PDFKIT_SCRIPT = r'''
import Foundation
import PDFKit
let environment = ProcessInfo.processInfo.environment
guard let rawPath = environment["NORUCT_LOCAL_DOCUMENT_SOURCE"] else { exit(2) }
let url = URL(fileURLWithPath: rawPath)
guard let document = PDFDocument(url: url) else { exit(3) }
let text = document.string ?? ""
FileHandle.standardOutput.write(text.data(using: .utf8)!)
'''

_VISION_SCRIPT = r'''
import Foundation
import Vision
let environment = ProcessInfo.processInfo.environment
guard let rawPath = environment["NORUCT_LOCAL_DOCUMENT_SOURCE"] else { exit(2) }
let url = URL(fileURLWithPath: rawPath)
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
let handler = VNImageRequestHandler(url: url, options: [:])
do {
    try handler.perform([request])
    let lines = (request.results ?? []).compactMap { observation in
        observation.topCandidates(1).first?.string
    }
    FileHandle.standardOutput.write(lines.joined(separator: "\n").data(using: .utf8)!)
} catch {
    exit(3)
}
'''


def _normalize(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n")).strip()
    if not normalized:
        raise ValueError("Document contains no extractable text")
    payload = normalized.encode("utf-8")
    if len(payload) > MAX_REPRESENTATION_BYTES:
        raise ValueError("Document extraction exceeds the derived-representation limit")
    return normalized


def _safe_docx_member(name: str) -> bool:
    candidate = Path(name.replace("\\", "/"))
    return not candidate.is_absolute() and ".." not in candidate.parts and not name.startswith("/")


def _safe_ooxml_parts(source: Path, selected: list[str]) -> dict[str, bytes]:
    """Read selected OOXML XML parts after one bounded package validation."""

    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_ZIP_MEMBERS:
            raise ValueError("Office package has an invalid member count")
        expanded = 0
        for member in members:
            if not _safe_docx_member(member.filename) or member.flag_bits & 0x1:
                raise ValueError("Office package contains an unsafe member")
            if member.is_dir():
                continue
            expanded += member.file_size
            if expanded > MAX_ZIP_EXPANDED_BYTES or member.file_size > MAX_REPRESENTATION_BYTES:
                raise ValueError("Office package exceeds its bounded expansion limit")
        available = {member.filename for member in members if not member.is_dir()}
        result: dict[str, bytes] = {}
        for name in selected:
            if name in available:
                result[name] = archive.read(name)
    return result


def _safe_xml(raw: bytes, *, label: str) -> ElementTree.Element:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError(f"{label} XML declarations are not supported")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError(f"{label} XML is invalid") from exc


def _docx_text(source: Path) -> str:
    parts = _safe_ooxml_parts(source, ["word/document.xml"])
    raw = parts.get("word/document.xml")
    if raw is None:
        raise ValueError("DOCX package has no main document XML")
    root = _safe_xml(raw, label="DOCX main document")
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_WORD_NAMESPACE}p"):
        fragments: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_WORD_NAMESPACE}t" and node.text:
                fragments.append(node.text)
            elif node.tag == f"{_WORD_NAMESPACE}tab":
                fragments.append("\t")
            elif node.tag in {f"{_WORD_NAMESPACE}br", f"{_WORD_NAMESPACE}cr"}:
                fragments.append("\n")
        value = "".join(fragments).strip()
        if value:
            paragraphs.append(value)
    return _normalize("\n\n".join(paragraphs))


def _pptx_slide_key(name: str) -> tuple[int, str]:
    match = re.search(r"slide(\d+)\.xml$", name)
    return (int(match.group(1)) if match else 1_000_000, name)


def _pptx_text(source: Path) -> str:
    with zipfile.ZipFile(source) as archive:
        slide_names = sorted(
            (
                member.filename
                for member in archive.infolist()
                if member.filename.startswith("ppt/slides/slide") and member.filename.endswith(".xml")
            ),
            key=_pptx_slide_key,
        )
    if not slide_names:
        raise ValueError("PPTX package has no slide XML")
    parts = _safe_ooxml_parts(source, slide_names)
    slides: list[str] = []
    for ordinal, name in enumerate(slide_names, start=1):
        raw = parts.get(name)
        if raw is None:
            continue
        root = _safe_xml(raw, label="PPTX slide")
        fragments = [node.text or "" for node in root.iter(f"{_DRAWING_NAMESPACE}t")]
        text = "".join(fragments).strip()
        if text:
            slides.append(f"# Slide {ordinal}\n\n{text}")
    return _normalize("\n\n".join(slides))


def _xlsx_cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_SPREADSHEET_NAMESPACE}t")).strip()
    value = cell.findtext(f"{_SPREADSHEET_NAMESPACE}v", default="").strip()
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError):
            return ""
    return value


def _xlsx_text(source: Path) -> str:
    with zipfile.ZipFile(source) as archive:
        names = [member.filename for member in archive.infolist() if not member.is_dir()]
    sheet_names = sorted(
        (name for name in names if name.startswith("xl/worksheets/") and name.endswith(".xml")),
        key=lambda value: value.casefold(),
    )
    if not sheet_names:
        raise ValueError("XLSX package has no worksheet XML")
    parts = _safe_ooxml_parts(source, ["xl/sharedStrings.xml", *sheet_names])
    shared_strings: list[str] = []
    raw_strings = parts.get("xl/sharedStrings.xml")
    if raw_strings is not None:
        root = _safe_xml(raw_strings, label="XLSX shared strings")
        for item in root.iter(f"{_SPREADSHEET_NAMESPACE}si"):
            shared_strings.append("".join(node.text or "" for node in item.iter(f"{_SPREADSHEET_NAMESPACE}t")).strip())
    sheets: list[str] = []
    for ordinal, name in enumerate(sheet_names, start=1):
        raw = parts.get(name)
        if raw is None:
            continue
        root = _safe_xml(raw, label="XLSX worksheet")
        rows: list[str] = []
        for row in root.iter(f"{_SPREADSHEET_NAMESPACE}row"):
            values = [
                value
                for cell in row.findall(f"{_SPREADSHEET_NAMESPACE}c")
                if (value := _xlsx_cell_text(cell, shared_strings))
            ]
            if values:
                rows.append(" | ".join(values))
        if rows:
            sheets.append(f"# Worksheet {ordinal}\n\n" + "\n".join(rows))
    return _normalize("\n\n".join(sheets))


def _pdf_literal_strings(stream: bytes) -> list[str]:
    """Extract literal PDF string operands while honoring simple escapes."""

    values: list[str] = []
    cursor = 0
    while cursor < len(stream):
        if stream[cursor] != 0x28:  # '('
            cursor += 1
            continue
        cursor += 1
        depth = 1
        payload = bytearray()
        while cursor < len(stream) and depth:
            current = stream[cursor]
            cursor += 1
            if current == 0x5C:  # '\\'
                if cursor >= len(stream):
                    break
                escaped = stream[cursor]
                cursor += 1
                simple = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f"}
                if escaped in simple:
                    payload.extend(simple[escaped])
                elif escaped in b"()\\":
                    payload.append(escaped)
                elif 48 <= escaped <= 55:
                    digits = bytes([escaped])
                    while cursor < len(stream) and len(digits) < 3 and 48 <= stream[cursor] <= 55:
                        digits += bytes([stream[cursor]])
                        cursor += 1
                    payload.append(int(digits, 8))
                elif escaped in (10, 13):
                    if escaped == 13 and cursor < len(stream) and stream[cursor] == 10:
                        cursor += 1
                else:
                    payload.append(escaped)
            elif current == 0x28:
                depth += 1
                payload.append(current)
            elif current == 0x29:
                depth -= 1
                if depth:
                    payload.append(current)
            else:
                payload.append(current)
        if depth == 0 and payload:
            values.append(payload.decode("latin-1", errors="replace"))
    return values


def _pdf_streams(payload: bytes) -> list[bytes]:
    """Read small PDF content streams without interpreting object references."""

    streams: list[bytes] = []
    pattern = re.compile(rb"<<(?:[^>]|>(?!>))*>>\s*stream\r?\n", re.DOTALL)
    for match in pattern.finditer(payload):
        end = payload.find(b"endstream", match.end())
        if end < 0:
            continue
        dictionary = match.group(0)
        raw = payload[match.end():end].rstrip(b"\r\n")
        if b"/FlateDecode" in dictionary:
            try:
                decompressor = zlib.decompressobj()
                raw = decompressor.decompress(raw, MAX_REPRESENTATION_BYTES + 1)
                raw += decompressor.flush(MAX_REPRESENTATION_BYTES + 1 - len(raw))
            except zlib.error:
                continue
        if len(raw) <= MAX_REPRESENTATION_BYTES:
            streams.append(raw)
    return streams


def _basic_pdf_text(source: Path) -> str:
    if source.stat().st_size > MAX_BASIC_PDF_BYTES:
        raise ValueError("PDF needs a platform extractor; bounded fallback supports files up to 32 MiB")
    payload = source.read_bytes()
    if not payload.startswith(b"%PDF-"):
        raise ValueError("PDF header is invalid")
    fragments: list[str] = []
    for stream in _pdf_streams(payload):
        # Only text-showing streams are candidates.  This avoids treating
        # metadata or binary objects as human-readable knowledge.
        if b" Tj" not in stream and b" TJ" not in stream and b"'" not in stream:
            continue
        fragments.extend(_pdf_literal_strings(stream))
    return _normalize("\n".join(fragments))


def _png_metadata(source: Path) -> list[str]:
    payload = source.read_bytes()
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return []
    cursor = 8
    values: list[str] = []
    while cursor + 12 <= len(payload):
        size = int.from_bytes(payload[cursor:cursor + 4], "big")
        kind = payload[cursor + 4:cursor + 8]
        end = cursor + 12 + size
        if end > len(payload) or size > 1_000_000:
            break
        data = payload[cursor + 8:cursor + 8 + size]
        if kind == b"tEXt" and b"\x00" in data:
            _, value = data.split(b"\x00", 1)
            values.append(value.decode("latin-1", errors="replace"))
        elif kind == b"iTXt":
            parts = data.split(b"\x00", 5)
            if len(parts) == 6 and parts[1] == b"\x00":
                values.append(parts[5].decode("utf-8", errors="replace"))
        if kind == b"IEND":
            break
        cursor = end
    return [value for value in values if value.strip()]


def _image_metadata(source: Path, media_type: str) -> str | None:
    if source.stat().st_size > MAX_IMAGE_METADATA_BYTES:
        return None
    if media_type == "image/png":
        values = _png_metadata(source)
        if values:
            return _normalize("\n".join(values))
    return None


def _tesseract_extract(source: Path, *, timeout_seconds: float) -> str:
    configured = os.environ.get("NORUCT_LOCAL_TESSERACT", "").strip()
    executable = configured or shutil.which("tesseract")
    if executable is None:
        raise ValueError("No local OCR capability is installed for this image")
    candidate = Path(executable).expanduser()
    if configured and (not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file()):
        raise ValueError("Configured local OCR executable is unavailable or unsafe")
    result = _bounded_output(
        [executable, str(source), "stdout"],
        input_bytes=b"",
        timeout_seconds=timeout_seconds,
    )
    return _normalize(result.decode("utf-8", errors="strict"))


def extract(source: Path, *, media_type: str, timeout_seconds: float) -> tuple[str, str]:
    if media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _docx_text(source), "docx-stdlib"
    if media_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        return _pptx_text(source), "pptx-stdlib"
    if media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return _xlsx_text(source), "xlsx-stdlib"
    if media_type == "application/pdf":
        if platform.system() == "Darwin" and shutil.which("swift"):
            try:
                return _swift_extract(_PDFKIT_SCRIPT, source, timeout_seconds=timeout_seconds), "macos-pdfkit"
            except (OSError, TimeoutError, ValueError):
                # A digital PDF can still be useful on systems where PDFKit
                # rejects an otherwise simple document.  Fall back rather
                # than declaring a parser success for encrypted/scanned PDFs.
                pass
        return _basic_pdf_text(source), "pdf-basic-text"
    if media_type in _IMAGE_MEDIA_TYPES:
        if platform.system() == "Darwin" and shutil.which("swift"):
            try:
                return _swift_extract(_VISION_SCRIPT, source, timeout_seconds=timeout_seconds), "macos-vision-ocr"
            except (OSError, TimeoutError, ValueError):
                pass
        metadata = _image_metadata(source, media_type)
        if metadata is not None:
            return metadata, "image-embedded-metadata"
        return _tesseract_extract(source, timeout_seconds=timeout_seconds), "local-tesseract-ocr"
    raise ValueError("No first-party local document worker supports this media type")


def _request() -> dict[str, object]:
    raw = sys.stdin.buffer.read(128 * 1024)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Local document worker request is invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != REQUEST_SCHEMA:
        raise ValueError("Local document worker request schema is invalid")
    return value


def main() -> int:
    try:
        request = _request()
        raw_path = request.get("source_path")
        source_hash = request.get("source_sha256")
        media_type = request.get("media_type")
        timeout_seconds = request.get("timeout_seconds")
        if (
            not isinstance(raw_path, str)
            or not isinstance(source_hash, str)
            or len(source_hash) != 64
            or any(character not in "0123456789abcdef" for character in source_hash)
            or not isinstance(media_type, str)
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
            or timeout_seconds > 600
        ):
            raise ValueError("Local document worker request fields are invalid")
        source = Path(raw_path)
        if source.is_symlink() or not source.is_file():
            raise ValueError("Local document worker source must be a regular file")
        observed_hash, _ = sha256_file(source, max_bytes=MAX_ASSET_BYTES)
        if observed_hash != source_hash:
            raise ValueError("Local document worker source hash changed")
        markdown, profile = extract(source, media_type=media_type, timeout_seconds=float(timeout_seconds))
        response: dict[str, object] = {
            "schema_version": RESPONSE_SCHEMA,
            "status": "SUCCESS",
            "source_sha256": source_hash,
            "processor": PROCESSOR,
            "processor_version": PROCESSOR_VERSION,
            "profile": profile,
            "local_only": True,
            "markdown": markdown,
        }
    except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        response = {
            "schema_version": RESPONSE_SCHEMA,
            "status": "FAILED",
            "error": str(exc)[:2_000],
            "local_only": True,
        }
    encoded = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REPRESENTATION_BYTES + 64 * 1024:
        encoded = json.dumps(
            {
                "schema_version": RESPONSE_SCHEMA,
                "status": "FAILED",
                "error": "Local document worker response exceeds its output limit",
                "local_only": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
