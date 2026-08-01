from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    kind: str
    base_url: str
    api_key_env: str | None
    transport: str
    service_owner: str
    # A provider can expose a completion protocol without exposing a safe,
    # documented model-list endpoint. Keep the operator's metadata probe
    # opt-in rather than guessing a vendor-specific URL.
    model_list_path: str | None = "/models"
    # Some documented OpenAI-compatible APIs place their catalogue at a
    # sibling route rather than below the chat-completions base URL.
    model_list_url: str | None = None
    # This is fixed profile metadata rather than a user-configurable header
    # map.  It permits documented API-key variants without making the generic
    # transport a credential/header injection mechanism.
    credential_header: str = "Authorization"
    credential_prefix: str = "Bearer "


PROVIDER_PROFILES = {
    "openai_api": ProviderProfile(
        kind="openai_api",
        base_url="",
        api_key_env="NORUCT_API_KEY",
        transport="openai-compatible",
        service_owner="configured-endpoint",
    ),
    "anthropic_api": ProviderProfile(
        kind="anthropic_api",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        transport="anthropic-messages",
        service_owner="Anthropic",
    ),
    "gemini_api": ProviderProfile(
        kind="gemini_api",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        transport="openai-compatible",
        service_owner="Google",
    ),
    "openrouter": ProviderProfile(
        kind="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        transport="openai-compatible",
        service_owner="OpenRouter",
    ),
    "azure_foundry": ProviderProfile(
        kind="azure_foundry",
        # Azure resources are tenant/resource specific.  The operator enters
        # the documented OpenAI-compatible v1 base, e.g.
        # https://RESOURCE.openai.azure.com/openai/v1 .
        base_url="",
        api_key_env="AZURE_FOUNDRY_API_KEY",
        transport="openai-compatible",
        service_owner="Microsoft Foundry",
        # Do not infer a /models endpoint.  The reviewed REST reference
        # establishes chat completions, not this bounded metadata probe.
        model_list_path=None,
        credential_header="api-key",
        credential_prefix="",
    ),
    "vertex": ProviderProfile(
        kind="vertex",
        base_url="",
        api_key_env=None,
        transport="vertex-openai-compatible",
        service_owner="Google Vertex AI",
        model_list_path=None,
    ),
    "nous": ProviderProfile(
        kind="nous",
        base_url="https://inference.nousresearch.com/v1",
        api_key_env="NOUS_API_KEY",
        transport="openai-compatible",
        service_owner="Nous Research",
        model_list_path=None,
    ),
    "bedrock": ProviderProfile(
        kind="bedrock",
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        api_key_env="AWS_BEARER_TOKEN_BEDROCK",
        transport="bedrock-converse",
        service_owner="Amazon Bedrock",
        model_list_path=None,
    ),
    # These provider identities are a deliberately small adaptation of the
    # registered employee foundation's provider registry.  They all retain
    # Noruct's existing parent-owned OpenAI-compatible transport; no provider
    # SDK, plugin hook, credential store, or upstream runtime type crosses
    # the product boundary.
    "deepseek": ProviderProfile(
        kind="deepseek",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        transport="openai-compatible",
        service_owner="DeepSeek",
    ),
    "fireworks": ProviderProfile(
        kind="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY",
        transport="openai-compatible",
        service_owner="Fireworks AI",
    ),
    "xai": ProviderProfile(
        kind="xai",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        transport="openai-compatible",
        service_owner="xAI",
    ),
    "ollama": ProviderProfile(
        kind="ollama",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        transport="openai-compatible",
        service_owner="local-user-runtime",
    ),
    "lmstudio": ProviderProfile(
        kind="lmstudio",
        base_url="http://localhost:1234/v1",
        api_key_env=None,
        transport="openai-compatible",
        service_owner="local-user-runtime",
    ),
    "huggingface": ProviderProfile(
        kind="huggingface",
        base_url="https://router.huggingface.co/v1",
        api_key_env="HF_TOKEN",
        transport="openai-compatible",
        service_owner="Hugging Face Inference Providers",
    ),
    "minimax": ProviderProfile(
        kind="minimax",
        base_url="https://api.minimax.io/anthropic/v1",
        api_key_env="MINIMAX_API_KEY",
        transport="anthropic-messages",
        service_owner="MiniMax",
        # MiniMax documents the Messages endpoint but not a compatible
        # /models metadata endpoint. The normal completion path remains
        # available; `provider preflight` reports this explicitly.
        model_list_path=None,
    ),
    "deepinfra": ProviderProfile(
        kind="deepinfra",
        base_url="https://api.deepinfra.com/v1/openai",
        api_key_env="DEEPINFRA_TOKEN",
        transport="openai-compatible",
        service_owner="DeepInfra",
        # This route is documented separately from the `/v1/openai` base.
        model_list_path=None,
        model_list_url="https://api.deepinfra.com/v1/models",
    ),
    "nvidia": ProviderProfile(
        kind="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key_env="NVIDIA_API_KEY",
        transport="openai-compatible",
        service_owner="NVIDIA NIM",
        # The reviewed NIM material documents chat completions, not a
        # compatible bounded model-list endpoint.
        model_list_path=None,
    ),
    "alibaba": ProviderProfile(
        kind="alibaba",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        transport="openai-compatible",
        service_owner="Alibaba Cloud Model Studio",
        # The reviewed compatibility route establishes chat completions but
        # not a bounded catalogue probe compatible with this adapter.
        model_list_path=None,
    ),
    "zai": ProviderProfile(
        kind="zai",
        base_url="https://api.z.ai/api/paas/v4",
        api_key_env="ZAI_API_KEY",
        transport="openai-compatible",
        service_owner="Z.AI Open Platform",
        # The reviewed general API material establishes chat completions and
        # Bearer API-key authentication, but not a bounded catalogue route.
        # Do not substitute the Coding Plan endpoint: its documentation marks
        # it as intended only for supported coding tools.
        model_list_path=None,
    ),
    "arcee": ProviderProfile(
        kind="arcee",
        base_url="https://api.arcee.ai/api/v1",
        api_key_env="ARCEEAI_API_KEY",
        transport="openai-compatible",
        service_owner="Arcee AI",
        # The reviewed API quick start confirms chat and Bearer key use, but
        # not a bounded model catalogue route.
        model_list_path=None,
    ),
    "gmi": ProviderProfile(
        kind="gmi",
        base_url="https://api.gmi-serving.com/v1",
        api_key_env="GMI_API_KEY",
        transport="openai-compatible",
        service_owner="GMI Cloud Inference Engine",
    ),
    "stepfun": ProviderProfile(
        kind="stepfun",
        base_url="https://api.stepfun.com/v1",
        api_key_env="STEPFUN_API_KEY",
        transport="openai-compatible",
        service_owner="StepFun Open Platform",
        # The official Chat Completions material establishes the endpoint,
        # Bearer-key authentication, streaming and tool calls, but does not
        # document a bounded compatible model-list endpoint.  Do not guess
        # one merely because this is an OpenAI-compatible transport.
        model_list_path=None,
    ),
    "kilo": ProviderProfile(
        kind="kilo",
        base_url="https://api.kilo.ai/api/gateway",
        api_key_env="KILO_API_KEY",
        transport="openai-compatible",
        service_owner="Kilo AI Gateway",
    ),
    "kimi": ProviderProfile(
        kind="kimi",
        base_url="https://api.moonshot.ai/v1",
        api_key_env="MOONSHOT_API_KEY",
        transport="openai-compatible",
        service_owner="Kimi Open Platform",
    ),
    "novita": ProviderProfile(
        kind="novita",
        base_url="https://api.novita.ai/openai/v1",
        api_key_env="NOVITA_API_KEY",
        transport="openai-compatible",
        service_owner="Novita AI",
        # Novita documents its model catalogue at the sibling /v1/models
        # route while chat completions live below /openai/v1.
        model_list_path=None,
        model_list_url="https://api.novita.ai/v1/models",
    ),
    "xiaomi": ProviderProfile(
        kind="xiaomi",
        base_url="https://api.xiaomimimo.com/v1",
        api_key_env="MIMO_API_KEY",
        transport="openai-compatible",
        service_owner="Xiaomi MiMo API Open Platform",
        # The reviewed Chat Completions guide establishes the API base,
        # authentication, streaming and tool wire, but does not establish a
        # bounded /models metadata endpoint for this adapter.
        model_list_path=None,
    ),
    "opencode_zen": ProviderProfile(
        kind="opencode_zen",
        base_url="https://opencode.ai/zen/v1",
        api_key_env="OPENCODE_ZEN_API_KEY",
        transport="openai-compatible",
        service_owner="OpenCode Zen",
        model_list_path=None,
    ),
    "ollama_cloud": ProviderProfile(
        kind="ollama_cloud",
        base_url="https://ollama.com/v1",
        api_key_env="OLLAMA_API_KEY",
        transport="openai-compatible",
        service_owner="Ollama Cloud",
        model_list_path=None,
    ),
    "alibaba_coding": ProviderProfile(
        kind="alibaba_coding",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key_env="ALIBABA_CODING_PLAN_API_KEY",
        transport="openai-compatible",
        service_owner="Alibaba Cloud Coding Plan",
        model_list_path=None,
    ),
}


