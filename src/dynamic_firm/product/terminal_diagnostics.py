"""Privacy-bounded diagnostics for failures in the optional terminal surface.

The terminal must leave an operator-visible breadcrumb when its UI fails, but
it must never turn prompts, model output, tool arguments, or exception text
into a local diagnostic corpus.  This module records only a timestamp, a
controlled failure location, exception class, and bounded source stack.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import traceback


_MAX_LOG_BYTES = 512 * 1024
_RETAIN_LOG_BYTES = 256 * 1024


def modern_terminal_crash_log_path() -> Path:
    """Return the local, user-owned path for optional terminal diagnostics."""

    return Path.home() / ".noruct" / "logs" / "modern-terminal-crashes.log"


def record_modern_terminal_crash(
    exc: BaseException,
    *,
    phase: str,
    path: Path | None = None,
) -> Path | None:
    """Append a redacted terminal failure record and return its path when saved.

    ``str(exc)`` and ``repr(exc)`` are deliberately never persisted: both can
    contain workspace paths, model output, credentials, or user-provided text.
    Failure logging itself is best effort and cannot replace the original
    exception or destabilize the terminal.
    """

    path = path or modern_terminal_crash_log_path()
    frames = traceback.extract_tb(exc.__traceback__)[-40:]
    stack = "".join(
        f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
        for frame in frames
    )[-12_000:]
    record = (
        "\n"
        "schema=noruct.modern-terminal-crash.v1\n"
        f"timestamp={datetime.now(timezone.utc).isoformat()}\n"
        f"phase={phase}\n"
        f"exception_type={type(exc).__name__}\n"
        "stack:\n"
        f"{stack or '<no Python traceback available>'}\n"
    )
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > _MAX_LOG_BYTES:
            retained = path.read_bytes()[-_RETAIN_LOG_BYTES:]
            path.write_bytes(retained)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record)
    except OSError:
        return None
    return path
