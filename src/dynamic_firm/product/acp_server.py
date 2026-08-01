"""Noruct-owned stdio bridge for the Agent Client Protocol (ACP).

The bridge deliberately owns only protocol framing, Company-session mapping and
approval forwarding.  The Dynamic Firm runtime remains the sole executor and
state authority: every prompt becomes an ordinary ``run_goal`` call and every
turn is persisted in ``CompanySessionStore``.

It uses JSON-RPC 2.0 over stdio directly instead of importing an ACP runtime
SDK.  That keeps the protocol boundary small, avoids making another product's
configuration/session implementation authoritative, and reserves stdout
strictly for protocol frames.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from dynamic_firm.product.events import ProductEvent, ProductEventType
from dynamic_firm.product.sessions import CompanySession, CompanySessionStore
from dynamic_firm.runtime.models import ApprovalDecision, ApprovalRequest, Usage
from dynamic_firm.runtime.ports import CancellationToken


ACP_PROTOCOL_VERSION = 1
ACP_SERVER_NAME = "noruct"
ACP_SERVER_VERSION = "0.0.0"
_MAX_FRAME_BYTES = 512_000
_MAX_PROMPT_BYTES = 128_000
_APPROVAL_TIMEOUT_SECONDS = 90.0


class AcpProtocolError(ValueError):
    """A client sent an invalid or unsupported ACP JSON-RPC frame."""


@dataclass(frozen=True, slots=True)
class AcpSessionInfo:
    """Small ACP projection of a Company-owned session."""

    session_id: str
    workspace: Path
    model: str
    title: str


TurnRunner = Callable[
    [CompanySession, str, Callable[[ProductEvent], None], "AcpApprovalPort | None"],
    Awaitable[Any],
]


def _jsonrpc_error(message_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def _workspace(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise AcpProtocolError("ACP session cwd must be a non-empty absolute path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise AcpProtocolError("ACP session cwd must be an absolute path")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise AcpProtocolError("ACP session cwd is not an existing directory")
    return resolved


def _text_prompt(value: object) -> str:
    if not isinstance(value, list) or not value:
        raise AcpProtocolError("ACP session prompt requires at least one text content block")
    pieces: list[str] = []
    for block in value:
        if not isinstance(block, Mapping):
            raise AcpProtocolError("ACP prompt content block must be an object")
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise AcpProtocolError("This ACP bridge currently accepts text prompt blocks only")
        pieces.append(str(block["text"]))
    text = "\n".join(pieces).strip()
    if not text:
        raise AcpProtocolError("ACP prompt text must be non-empty")
    if len(text.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise AcpProtocolError("ACP prompt exceeds the configured byte limit")
    return text


def _session_update_text(text: str, *, thought: bool = False) -> dict[str, object]:
    return {
        "sessionUpdate": "agent_thought_chunk" if thought else "agent_message_chunk",
        "content": {"type": "text", "text": text},
    }


class AcpApprovalPort:
    """Maps a runtime approval request to the ACP client's approval dialog."""

    def __init__(self, server: "AcpStdioServer", session_id: str) -> None:
        self._server = server
        self._session_id = session_id

    async def request(
        self,
        request: ApprovalRequest,
        cancellation: CancellationToken,
    ) -> ApprovalDecision:
        cancellation.raise_if_cancelled()
        options: list[dict[str, str]] = [
            {"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"},
        ]
        if request.allow_session:
            options.append(
                {
                    "optionId": "allow_session",
                    "kind": "allow_always",
                    "name": "Allow for this session",
                }
            )
        options.append({"optionId": "deny", "kind": "reject_once", "name": "Deny"})
        result = await self._server.request_client(
            "session/request_permission",
            {
                "sessionId": self._session_id,
                "toolCall": {
                    "toolCallId": request.action_id,
                    "title": request.preview,
                    "kind": "execute",
                    "status": "pending",
                    "content": [{"type": "text", "text": request.preview}],
                    "rawInput": {
                        "tool": request.tool_name,
                        "effect": request.effect.value,
                        "risk": request.risk.value,
                        "resource": request.resource_key,
                    },
                },
                "options": options,
            },
            timeout=_APPROVAL_TIMEOUT_SECONDS,
        )
        outcome = result.get("outcome") if isinstance(result, Mapping) else None
        if not isinstance(outcome, Mapping) or outcome.get("outcome") != "selected":
            return ApprovalDecision.DENY
        option_id = outcome.get("optionId", outcome.get("option_id"))
        if option_id == "allow_once":
            return ApprovalDecision.ALLOW_ONCE
        if option_id == "allow_session" and request.allow_session:
            return ApprovalDecision.ALLOW_SESSION
        return ApprovalDecision.DENY


