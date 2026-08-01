"""Coverage inventory for the full private employee-runtime foundation.

This is deliberately an intake/activation map, not an upstream command
launcher.  It makes every vendored source family visible to Noruct operators
and tests so a future capability migration cannot quietly regress to a small
trace-only subset.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from .source import FoundationSourceError, verify_foundation_source


_MANIFEST_PATH = (
    Path(__file__).parents[1] / "_vendor" / "hermes_agent" / "UPSTREAM_MANIFEST.json"
)

# The categories are product-neutral.  They describe source families rather
# than exposing upstream names or types in any Noruct runtime contract.
_FAMILY_PREFIXES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("employee-core", ("agent/", "run_agent.py"), "active-private-core"),
    ("interactive-shell", ("hermes_cli/", "tui_gateway/", "cli.py"), "bridged-product-shell"),
    ("tool-lifecycle", ("tools/", "toolsets.py", "toolset_distributions.py", "model_tools.py"), "parent-authority-bridge"),
    ("provider-and-auth", ("providers/", "plugins/model-providers/"), "parent-provider-bridge"),
    ("gateway-and-channels", ("gateway/", "plugins/platforms/"), "operator-supervisor-bridge"),
    ("automation", ("cron/",), "operator-started-bridge"),
    ("editor-protocol", ("acp_adapter/",), "local-editor-bridge"),
    ("plugins-and-extensions", ("plugins/",), "reviewed-package-bridge"),
)


def _family_for(path: str) -> tuple[str, str]:
    for family, prefixes, activation in _FAMILY_PREFIXES:
        if any(path == prefix or path.startswith(prefix) for prefix in prefixes):
            return family, activation
    return "foundation-support", "active-private-core"


def foundation_capability_inventory() -> dict[str, Any]:
    """Return a deterministic, content-free map of every vendored source file.

    The output distinguishes physical full-source intake from activation mode.
    ``bridged`` means the source family is available through Noruct's public
    contracts; it never means the child process owns credentials, effects, or
    durable state.
    """

    source = verify_foundation_source()
    try:
        raw = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FoundationSourceError("invalid full foundation inventory manifest") from exc
    files = raw.get("files")
    if not isinstance(files, list):
        raise FoundationSourceError("full foundation inventory has no file list")

    grouped: dict[str, list[str]] = defaultdict(list)
    activation: dict[str, str] = {}
    for item in files:
        if not isinstance(item, Mapping) or not isinstance(item.get("upstream_path"), str):
            raise FoundationSourceError("full foundation inventory has an invalid file entry")
        path = item["upstream_path"]
        family, mode = _family_for(path)
        grouped[family].append(path)
        activation[family] = mode

    family_rows = tuple(
        {
            "family": family,
            "source_file_count": len(paths),
            "activation_mode": activation[family],
            "representative_paths": tuple(sorted(paths)[:5]),
        }
        for family, paths in sorted(grouped.items())
    )
    categorized = sum(int(row["source_file_count"]) for row in family_rows)
    if categorized != len(files):
        raise FoundationSourceError("full foundation inventory did not classify every source file")
    modes = Counter(str(row["activation_mode"]) for row in family_rows)
    return {
        "schema_version": "noruct.employee-foundation-inventory.v1",
        "product_identity": "noruct",
        "source_file_count": len(files),
        "verified_source_file_count": int(source["file_count"]),
        "tree_sha256": source["tree_sha256"],
        "complete_source_intake": len(files) == int(source["file_count"]),
        "activation_mode_counts": dict(sorted(modes.items())),
        "families": family_rows,
        "authority": {
            "company_state": "noruct",
            "provider_credentials": "noruct-parent",
            "tool_effects_and_approvals": "noruct-parent",
            "durable_session": "noruct",
        },
    }
