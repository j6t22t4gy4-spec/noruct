"""Inert upstream-import shims for the isolated Employee Runtime worker.

These shims retain only import-level contracts required by the audited
foundation loop.  They deliberately prevent the child from discovering tools,
providers, credentials, transports, plugins, or durable session authority.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType


def install_parent_authority_shims() -> None:
    """Remove upstream effect discovery from the private worker import path.

    ``run_agent`` imports the upstream tool orchestrator and local browser /
    terminal cleanup functions at module load time.  It also imports provider
    credentials, provider metadata, TLS transport setup, and external runtime
    environments while constructing ``AIAgent``. Noruct never delegates any
    of those effects to the child: schemas, dispatch, cleanup, approval,
    credentials, provider selection, TLS, and cancellation are parent-owned
    RPC operations. Supplying the narrow names that the loop imports keeps the
    exact upstream conversation loop while preventing unrelated CLI, gateway,
    plugin, MCP, provider, and builtin-tool graphs from becoming employee
    runtime dependencies.
    """

    def forbidden_tool_dispatch(*args: object, **kwargs: object) -> str:
        raise RuntimeError("employee worker tool dispatch must cross the Noruct parent")

    model_tools = ModuleType("model_tools")
    model_tools.get_tool_definitions = lambda **kwargs: []
    model_tools.get_toolset_for_tool = lambda name: "noruct-parent"
    model_tools.handle_function_call = forbidden_tool_dispatch
    model_tools.check_toolset_requirements = lambda: {}
    sys.modules["model_tools"] = model_tools

    env_loader = ModuleType("hermes_cli.env_loader")
    env_loader.load_hermes_dotenv = lambda **kwargs: []
    sys.modules["hermes_cli.env_loader"] = env_loader

    browser_tool = ModuleType("tools.browser_tool")
    browser_tool.cleanup_browser = lambda task_id=None: None
    sys.modules["tools.browser_tool"] = browser_tool

    terminal_tool = ModuleType("tools.terminal_tool")
    terminal_tool.cleanup_vm = lambda task_id=None: None
    terminal_tool.is_persistent_env = lambda task_id=None: False
    # ``agent.tool_executor`` imports this accessor to decide whether a tool
    # result belongs to an upstream persistent environment.  The worker has no
    # such environment: every effect is completed by the parent-owned executor
    # before a result crosses back over JSONL.
    terminal_tool.get_active_env = lambda task_id=None: None
    sys.modules["tools.terminal_tool"] = terminal_tool

    delegate_tool = ModuleType("tools.delegate_tool")
    delegate_tool._get_max_concurrent_children = lambda: 1
    sys.modules["tools.delegate_tool"] = delegate_tool

    # The parent has already selected the provider and owns all credentials.
    # Preserve the upstream loop's minimum-context guard and token-estimation
    # contracts, but never discover a model through remote registries or probe
    # an endpoint from this isolated worker.
    model_metadata = ModuleType("agent.model_metadata")
    model_metadata.MINIMUM_CONTEXT_LENGTH = 64_000
    model_metadata.DEFAULT_FALLBACK_CONTEXT = 256_000
    model_metadata.fetch_model_metadata = lambda *args, **kwargs: {}
    model_metadata.fetch_endpoint_model_metadata = lambda *args, **kwargs: {}
    model_metadata.query_ollama_num_ctx = lambda *args, **kwargs: None
    model_metadata.is_local_endpoint = lambda *args, **kwargs: False
    model_metadata.get_model_context_length = (
        lambda *args, **kwargs: int(kwargs.get("config_context_length") or 256_000)
    )

    def estimate_messages_tokens_rough(messages, *args, **kwargs) -> int:
        return max(0, len(json.dumps(messages or [], ensure_ascii=False)) // 4)

    def estimate_request_tokens_rough(messages, *args, **kwargs) -> int:
        payload = {
            "messages": messages or [],
            "system_prompt": kwargs.get("system_prompt") or "",
            "tools": kwargs.get("tools") or [],
        }
        return max(0, len(json.dumps(payload, ensure_ascii=False)) // 4)

    model_metadata.estimate_messages_tokens_rough = estimate_messages_tokens_rough
    model_metadata.estimate_request_tokens_rough = estimate_request_tokens_rough

    def unavailable_model_metadata_member(name: str):
        # Provider- and endpoint-specific metadata is deliberately absent from
        # this child. Optional upstream recovery paths treat a null result as
        # "no metadata available" and leave the parent-selected profile intact.
        return lambda *args, **kwargs: None

    model_metadata.__getattr__ = unavailable_model_metadata_member
    sys.modules["agent.model_metadata"] = model_metadata

    models_dev = ModuleType("agent.models_dev")
    models_dev._load_disk_cache = lambda *args, **kwargs: {}
    models_dev.fetch_models_dev = lambda *args, **kwargs: {}
    models_dev.lookup_models_dev_context = lambda *args, **kwargs: None
    models_dev.list_agentic_models = lambda *args, **kwargs: []
    models_dev.get_model_capabilities = lambda *args, **kwargs: None
    models_dev.__getattr__ = unavailable_model_metadata_member
    sys.modules["agent.models_dev"] = models_dev

    # A provider-native Gemini client cannot be selected through Noruct's
    # parent-owned OpenAI-shaped bridge. The helper is still imported by the
    # upstream chat-completions loop, so retain its negative predicate only.
    gemini_adapter = ModuleType("agent.gemini_native_adapter")
    gemini_adapter.is_native_gemini_base_url = lambda *args, **kwargs: False
    sys.modules["agent.gemini_native_adapter"] = gemini_adapter

    # Provider-native Anthropic transport is likewise outside the isolated
    # worker boundary.  Noruct's parent injects one already-authorized
    # OpenAI-shaped client, so none of the upstream adapter's credential,
    # endpoint, message-conversion, or retry helpers may select a second
    # provider path.  The retained loop imports those helpers lazily only in
    # provider-native branches; make such a branch fail at its authority
    # boundary rather than load the adapter into the shipped worker capsule.
    anthropic_adapter = ModuleType("agent.anthropic_adapter")
    # The retained chat-completions path asks this classifier whether a
    # parent-selected generic profile belongs to an adapter-only family.  It
    # cannot be true for the private parent bridge.
    anthropic_adapter._model_name_is_kimi_family = lambda *args, **kwargs: False

    def blocked_native_provider_transport(member: str, *args, **kwargs):
        raise RuntimeError(
            "employee worker native provider transport must cross the Noruct parent "
            f"(member={member})"
        )

    anthropic_adapter.__getattr__ = lambda name: (
        lambda *args, **kwargs: blocked_native_provider_transport(name, *args, **kwargs)
    )
    sys.modules["agent.anthropic_adapter"] = anthropic_adapter

    # The retained upstream stream assembler imports httpx for exception
    # taxonomy even when the injected local client never opens a socket. Keep
    # only that taxonomy in-process; any attempt to construct a transport is
    # rejected by the worker's socket guard before it can become an effect.
    httpx = ModuleType("httpx")

    class _HttpxError(RuntimeError):
        pass

    for error_name in (
        "HTTPError",
        "RequestError",
        "TransportError",
        "NetworkError",
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ConnectError",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "ProtocolError",
        "HTTPStatusError",
    ):
        setattr(httpx, error_name, _HttpxError)

    class _Timeout:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

    httpx.Timeout = _Timeout
    sys.modules["httpx"] = httpx

    # The child has no network authority and its local client never constructs
    # an HTTP/TLS transport. A CA-bundle check would only reintroduce certifi
    # without protecting a real outbound request.
    ssl_guard = ModuleType("agent.ssl_guard")
    ssl_guard.verify_ca_bundle_with_fallback = lambda: None
    sys.modules["agent.ssl_guard"] = ssl_guard

    # Context compression's upstream auxiliary client is an independent
    # provider and credential router. It is outside the worker boundary: the
    # parent must decide if a separate model call is allowed. The primary loop
    # still retains its normal context-compression state and simply receives no
    # unauthorized auxiliary transport from the child.
    auxiliary_client = ModuleType("agent.auxiliary_client")

    @contextlib.contextmanager
    def aux_interrupt_protection(*args, **kwargs):
        yield

    def blocked_auxiliary_call(*args, **kwargs):
        raise RuntimeError("auxiliary model calls must be authorized by the Noruct parent")

    auxiliary_client.aux_interrupt_protection = aux_interrupt_protection
    auxiliary_client.call_llm = blocked_auxiliary_call
    auxiliary_client._is_connection_error = lambda *args, **kwargs: False
    auxiliary_client._apply_user_default_headers = lambda headers=None: headers
    sys.modules["agent.auxiliary_client"] = auxiliary_client

    # Credential pools are an upstream provider failover feature. They must
    # never discover, rotate, or persist credentials inside the isolated
    # child; parent provider failures arrive as explicit RPC error frames.
    credential_pool = ModuleType("agent.credential_pool")
    credential_pool.STATUS_EXHAUSTED = "exhausted"
    credential_pool.CUSTOM_POOL_PREFIX = "custom:"
    credential_pool.load_pool = lambda *args, **kwargs: None
    credential_pool.get_custom_provider_pool_key = lambda *args, **kwargs: None
    credential_pool.credential_pool_matches_provider = lambda *args, **kwargs: False
    sys.modules["agent.credential_pool"] = credential_pool

    # H2 accepts a frozen parent-projected model profile and does not read
    # upstream YAML configuration or provider-specific settings. These narrow
    # helpers satisfy AIAgent's optional configuration paths without allowing a
    # child config file to alter Noruct's selected capability boundary.
    config = ModuleType("hermes_cli.config")

    def cfg_get(cfg, *keys, default=None):
        value = cfg
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    config.cfg_get = cfg_get
    config.load_config = lambda: {}
    config.load_config_readonly = lambda: {}
    config.load_env = lambda: {}
    config.get_hermes_home = lambda: Path(os.environ["HERMES_HOME"])
    config.get_config_path = lambda: Path(os.environ["HERMES_HOME"]) / "config.yaml"
    config.get_compatible_custom_providers = lambda *args, **kwargs: []
    config.get_custom_provider_context_length = lambda *args, **kwargs: None
    config.apply_custom_provider_tls_to_client_kwargs = lambda *args, **kwargs: None
    config.apply_custom_provider_extra_headers_to_client_kwargs = (
        lambda *args, **kwargs: None
    )
    config.ensure_hermes_home = lambda *args, **kwargs: None

    def unavailable_config_member(name: str):
        # Upstream feature modules occasionally import a config helper only to
        # gate an optional local side effect. Keep those paths inert instead of
        # importing the full user-managed configuration surface into the child.
        if name.startswith("is_"):
            return lambda *args, **kwargs: False
        if name.startswith("load_"):
            return lambda *args, **kwargs: {}
        return lambda *args, **kwargs: None

    config.__getattr__ = unavailable_config_member
    sys.modules["hermes_cli.config"] = config

    # Model discovery, live catalog refresh, pricing, provider-specific
    # headers and local-server lifecycle belong to the Noruct parent.  The
    # retained loop only reaches this module through optional Copilot, GitHub
    # Models, LM Studio and credit-reporting branches.  Keep their negative
    # predicates and inert values available without importing the upstream
    # catalog, plugin-provider discovery and its unrelated source surface.
    models = ModuleType("hermes_cli.models")
    models.ensure_lmstudio_model_loaded = lambda *args, **kwargs: None
    models._should_use_copilot_responses_api = lambda *args, **kwargs: False
    models.copilot_default_headers = lambda *args, **kwargs: {}
    models.github_model_reasoning_efforts = lambda *args, **kwargs: []
    models.lmstudio_model_reasoning_options = lambda *args, **kwargs: []
    models._is_model_free = lambda *args, **kwargs: False
    models._pricing_cache = {}
    models.__getattr__ = unavailable_model_metadata_member
    sys.modules["hermes_cli.models"] = models

    # Noruct supplies the complete system/task/skill projection from the
    # parent.  The upstream prompt builder otherwise adds vendor-specific
    # subscription, local-environment, plugin and execution guidance after
    # that authority boundary.  Keep only the import-level values used by
    # the retained loop and deliberately contribute no child-owned prompt
    # content.  A real product prompt change belongs to the first-party
    # PromptBuilder and its audited parent projection, never this worker.
    prompt_builder = ModuleType("agent.prompt_builder")
    prompt_builder.DEFAULT_AGENT_IDENTITY = "Noruct Employee Runtime"
    prompt_builder.DEVELOPER_ROLE_MODELS = set()
    prompt_builder.KANBAN_GUIDANCE = ""
    prompt_builder.build_skills_system_prompt = lambda *args, **kwargs: ""
    prompt_builder.build_context_files_prompt = lambda *args, **kwargs: ""
    prompt_builder.build_environment_hints = lambda *args, **kwargs: ""
    prompt_builder.build_nous_subscription_prompt = lambda *args, **kwargs: ""
    prompt_builder.load_soul_md = lambda *args, **kwargs: ""
    prompt_builder.format_steer_marker = lambda value, *args, **kwargs: str(value or "")
    prompt_builder._scan_context_content = lambda *args, **kwargs: ""
    prompt_builder.__getattr__ = lambda name: ""
    sys.modules["agent.prompt_builder"] = prompt_builder

    # Responses API transport normalization is a parent responsibility.  The
    # retained child only needs a text summary for its local transcript and
    # deterministic tool-id helpers so an upstream-shaped message remains
    # internally consistent.  Do not load the broader Codex/OpenAI response
    # conversion surface or let it become a second provider boundary.
    import hashlib
    import re
    import uuid

    codex_responses = ModuleType("agent.codex_responses_adapter")

    def summarize_user_message_for_log(content, *, sep=" ") -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts: list[str] = []
            images = 0
            for part in content:
                if isinstance(part, str) and part:
                    texts.append(part)
                elif isinstance(part, dict):
                    kind = str(part.get("type") or "").strip().lower()
                    if kind in {"text", "input_text", "output_text"} and isinstance(part.get("text"), str):
                        texts.append(part["text"])
                    elif kind in {"image_url", "input_image"}:
                        images += 1
            rendered = sep.join(texts).strip()
            if images:
                marker = f"[{images} image{'s' if images != 1 else ''}]"
                return f"{marker} {rendered}" if rendered else marker
            return rendered
        return str(content)

    def deterministic_call_id(name: str, arguments: str, index: int = 0) -> str:
        digest = hashlib.sha256(f"{name}:{arguments}:{index}".encode("utf-8", "replace")).hexdigest()[:12]
        return f"call_{digest}"

    def split_responses_tool_id(raw_id):
        if not isinstance(raw_id, str) or not raw_id.strip():
            return None, None
        value = raw_id.strip()
        if "|" in value:
            call_id, item_id = value.split("|", 1)
            return call_id.strip() or None, item_id.strip() or None
        return (None, value) if value.startswith("fc_") else (value, None)

    def derive_responses_function_call_id(call_id: str, response_item_id=None) -> str:
        if isinstance(response_item_id, str) and response_item_id.strip().startswith("fc_"):
            return response_item_id.strip()
        source = str(call_id or "").strip()
        if source.startswith("fc_"):
            return source
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "", source.removeprefix("call_"))
        return f"fc_{cleaned[:48]}" if cleaned else f"fc_{uuid.uuid4().hex}"

    codex_responses._summarize_user_message_for_log = summarize_user_message_for_log
    codex_responses._deterministic_call_id = deterministic_call_id
    codex_responses._split_responses_tool_id = split_responses_tool_id
    codex_responses._derive_responses_function_call_id = derive_responses_function_call_id
    codex_responses.__getattr__ = unavailable_model_metadata_member
    sys.modules["agent.codex_responses_adapter"] = codex_responses

    timeouts = ModuleType("hermes_cli.timeouts")
    timeouts.get_provider_request_timeout = lambda *args, **kwargs: None
    timeouts.get_provider_stale_timeout = lambda *args, **kwargs: None
    sys.modules["hermes_cli.timeouts"] = timeouts

    plugins = ModuleType("hermes_cli.plugins")
    plugins.resolve_pre_tool_block = lambda *args, **kwargs: None

    class _NoPluginManager:
        _middleware: dict[str, list] = {}

        def has_middleware(self, *args, **kwargs) -> bool:
            return False

        def has_hook(self, *args, **kwargs) -> bool:
            return False

        def invoke_hook(self, *args, **kwargs) -> list:
            return []

        def invoke_middleware(self, *args, **kwargs) -> list:
            return []

    no_plugins = _NoPluginManager()
    plugins.get_plugin_manager = lambda: no_plugins
    plugins.invoke_hook = no_plugins.invoke_hook
    plugins.invoke_middleware = no_plugins.invoke_middleware
    plugins.has_middleware = no_plugins.has_middleware
    plugins.has_hook = no_plugins.has_hook
    sys.modules["hermes_cli.plugins"] = plugins

    # Plugin hooks are deliberately inert in the employee worker.  The
    # upstream turn context still imports its optional hook-output spill
    # helper before it discovers that the parent-owned plugin manager returns
    # no hooks.  Supply the tiny no-op contract here so an unrelated Codex
    # provenance-bearing helper is neither imported nor distributed by the
    # traced Employee Runtime capsule.
    hook_output_spill = ModuleType("tools.hook_output_spill")
    hook_output_spill.get_spill_config = lambda *args, **kwargs: None
    hook_output_spill.spill_if_oversized = lambda value, *args, **kwargs: value
    sys.modules["tools.hook_output_spill"] = hook_output_spill

    # The retained agent loop only uses gateway.session_context as a small
    # process-local session-id/environment bridge. Importing that submodule
    # normally first executes gateway/__init__.py, which eagerly imports the
    # unrelated messaging GatewayConfig surface (and its platform policy,
    # delivery and persistence modules). Noruct owns the durable session and
    # does not expose gateway operation in this worker. Preserve the narrow
    # context contract without distributing that unrelated package tree.
    gateway = ModuleType("gateway")
    gateway.__path__ = []  # type: ignore[attr-defined]
    gateway_session_context = ModuleType("gateway.session_context")

    def get_session_env(name: str, default: object = "") -> object:
        return os.environ.get(str(name), default)

    def set_current_session_id(session_id: object) -> None:
        os.environ["HERMES_SESSION_ID"] = str(session_id or "")

    gateway_session_context.get_session_env = get_session_env
    gateway_session_context.set_current_session_id = set_current_session_id
    gateway.session_context = gateway_session_context
    sys.modules["gateway"] = gateway
    sys.modules["gateway.session_context"] = gateway_session_context

    # The capsule's agent/redact.py is byte-identical to Noruct's separately
    # registered runtime-safety redactor.  Reuse that private source module so
    # the worker retains the exact redaction behavior without distributing a
    # second copy through the Employee Runtime capsule.
    from dynamic_firm._vendor.runtime_safety import redact as runtime_safety_redact

    agent_redact = ModuleType("agent.redact")
    agent_redact.__getattr__ = lambda name: getattr(runtime_safety_redact, name)
    sys.modules["agent.redact"] = agent_redact
