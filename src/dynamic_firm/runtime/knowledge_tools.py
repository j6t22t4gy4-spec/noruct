"""Company employee tools for the local Knowledge Runtime.

These tools are intentionally small: they expose the existing first-party
Knowledge service through the normal Noruct ToolExecutor so natural-language
requests cannot bypass approval, audit, or workspace boundaries.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from dynamic_firm.knowledge.service import UserKnowledgeService
from dynamic_firm.knowledge.store import KnowledgeStore, knowledge_runtime_paths
from dynamic_firm.knowledge.vault import KnowledgeVault
from dynamic_firm.runtime.models import IdempotencyMode, ToolEffect, ToolRisk, to_primitive
from dynamic_firm.runtime.ports import CancellationToken
from dynamic_firm.runtime.tools import ToolDefinition, ToolValidationError


def _string(arguments: Mapping[str, Any], name: str, *, allow_empty: bool = False) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ToolValidationError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > 8_000:
        raise ToolValidationError(f"{name} exceeds the input bound")
    return value.strip()


def _result(value: object) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class KnowledgeRuntimeTools:
    def __init__(self, *, state_path: Path, workspace: Path) -> None:
        self.state_path = state_path.expanduser().resolve()
        self.workspace = workspace.expanduser().resolve()

    def _service(self) -> tuple[KnowledgeStore, UserKnowledgeService]:
        database, vault = knowledge_runtime_paths(self.state_path)
        store = KnowledgeStore(database)
        return store, UserKnowledgeService(store, KnowledgeVault(vault))

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._recall(), self._folder_open(), self._remember(), self._ingest()

    def _recall(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) - {"query", "limit"}:
                raise ToolValidationError("knowledge_recall received an unknown argument")
            limit = int(arguments.get("limit", 5))
            if limit < 1 or limit > 10:
                raise ToolValidationError("limit must be between 1 and 10")
            return {"query": _string(arguments, "query"), "limit": limit}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            store, service = await asyncio.to_thread(self._service)
            try:
                pack = await asyncio.to_thread(
                    service.build_evidence_pack,
                    str(arguments["query"]),
                    limit=int(arguments["limit"]),
                    persist=False,
                )
                return _result(pack)
            finally:
                store.close()

        return ToolDefinition(
            name="knowledge_recall",
            description="Build a bounded local evidence brief from the user's Knowledge Runtime.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=lambda a: f"knowledge:recall:{a['query']}",
            handler=handle,
            parallel_safe=True,
        )

    def _remember(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) - {"statement", "kind"}:
                raise ToolValidationError("knowledge_remember received an unknown argument")
            raw_kind = arguments.get("kind", "NOTE")
            if not isinstance(raw_kind, str):
                raise ToolValidationError("kind must be a string")
            kind = raw_kind.strip() or "NOTE"
            if len(kind.encode("utf-8")) > 128:
                raise ToolValidationError("kind exceeds the input bound")
            return {"statement": _string(arguments, "statement"), "kind": kind}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            store, _service = await asyncio.to_thread(self._service)
            try:
                record = await asyncio.to_thread(
                    store.create_record,
                    kind=str(arguments["kind"]), statement=str(arguments["statement"]),
                    confidence=1.0, access_scope="private",
                )
                return _result(record)
            finally:
                store.close()

        return ToolDefinition(
            name="knowledge_remember",
            description="Propose a private Knowledge record; Noruct approval is required before writing.",
            input_schema={
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "kind": {"type": "string"},
                },
                "required": ["statement"],
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            risk=ToolRisk.MEDIUM,
            idempotency_mode=IdempotencyMode.CALL_KEY,
            validator=validate,
            resource_key=lambda a: "knowledge:record",
            handler=handle,
            requires_approval=True,
            allow_session_approval=False,
        )

    def _folder_open(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) - {"entry_id", "max_bytes"}:
                raise ToolValidationError("knowledge_folder_open received an unknown argument")
            entry_id = _string(arguments, "entry_id")
            if not entry_id.startswith("folder-entry-"):
                raise ToolValidationError("entry_id must identify a Knowledge Folder entry")
            max_bytes = int(arguments.get("max_bytes", 16_000))
            if max_bytes < 128 or max_bytes > 64_000:
                raise ToolValidationError("max_bytes must be between 128 and 64000")
            return {"entry_id": entry_id, "max_bytes": max_bytes}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            store, service = await asyncio.to_thread(self._service)
            try:
                result = await asyncio.to_thread(
                    service.folders.open_entry,
                    str(arguments["entry_id"]),
                    max_bytes=int(arguments["max_bytes"]),
                )
                return _result(result)
            finally:
                store.close()

        return ToolDefinition(
            name="knowledge_folder_open",
            description=(
                "Open one indexed user Knowledge Folder file with a byte bound and "
                "freeze the exact content as evidence before returning it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                },
                "required": ["entry_id"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ,
            risk=ToolRisk.LOW,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=lambda a: f"knowledge:folder-open:{a['entry_id']}",
            handler=handle,
            parallel_safe=True,
        )

    def _ingest(self) -> ToolDefinition:
        def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if set(arguments) != {"path"}:
                raise ToolValidationError("knowledge_ingest requires only path")
            raw = _string(arguments, "path")
            path = PurePosixPath(raw)
            if path.is_absolute() or ".." in path.parts:
                raise ToolValidationError("Knowledge intake path must stay inside the workspace")
            return {"path": str(path)}

        async def handle(arguments: Mapping[str, object], cancellation: CancellationToken) -> str:
            cancellation.raise_if_cancelled()
            source = (self.workspace / str(arguments["path"])).resolve()
            try:
                source.relative_to(self.workspace)
            except ValueError as exc:
                raise ToolValidationError("Knowledge intake path escaped the workspace") from exc
            if not source.is_file() or source.is_symlink():
                raise ToolValidationError("Knowledge intake path is not a regular file")
            store, service = await asyncio.to_thread(self._service)
            try:
                result = await asyncio.to_thread(service.ingest, source)
                return _result(result)
            finally:
                store.close()

        return ToolDefinition(
            name="knowledge_ingest",
            description="Preserve and process one approved local document into the Knowledge Runtime.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            effect=ToolEffect.WRITE,
            risk=ToolRisk.MEDIUM,
            idempotency_mode=IdempotencyMode.NATURAL_KEY,
            validator=validate,
            resource_key=lambda a: f"knowledge:ingest:{a['path']}",
            handler=handle,
            requires_approval=True,
            allow_session_approval=False,
        )