class AcpStdioServer:
    """Bounded concurrent JSON-RPC server for one local IDE process."""

    def __init__(
        self,
        *,
        state_path: Path,
        default_workspace: Path,
        default_model: str,
        provider_binding: Mapping[str, str | None],
        permission_mode: str,
        turn_runner: TurnRunner,
        stdin: TextIO = sys.stdin,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
    ) -> None:
        if permission_mode not in {"read-only", "ask"}:
            raise ValueError("ACP permission mode must be read-only or ask")
        self._store = CompanySessionStore(state_path)
        self._default_workspace = default_workspace.resolve()
        self._default_model = default_model
        self._provider_binding = dict(provider_binding)
        self._permission_mode = permission_mode
        self._turn_runner = turn_runner
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._write_lock = asyncio.Lock()
        self._pending_client_requests: dict[str, asyncio.Future[Mapping[str, object]]] = {}
        self._prompt_tasks: dict[str, asyncio.Task[None]] = {}
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def serve(self) -> int:
        """Serve framed requests until stdin closes.  Stdout stays protocol-only."""

        try:
            while not self._closed:
                raw = await asyncio.to_thread(self._stdin.readline)
                if not raw:
                    break
                if len(raw.encode("utf-8", errors="replace")) > _MAX_FRAME_BYTES:
                    await self._send(_jsonrpc_error(None, -32600, "ACP JSON-RPC frame exceeds byte limit"))
                    continue
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send(_jsonrpc_error(None, -32700, "Invalid JSON-RPC frame"))
                    continue
                if not isinstance(frame, Mapping) or frame.get("jsonrpc") != "2.0":
                    await self._send(_jsonrpc_error(None, -32600, "Invalid JSON-RPC request"))
                    continue
                if self._accept_client_response(frame):
                    continue
                if not isinstance(frame.get("method"), str):
                    await self._send(_jsonrpc_error(frame.get("id"), -32600, "JSON-RPC method is required"))
                    continue
                task = asyncio.create_task(self._dispatch(frame))
                self._request_tasks.add(task)
                task.add_done_callback(self._request_tasks.discard)
                task.add_done_callback(self._report_task_exception)
        finally:
            self._closed = True
            for task in tuple(self._prompt_tasks.values()):
                task.cancel()
            # A client can close stdin immediately after its final request.
            # Let already-read non-prompt requests write their JSON-RPC reply
            # before closing the Company store; prompt requests have been
            # cancelled above and resolve through their regular cleanup path.
            if self._request_tasks:
                await asyncio.gather(*tuple(self._request_tasks), return_exceptions=True)
            if self._prompt_tasks:
                await asyncio.gather(*self._prompt_tasks.values(), return_exceptions=True)
            for future in self._pending_client_requests.values():
                if not future.done():
                    future.set_exception(AcpProtocolError("ACP client disconnected during approval"))
            self._store.close()
        return 0

    def _report_task_exception(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:  # pragma: no cover - defensive log path
            print(f"noruct ACP: unexpected request task failure: {type(exc).__name__}", file=self._stderr)

    def _accept_client_response(self, frame: Mapping[str, object]) -> bool:
        message_id = frame.get("id")
        if not isinstance(message_id, str) or message_id not in self._pending_client_requests:
            return False
        future = self._pending_client_requests.pop(message_id)
        if "error" in frame:
            future.set_exception(AcpProtocolError("ACP client denied or failed the permission request"))
        else:
            result = frame.get("result")
            future.set_result(result if isinstance(result, Mapping) else {})
        return True

    async def _dispatch(self, frame: Mapping[str, object]) -> None:
        message_id = frame.get("id")
        method = str(frame["method"])
        params = frame.get("params")
        if params is None:
            values: Mapping[str, object] = {}
        elif isinstance(params, Mapping):
            values = params
        else:
            await self._respond(message_id, error=(-32602, "JSON-RPC params must be an object"))
            return
        try:
            result = await self._handle(method, values)
        except AcpProtocolError as exc:
            await self._respond(message_id, error=(-32602, str(exc)))
        except KeyError:
            await self._respond(message_id, error=(-32004, "Unknown Noruct Company session"))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._respond(message_id, error=(-32603, "Noruct ACP request failed"))
        else:
            if message_id is not None:
                await self._respond(message_id, result=result)

    async def _respond(
        self,
        message_id: object,
        *,
        result: Mapping[str, object] | None = None,
        error: tuple[int, str] | None = None,
    ) -> None:
        if message_id is None:
            return
        if error is not None:
            await self._send(_jsonrpc_error(message_id, *error))
            return
        await self._send({"jsonrpc": "2.0", "id": message_id, "result": dict(result or {})})

    async def _send(self, frame: Mapping[str, object]) -> None:
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            self._stdout.write(encoded + "\n")
            self._stdout.flush()

    async def request_client(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        if self._closed:
            raise AcpProtocolError("ACP client is disconnected")
        request_id = f"noruct-{uuid.uuid4()}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Mapping[str, object]] = loop.create_future()
        self._pending_client_requests[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
            )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_client_requests.pop(request_id, None)

    async def _notify(self, method: str, params: Mapping[str, object]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _session(self, session_id: object) -> CompanySession:
        if not isinstance(session_id, str) or not session_id:
            raise AcpProtocolError("ACP sessionId is required")
        session = self._store.resolve(session_id)
        if session is None or session.session_id != session_id:
            raise KeyError(session_id)
        return session

    def _verify_session_workspace(self, session: CompanySession, cwd: object) -> None:
        if cwd is None:
            return
        selected = _workspace(cwd)
        if selected != Path(session.workspace).resolve():
            raise AcpProtocolError("ACP session cwd does not match the Company session workspace")

    def _new_session(self, values: Mapping[str, object]) -> Mapping[str, object]:
        workspace = _workspace(values.get("cwd", str(self._default_workspace)))
        session = self._store.create(
            workspace=workspace,
            model=self._default_model,
            provider_kind=str(self._provider_binding.get("provider_kind") or ""),
            provider_base_url=str(self._provider_binding.get("provider_base_url") or ""),
            provider_api_key_env=self._provider_binding.get("provider_api_key_env"),
        )
        return {"sessionId": session.session_id}

    async def _replay(self, session: CompanySession) -> None:
        turns = self._store.recall_turns(
            session_id=session.session_id,
            after_position=0,
            limit=8,
            max_bytes=16_000,
        )
        for turn in turns:
            await self._notify(
                "session/update",
                {"sessionId": session.session_id, "update": _session_update_text(turn.goal)},
            )
            if turn.summary:
                await self._notify(
                    "session/update",
                    {"sessionId": session.session_id, "update": _session_update_text(turn.summary)},
                )

    async def _handle(self, method: str, values: Mapping[str, object]) -> Mapping[str, object]:
        if method == "initialize":
            requested = values.get("protocolVersion")
            if requested is not None and requested != ACP_PROTOCOL_VERSION:
                raise AcpProtocolError("Unsupported ACP protocol version")
            return {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "agentInfo": {"name": ACP_SERVER_NAME, "version": ACP_SERVER_VERSION},
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"image": False},
                    "sessionCapabilities": {"list": {}, "resume": {}},
                },
            }
        if method == "session/new":
            return self._new_session(values)
        if method in {"session/load", "session/resume"}:
            session = self._session(values.get("sessionId"))
            self._verify_session_workspace(session, values.get("cwd"))
            await self._replay(session)
            return {"models": {"currentModelId": session.model}}
        if method == "session/list":
            raw_cursor = values.get("cursor")
            if raw_cursor is not None and not isinstance(raw_cursor, str):
                raise AcpProtocolError("ACP session cursor must be a string")
            cwd = values.get("cwd")
            selected_workspace = _workspace(cwd) if cwd is not None else None
            sessions = [
                item
                for item in self._store.list(limit=200)
                if selected_workspace is None or Path(item.workspace).resolve() == selected_workspace
            ]
            if raw_cursor:
                after = next((index for index, item in enumerate(sessions) if item.session_id == raw_cursor), None)
                sessions = sessions[after + 1 :] if after is not None else []
            page = sessions[:50]
            return {
                "sessions": [
                    {
                        "sessionId": item.session_id,
                        "cwd": item.workspace,
                        "title": item.title if item.title != "New session" else None,
                        "updatedAt": item.updated_at,
                    }
                    for item in page
                ],
                "nextCursor": page[-1].session_id if len(sessions) > len(page) and page else None,
            }
        if method == "session/cancel":
            session_id = values.get("sessionId")
            if not isinstance(session_id, str):
                raise AcpProtocolError("ACP sessionId is required")
            task = self._prompt_tasks.get(session_id)
            if task is not None:
                task.cancel()
            return {}
        if method == "session/set_model":
            session = self._session(values.get("sessionId"))
            model_id = values.get("modelId")
            if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 240:
                raise AcpProtocolError("ACP modelId must be a bounded non-empty string")
            self._store.update_model(session.session_id, model_id.strip())
            return {}
        if method == "session/prompt":
            session = self._session(values.get("sessionId"))
            text = _text_prompt(values.get("prompt"))
            active = self._prompt_tasks.get(session.session_id)
            if active is not None and not active.done():
                raise AcpProtocolError("ACP session already has an active prompt")
            task = asyncio.create_task(self._run_prompt(session, text))
            self._prompt_tasks[session.session_id] = task
            try:
                await task
            except asyncio.CancelledError:
                # A client-issued session/cancel is an acknowledged ACP
                # operation, not a transport failure.  The runtime task has
                # already received cancellation through task propagation.
                return {}
            finally:
                if self._prompt_tasks.get(session.session_id) is task:
                    self._prompt_tasks.pop(session.session_id, None)
            return {}
        raise AcpProtocolError("ACP method is not supported by Noruct")

    async def _run_prompt(self, session: CompanySession, text: str) -> None:
        emitted_text: list[str] = []
        notification_tasks: list[asyncio.Task[None]] = []

        def emit(event: ProductEvent) -> None:
            if event.type != ProductEventType.MODEL_STREAMING:
                return
            if event.data.get("stream_kind") != "text_delta" or not event.message:
                return
            emitted_text.append(event.message)
            notification_tasks.append(asyncio.create_task(
                self._notify(
                    "session/update",
                    {"sessionId": session.session_id, "update": _session_update_text(event.message)},
                )
            ))

        approval = AcpApprovalPort(self, session.session_id) if self._permission_mode == "ask" else None
        result = await self._turn_runner(session, text, emit, approval)
        if notification_tasks:
            await asyncio.gather(*notification_tasks)
        summary = str(getattr(result, "summary", "") or "")
        if summary and not emitted_text:
            await self._notify(
                "session/update",
                {"sessionId": session.session_id, "update": _session_update_text(summary)},
            )
        usage = getattr(getattr(result, "metrics", None), "usage", Usage())
        if not isinstance(usage, Usage):
            usage = Usage()
        await self._notify(
            "session/update",
            {
                "sessionId": session.session_id,
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": {
                        "inputTokens": usage.input_tokens,
                        "outputTokens": usage.output_tokens,
                    },
                },
            },
        )


async def serve_acp_stdio(**kwargs: Any) -> int:
    """Construct and run the Noruct ACP bridge; convenient CLI entry point."""

    return await AcpStdioServer(**kwargs).serve()
