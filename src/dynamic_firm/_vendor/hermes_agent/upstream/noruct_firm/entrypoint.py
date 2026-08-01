"""Noruct application entrypoint hosted by the Hermes-derived fork.

The company shell remains a first-party domain service during the cutover. The
entrypoint lives in the fork so the next migration steps can move command and
TUI ownership into the Hermes CLI modules without changing the installed
`noruct` command again.
"""

from __future__ import annotations

import sys


_PRODUCT_FLAGS = frozenset({"--version", "--help", "-h"})


def _run_fork_or_product(args: list[str]) -> int:
    """Enter the Noruct Company surface; the fork remains its employee core.

    The previous cutover launched the upstream-shaped TUI for a bare
    ``noruct`` invocation.  That made the default ingress bypass Company,
    Knowledge, Intent and Noruct approval surfaces.  The product TUI already
    dispatches employee work through the active fork, so it is the single
    public ingress for both qualified and base installations.
    """

    if sys.modules.get("dynamic_firm.cli") is not None and not args:
        # A test or embedded caller may already own the product shell process.
        # Do not unexpectedly start a second interactive application there.
        from dynamic_firm.cli import main as company_shell_main

        return int(company_shell_main(args))
    from dynamic_firm.cli import main as company_shell_main

    return int(company_shell_main(args))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args and sys.platform != "emscripten":
        return _run_fork_or_product(args)
    if args and args[0] == "--hermes":
        from .fork_cli import main as fork_cli_main

        try:
            return fork_cli_main(args[1:])
        except RuntimeError as exc:
            print(f"noruct: {exc}", file=sys.stderr)
            return 2
    if args and args[0] in {"-q", "--query"}:
        # Preserve the familiar one-shot spelling while keeping the request
        # inside Noruct routing, approval, Company session and Parent tools.
        if len(args) < 2 or not args[1].strip():
            print("noruct: --query requires a non-empty goal", file=sys.stderr)
            return 2
        from dynamic_firm.cli import main as company_shell_main
        return int(company_shell_main(["ask", args[1]]))
    if args and args[0] in _PRODUCT_FLAGS:
        from dynamic_firm.cli import main as company_shell_main
        return int(company_shell_main(args))
    # ``--config`` and future product-global options necessarily precede the
    # command name.  Routing an option-led invocation to the compatibility
    # CLI made an installed ``noruct --config path setup`` silently bypass the
    # Company surface.  The direct foundation CLI remains intentionally
    # available only through the explicit ``--hermes`` diagnostic escape.
    from dynamic_firm.cli import main as company_shell_main

    return int(company_shell_main(args))