# The runtime owns this small, audited profile set.  It deliberately describes
# connection *contracts* rather than importing an upstream provider registry:
# OpenAI-compatible HTTP, Anthropic Messages, user-managed local endpoints,
# and the separately handled user-managed Codex executable.
PROVIDER_KINDS = ("openai_codex", "external_exec", *PROVIDER_PROFILES.keys())

# Stable presentation metadata for setup and first-run onboarding.  No secret
# values, account state, or provider-native tokens belong here.
PROVIDER_SETUP_OPTIONS = (
    ("openai_codex", "ChatGPT subscription", "Use an existing Codex CLI or IDE sign-in."),
    ("openai_api", "OpenAI API or compatible endpoint", "Use an API-key environment variable and an HTTPS endpoint."),
    ("anthropic_api", "Anthropic API", "Use an ANTHROPIC_API_KEY environment variable."),
    ("gemini_api", "Google Gemini API", "Use a GEMINI_API_KEY environment variable."),
    ("openrouter", "OpenRouter", "Use an OPENROUTER_API_KEY environment variable."),
    ("deepseek", "DeepSeek", "Use a DEEPSEEK_API_KEY environment variable."),
    ("fireworks", "Fireworks AI", "Use a FIREWORKS_API_KEY environment variable."),
    ("xai", "xAI", "Use an XAI_API_KEY environment variable."),
    ("ollama", "Ollama (local)", "Connect to a user-managed local model server."),
    ("lmstudio", "LM Studio (local)", "Connect to a user-managed local model server."),
    ("huggingface", "Hugging Face Inference Providers", "Use HF_TOKEN with its multi-provider OpenAI-compatible router."),
    ("minimax", "MiniMax API", "Use a MINIMAX_API_KEY environment variable through its Anthropic-compatible API."),
    ("deepinfra", "DeepInfra", "Use a DEEPINFRA_TOKEN environment variable through its OpenAI-compatible API."),
    ("nvidia", "NVIDIA NIM API", "Use a NVIDIA_API_KEY environment variable through its OpenAI-compatible API."),
    ("alibaba", "Alibaba Cloud Model Studio", "Use a DASHSCOPE_API_KEY environment variable through its OpenAI-compatible API."),
    ("zai", "Z.AI Open Platform", "Use a ZAI_API_KEY environment variable through its OpenAI-compatible API."),
    ("arcee", "Arcee AI", "Use an ARCEEAI_API_KEY environment variable through its OpenAI-compatible API."),
    ("gmi", "GMI Cloud", "Use a GMI_API_KEY environment variable through its OpenAI-compatible API."),
    ("stepfun", "StepFun Open Platform", "Use a STEPFUN_API_KEY environment variable through its OpenAI-compatible API."),
    ("kilo", "Kilo AI Gateway", "Use a KILO_API_KEY environment variable through its OpenAI-compatible API."),
    ("kimi", "Kimi Open Platform", "Use a MOONSHOT_API_KEY environment variable through its OpenAI-compatible API."),
    ("novita", "Novita AI", "Use a NOVITA_API_KEY environment variable through its OpenAI-compatible API."),
    ("xiaomi", "Xiaomi MiMo API", "Use a MIMO_API_KEY environment variable through its OpenAI-compatible API."),
    ("opencode_zen", "OpenCode Zen", "Use an OPENCODE_ZEN_API_KEY environment variable through its OpenAI-compatible API."),
    ("ollama_cloud", "Ollama Cloud", "Use an OLLAMA_API_KEY environment variable through its OpenAI-compatible API."),
    ("alibaba_coding", "Alibaba Cloud Coding Plan", "Use an ALIBABA_CODING_PLAN_API_KEY environment variable through its OpenAI-compatible API."),
    # Append new options to preserve the selected-number meaning of existing
    # first-run setup scripts and terminal fixtures.
    ("azure_foundry", "Microsoft Foundry", "Use AZURE_FOUNDRY_API_KEY and your resource-specific OpenAI-compatible v1 endpoint."),
    ("vertex", "Google Vertex AI", "Use your regional OpenAI-compatible endpoint and user-managed gcloud Application Default Credentials."),
    ("nous", "Nous Research API", "Use a NOUS_API_KEY with the documented inference endpoint."),
    ("bedrock", "Amazon Bedrock API key", "Use AWS_BEARER_TOKEN_BEDROCK with the bounded Converse API transport."),
    ("external_exec", "External subscription or OAuth CLI", "Use a user-managed executable implementing Noruct's JSON stdin/stdout bridge; credentials remain in that CLI."),
)


def provider_profile(kind: str) -> ProviderProfile:
    try:
        return PROVIDER_PROFILES[kind]
    except KeyError:
        raise ValueError(f"Unknown provider profile: {kind}") from None
