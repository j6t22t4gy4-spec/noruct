"""Private employee-agent execution worker behind the Noruct JSONL port.

The loop and stream accumulator come from the exact-pinned foundation source.
Model and tool effects are RPC requests to the Noruct parent; this process has
no authority to contact a provider or execute a tool itself.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import queue
import socket
import sys
import threading
from pathlib import Path
from types import MethodType, ModuleType, SimpleNamespace
from typing import Any

from dynamic_firm._vendor.runtime_safety.memory_context import (
    StreamingContextScrubber,
    sanitize_context,
)

from .protocol import (
    MAX_FRAME_BYTES,
    FoundationFrame,
    FoundationProtocolError,
    FrameSequence,
    decode_frame,
    encode_frame,
)


class _WorkerCancelled(BaseException):
    pass


def _vendor_root() -> Path:
    """Return the exact-pinned full agent-core baseline.

    The former trace-bound capsule made every omitted upstream mechanism a
    Noruct shim or a new adapter.  The employee worker now executes the
    coherent registered core directly.  This does not grant it provider,
    filesystem, network, credential, or durable-state authority: those
    effects still cross the parent JSONL port below.

    The baseline itself remains immutable for provenance.  A later active
    fork may carry Noruct-specific core modifications, but it must preserve
    this exact tree as its comparison source.
    """

    return (
        Path(__file__).parents[1]
        / "_vendor"
        / "hermes_agent"
        / "upstream"
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def _scrub_memory_context(value: Any) -> Any:
    """Keep private recalled-memory fences out of worker terminal history."""

    if isinstance(value, str):
        return sanitize_context(value)
    if isinstance(value, dict):
        return {str(key): _scrub_memory_context(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_memory_context(item) for item in value]
    if isinstance(value, tuple):
        return [_scrub_memory_context(item) for item in value]
    return value


class _Bridge:
    def __init__(self, wire_out) -> None:
        self._wire_out = wire_out
        self._incoming: queue.Queue[FoundationFrame | BaseException | None] = queue.Queue()
        self._outbound = FrameSequence()
        self._inbound = FrameSequence()
        self._write_lock = threading.Lock()
        self._agent_lock = threading.Lock()
        self._active_agent = None
        self._reader = threading.Thread(target=self._read_stdin, daemon=True)

    def start(self) -> None:
        self._reader.start()

    def bind_agent(self, agent) -> None:
        with self._agent_lock:
            self._active_agent = agent

    def unbind_agent(self, agent) -> None:
        with self._agent_lock:
            if self._active_agent is agent:
                self._active_agent = None

    def _read_stdin(self) -> None:
        try:
            while True:
                raw = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 2)
                if not raw:
                    self._incoming.put(None)
                    return
                if len(raw) > MAX_FRAME_BYTES + 1 or not raw.endswith(b"\n"):
                    raise FoundationProtocolError("inbound frame exceeds the byte limit")
                frame = decode_frame(raw)
                self._inbound.accept(frame)
                if frame.type == "cancel":
                    with self._agent_lock:
                        agent = self._active_agent
                    if agent is not None:
                        agent.interrupt(str(frame.payload.get("reason") or "cancelled"))
                self._incoming.put(frame)
        except BaseException as exc:
            self._incoming.put(exc)

    def emit(self, frame_type: str, run_id: str, payload: dict[str, Any]) -> None:
        frame = FoundationFrame(
            frame_type,
            run_id,
            self._outbound.next(run_id),
            _jsonable(payload),
        )
        raw = encode_frame(frame)
        with self._write_lock:
            self._wire_out.buffer.write(raw)
            self._wire_out.buffer.flush()

    def next_execute(self) -> FoundationFrame | None:
        while True:
            item = self._incoming.get()
            if item is None:
                return None
            if isinstance(item, BaseException):
                raise item
            if item.type == "execute":
                return item
            if item.type == "cancel":
                continue
            raise FoundationProtocolError(f"unexpected idle frame: {item.type}")

    def wait_reply(self, run_id: str, expected: str) -> FoundationFrame:
        while True:
            item = self._incoming.get()
            if item is None:
                raise EOFError("parent closed the worker channel")
            if isinstance(item, BaseException):
                raise item
            if item.run_id != run_id:
                raise FoundationProtocolError("cross-run frame on a serialized worker")
            if item.type == "cancel":
                raise _WorkerCancelled(str(item.payload.get("reason") or "cancelled"))
            if item.type == "provider_error":
                code = str(item.payload.get("code") or "PROVIDER_ERROR")
                if code == "RUN_CANCELLED":
                    raise _WorkerCancelled(str(item.payload.get("message") or "cancelled"))
                raise RuntimeError(f"Noruct parent provider error: {code}")
            if item.type != expected:
                raise FoundationProtocolError(
                    f"expected {expected}, received {item.type}"
                )
            return item


class _LocalCompletions:
    def __init__(
        self,
        bridge: _Bridge,
        run_id: str,
        *,
        initial_call_index: int = 0,
    ) -> None:
        self.bridge = bridge
        self.run_id = run_id
        self.call_index = max(0, initial_call_index)

    def create(self, **kwargs):
        self.call_index += 1
        self.bridge.emit(
            "model_request",
            self.run_id,
            {
                "call_index": self.call_index,
                "messages": _jsonable(kwargs.get("messages") or []),
                "tools": _jsonable(kwargs.get("tools") or []),
            },
        )
        reply = self.bridge.wait_reply(self.run_id, "provider_response")
        payload = reply.payload
        content = str(payload.get("content") or "")
        tool_calls = payload.get("tool_calls") or []
        finish_reason = str(
            payload.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
        )
        chunks: list[SimpleNamespace] = []
        # Deterministic, bounded chunks exercise the foundation stream
        # accumulator and callbacks without inventing another stream engine.
        for offset in range(0, len(content), 32):
            delta = SimpleNamespace(
                content=content[offset : offset + 32],
                tool_calls=None,
                reasoning_content=None,
                reasoning=None,
            )
            chunks.append(
                SimpleNamespace(
                    choices=[SimpleNamespace(index=0, delta=delta, finish_reason=None)],
                    model="noruct-parent",
                    usage=None,
                )
            )
        for index, raw_call in enumerate(tool_calls):
            arguments = json.dumps(
                raw_call.get("arguments") or {},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            tc_delta = SimpleNamespace(
                index=index,
                id=str(raw_call.get("call_id") or f"call-{index + 1}"),
                function=SimpleNamespace(
                    name=str(raw_call.get("name") or ""),
                    arguments=arguments,
                ),
            )
            delta = SimpleNamespace(
                content=None,
                tool_calls=[tc_delta],
                reasoning_content=None,
                reasoning=None,
            )
            chunks.append(
                SimpleNamespace(
                    choices=[SimpleNamespace(index=0, delta=delta, finish_reason=None)],
                    model="noruct-parent",
                    usage=None,
                )
            )
        terminal_delta = SimpleNamespace(
            content=None,
            tool_calls=None,
            reasoning_content=None,
            reasoning=None,
        )
        chunks.append(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        index=0,
                        delta=terminal_delta,
                        finish_reason=finish_reason,
                    )
                ],
                model="noruct-parent",
                usage=None,
            )
        )
        usage = payload.get("usage") or {}
        chunks.append(
            SimpleNamespace(
                choices=[],
                model="noruct-parent",
                usage=SimpleNamespace(
                    prompt_tokens=int(usage.get("input_tokens", 0) or 0),
                    completion_tokens=int(usage.get("output_tokens", 0) or 0),
                    total_tokens=int(usage.get("input_tokens", 0) or 0)
                    + int(usage.get("output_tokens", 0) or 0),
                ),
            )
        )
        return iter(chunks)


class _LocalClient:
    def __init__(
        self,
        bridge: _Bridge,
        run_id: str,
        *,
        initial_call_index: int = 0,
    ) -> None:
        self.chat = SimpleNamespace(
            completions=_LocalCompletions(
                bridge,
                run_id,
                initial_call_index=initial_call_index,
            )
        )
        self.is_closed = False

    def close(self) -> None:
        # AIAgent creates per-request clients. They all route to the same
        # in-process bridge, so close is intentionally a no-op.
        return None


def _tool_definitions(raw_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    definitions = []
    for item in raw_tools:
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": str(item["name"]),
                    "description": str(item.get("description") or ""),
                    "parameters": item.get("input_schema") or {"type": "object"},
                },
            }
        )
    return definitions


_NORUCT_CORE_TOOL_NAMES = frozenset(
    {
        "read_workspace_file",
        "list_workspace_files",
        "search_workspace_files",
        "write_workspace_file",
        "edit_workspace_file",
        "patch_workspace_file",
        "apply_workspace_multi_patch",
        "move_workspace_file",
        "delete_workspace_file",
        "run_workspace_command",
        "run_workspace_background_command",
        "list_workspace_processes",
        "inspect_workspace_process",
        "wait_workspace_process",
        "stop_workspace_process",
        "todo",
        # A deterministic fixture is intentionally core so the foundation
        # parity suite keeps exercising the ordinary direct-tool path.
        "read_fixture",
    }
)


def _prepare_tool_disclosure(
    raw_tools: list[dict[str, Any]],
    *,
    include_local_planning: bool,
) -> tuple[list[dict[str, Any]], callable]:
    """Activate the exact vendored tool registry and progressive disclosure.

    Tool handlers, approvals and effects stay parent-owned.  The private
    worker only uses the upstream registry/catalog code to decide the
    model-visible schema surface and to resolve its three data-only bridge
    calls.  That lets a large user-managed MCP/plugin tool surface avoid
    paying its full schema cost every turn without creating a parallel
    first-party tool-search implementation.
    """

    # ``tools.tool_search`` deliberately looks up both these modules lazily.
    # The capsule excludes the upstream product toolset selector because that
    # selector would discover executors and configuration owned by the parent.
    # Its only needed invariant here is the set of always-visible core names.
    toolsets = ModuleType("toolsets")
    toolsets._HERMES_CORE_TOOLS = set(_NORUCT_CORE_TOOL_NAMES)
    sys.modules["toolsets"] = toolsets

    registry_module = importlib.import_module("tools.registry")
    source_registry = registry_module.ToolRegistry()
    # The source catalog obtains the singleton through a lazy import. Rebind
    # it per run so persistent workers cannot retain another Job's tool view.
    registry_module.registry = source_registry
    search_module = importlib.import_module("tools.tool_search")

    # ``todo_tool`` is a dependency-free, no-effect source tool already
    # present in the sealed capsule and instantiated by the exact source
    # agent. Surface its exact schema instead of recreating a planning-tool
    # contract in the product. Its mutable list remains agent/session-local;
    # it never becomes Company or ActionPolicy state.
    if include_local_planning:
        todo_module = importlib.import_module("tools.todo_tool")
        if not any(
            isinstance(item.get("function"), dict)
            and item["function"].get("name") == "todo"
            for item in raw_tools
        ):
            raw_tools = [
                *raw_tools,
                {"type": "function", "function": dict(todo_module.TODO_SCHEMA)},
            ]

    for definition in raw_tools:
        function = definition.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        schema = {
            "name": name,
            "description": str(function.get("description") or ""),
            "parameters": function.get("parameters")
            if isinstance(function.get("parameters"), dict)
            else {"type": "object"},
        }
        source_registry.register(
            name=name,
            toolset="noruct-core" if name in _NORUCT_CORE_TOOL_NAMES else "noruct-capability",
            schema=schema,
            # This is intentionally unreachable: concrete dispatch crosses
            # the Noruct parent below.  The registry requires a handler only
            # as metadata for one exact source catalog implementation.
            handler=lambda *_args, **_kwargs: "",
            description=schema["description"],
        )

    config = search_module.ToolSearchConfig.from_raw(
        {
            "enabled": "auto",
            "threshold_pct": 10,
            "search_default_limit": 5,
            "max_search_limit": 20,
        }
    )
    # The worker deliberately has no provider-specific remote catalog.  A
    # conservative local 16k context estimate means ordinary repository work
    # stays direct while materially large capability sets are deferred.
    assembly = search_module.assemble_tool_defs(
        raw_tools,
        context_length=16_000,
        config=config,
    )
    scoped_names = search_module.scoped_deferrable_names(raw_tools)

    def dispatch(
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any], str | None]:
        """Return (parent-tool-name, parent-arguments, local-result).

        A non-null local result is a data-only source bridge answer.  A
        resolved ``tool_call`` returns the underlying granted Noruct tool so
        it still enters the usual ActionPolicy/approval/ledger path.
        """

        if name == search_module.TOOL_SEARCH_NAME:
            return (
                None,
                {},
                search_module.dispatch_tool_search(
                    arguments,
                    current_tool_defs=raw_tools,
                    config=config,
                ),
            )
        if name == search_module.TOOL_DESCRIBE_NAME:
            return (
                None,
                {},
                search_module.dispatch_tool_describe(
                    arguments,
                    current_tool_defs=raw_tools,
                ),
            )
        if name == search_module.TOOL_CALL_NAME:
            target, target_arguments, error = search_module.resolve_underlying_call(arguments)
            if error is not None:
                return None, {}, json.dumps({"error": error}, ensure_ascii=False)
            if target not in scoped_names:
                return None, {}, json.dumps(
                    {"error": "Requested capability is outside this job's tool scope."},
                    ensure_ascii=False,
                )
            return target, target_arguments, None
        return name, arguments, None

    return assembly.tool_defs, dispatch


def _deny_network(*args: object, **kwargs: object) -> None:
    raise RuntimeError("network access is forbidden in the employee execution worker")


def _configure_environment() -> None:
    if os.environ.get("NORUCT_FOUNDATION_EXECUTION_WORKER") != "1":
        raise RuntimeError("execution worker is private and must be launched by Noruct")
    root = _vendor_root()
    if not root.is_dir():
        raise RuntimeError("vendored employee foundation root is missing")
    sys.dont_write_bytecode = True
    sys.path[:] = [item for item in sys.path if item not in ("", ".")]
    sys.path.insert(0, str(root))
    os.environ["HERMES_PYTHON_SRC_ROOT"] = str(root)
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    home = Path(os.environ["HERMES_HOME"])
    for name in ("bundled-skills", "optional-skills", "optional-mcps"):
        path = home / name
        path.mkdir(parents=True, exist_ok=True)
        os.environ[f"HERMES_{name.replace('-', '_').upper()}"] = str(path)
    socket.create_connection = _deny_network  # type: ignore[assignment]
    socket.socket.connect = _deny_network  # type: ignore[assignment,method-assign]
    socket.socket.connect_ex = _deny_network  # type: ignore[assignment,method-assign]


from .employee_worker_shims import (  # noqa: E402
    install_parent_authority_shims as _install_parent_authority_shims,
)


def _run_execute(bridge: _Bridge, frame: FoundationFrame) -> None:
    _install_parent_authority_shims()
    try:
        # The active application spine is the Noruct fork package. It loads
        # the exact Hermes core from this pinned tree while retaining the
        # parent-owned RPC/tool seams below this worker.
        from noruct_firm import agent as fork_agent

        run_agent = fork_agent.load_core()
    except (ImportError, ModuleNotFoundError) as exc:
        bridge.emit(
            "worker_error",
            frame.run_id,
            {"error_type": type(exc).__name__, "message": str(exc)[:500]},
        )
        return

    payload = frame.payload
    run_id = frame.run_id
    raw_tools = _tool_definitions(list(payload.get("tools") or []))
    tools, resolve_tool_call = _prepare_tool_disclosure(
        raw_tools,
        include_local_planning=bool(payload.get("local_planning_enabled")),
    )
    client = _LocalClient(
        bridge,
        run_id,
        initial_call_index=int(payload.get("initial_model_call_index") or 0),
    )
    # The exact source executor unwraps ``tool_call`` before it reaches the
    # dispatch seam, and obtains the allowed deferred names from this
    # model-tools accessor.  Feed it the pre-assembly parent-approved catalog
    # so the source scope gate and the source catalog always agree.
    model_tools = sys.modules.get("model_tools")
    if model_tools is not None:
        model_tools.get_tool_definitions = lambda **_kwargs: raw_tools
    run_agent.get_tool_definitions = lambda **kwargs: tools
    run_agent.check_toolset_requirements = lambda: {}
    run_agent.cleanup_vm = lambda task_id=None: None
    run_agent.cleanup_browser = lambda task_id=None: None
    original_create = run_agent.AIAgent._create_openai_client
    run_agent.AIAgent._create_openai_client = (
        lambda self, client_kwargs, *, reason, shared: client
    )

    def parent_owned_tool_dispatch(
        function_name,
        function_args,
        effective_task_id,
        *,
        tool_call_id=None,
        **_ignored,
    ):
        """Adapt the foundation registry-dispatch seam to the parent RPC.

        ``agent.tool_executor`` remains responsible for parsing, sequential
        interruption, result-message formation, progress persistence and
        bounded result handling.  It reaches concrete tool handlers only via
        this upstream ``run_agent.handle_function_call`` seam.  Replacing that
        seam preserves the foundation execution loop while making Noruct's
        parent the sole authority for registration, ActionPolicy, approval,
        budget reservation, cancellation and every side effect.
        """

        arguments = function_args if isinstance(function_args, dict) else {}
        resolved_name, resolved_arguments, local_result = resolve_tool_call(
            str(function_name or ""),
            arguments,
        )
        if local_result is not None:
            return local_result
        if not resolved_name:
            return json.dumps({"error": "Tool call could not be resolved."}, ensure_ascii=False)
        call_id = str(tool_call_id or "tool-call")
        bridge.emit(
            "tool_intent",
            run_id,
            {
                "call_index": client.chat.completions.call_index,
                "call_id": call_id,
                "name": resolved_name,
                "arguments": resolved_arguments,
            },
        )
        reply = bridge.wait_reply(run_id, "tool_result")
        return str(reply.payload.get("content") or "")

    # The source executor calls this module-level binding rather than looking
    # up handlers directly.  Do not install an alternate first-party loop on
    # ``AIAgent``: the imported foundation dispatcher is now the active path.
    run_agent.handle_function_call = parent_owned_tool_dispatch

    stream_scrubber = StreamingContextScrubber()

    def stream_delta(text: str | None) -> None:
        if isinstance(text, str) and text:
            visible = stream_scrubber.feed(text)
            if visible:
                bridge.emit("text_delta", run_id, {"text": visible})

    agent = None
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            agent = fork_agent.create_agent(
                run_agent,
                base_url="http://127.0.0.1/noruct-parent",
                api_key="noruct-parent-no-secret",
                provider="custom",
                api_mode="chat_completions",
                model=str(payload.get("model_profile") or "noruct-parent"),
                max_iterations=max(1, int(payload.get("max_model_calls") or 1)),
                tool_delay=0.0,
                quiet_mode=True,
                stream_delta_callback=stream_delta,
                skip_context_files=True,
                skip_memory=True,
                session_id=str(payload.get("session_id") or run_id),
            )
            agent._persist_disabled = True
            agent._skip_mcp_refresh = True
            agent._session_db = None
            # This call crosses into the Noruct hook inserted directly in the
            # Hermes application fork.  The payload is data-only; provider,
            # tools, approvals and effects remain parent-owned RPC concerns.
            run_agent.attach_noruct_company_context(
                agent,
                payload.get("company_context")
                if isinstance(payload.get("company_context"), dict)
                else {},
            )
            # Provider selection and failover remain parent-owned.  Keep
            # Keep the foundation's empty-response retry loop, but never let an isolated
            # config activate an upstream credential/provider fallback.
            agent._fallback_chain = []
            agent._fallback_index = 0
            # Noruct owns product identity and the stable employee prompt;
            # The private foundation owns loop mechanics, stream assembly, and tool iteration;
            # interruption behavior only.
            agent._build_system_prompt = MethodType(
                lambda self, supplied=None: str(supplied or ""), agent
            )

            bridge.bind_agent(agent)
            result = agent.run_conversation(
                str(payload.get("user_message") or ""),
                system_message=str(payload.get("system_message") or ""),
                conversation_history=list(payload.get("conversation_history") or []),
                task_id=str(payload.get("task_id") or run_id),
            )
        trailing = stream_scrubber.flush()
        if trailing:
            bridge.emit("text_delta", run_id, {"text": sanitize_context(trailing)})
        bridge.emit(
            "terminal",
            run_id,
            {
                "completed": bool(result.get("completed", True)),
                "final_response": sanitize_context(str(result.get("final_response") or "")),
                "interrupted": bool(result.get("interrupted", False)),
                "messages": _scrub_memory_context(_jsonable(result.get("messages") or [])),
                "turn_exit_reason": str(result.get("turn_exit_reason") or ""),
                "api_calls": int(result.get("api_calls") or 0),
            },
        )
    except _WorkerCancelled as exc:
        bridge.emit(
            "terminal",
            run_id,
            {"completed": False, "interrupted": True, "reason": str(exc)},
        )
    except BaseException as exc:
        bridge.emit(
            "worker_error",
            run_id,
            {"error_type": type(exc).__name__, "message": str(exc)[:500]},
        )
    finally:
        if agent is not None:
            bridge.unbind_agent(agent)
        run_agent.AIAgent._create_openai_client = original_create


def main() -> int:
    wire_out = sys.stdout
    try:
        _configure_environment()
        bridge = _Bridge(wire_out)
        bridge.start()
        while True:
            execute = bridge.next_execute()
            if execute is None:
                return 0
            _run_execute(bridge, execute)
    except BaseException as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
