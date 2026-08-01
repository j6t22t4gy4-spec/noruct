"""Private bridge to the exact vendored upstream skill security scanner.

The upstream scanner is run in a short-lived subprocess instead of being
rewritten or imported into Noruct's process-global module namespace.  This
keeps its module/config assumptions private while exposing a Noruct-shaped,
data-only audit record to the product layer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


_UPSTREAM_ROOT = (
    Path(__file__).resolve().parents[1] / "_vendor" / "hermes_agent" / "upstream"
)
_AUDIT_PROGRAM = r"""
import json
import sys
from pathlib import Path
from tools.skills_guard import SCANNER_VERSION, full_content_hash, scan_skill

target = Path(sys.argv[1]).resolve()
source = sys.argv[2]
result = scan_skill(target, source=source)
print(json.dumps({
  "scanner_revision": SCANNER_VERSION,
  "content_hash": full_content_hash(target),
  "skill_name": result.skill_name,
  "source": result.source,
  "trust_level": result.trust_level,
  "verdict": result.verdict,
  "summary": result.summary,
  "findings": [
    {
      "pattern_id": item.pattern_id,
      "severity": item.severity,
      "category": item.category,
      "file": item.file,
      "line": item.line,
      "match": item.match,
      "description": item.description,
    }
    for item in result.findings[:100]
  ],
}, ensure_ascii=False, sort_keys=True))
"""


def audit_user_skill(path: Path, *, timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Scan one already-discovered local skill with the exact upstream guard.

    The caller must resolve and authorize the directory first.  The bridge
    never downloads, installs, executes, or writes inside that directory.
    """

    target = path.expanduser().resolve()
    if not target.is_dir() or not (target / "SKILL.md").is_file():
        raise ValueError("Skill audit requires one discovered SKILL.md directory")
    if not _UPSTREAM_ROOT.is_dir():
        raise RuntimeError("Vendored skill scanner source is unavailable")
    environment = dict(os.environ)
    inherited = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(_UPSTREAM_ROOT), inherited) if item
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _AUDIT_PROGRAM, str(target), "user-configured"],
            cwd=str(_UPSTREAM_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Skill audit exceeded its 15-second read-only budget") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "upstream scanner failed").strip()
        raise ValueError(f"Skill audit could not complete: {detail[:240]}")
    try:
        record = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Skill audit returned an invalid result") from exc
    if not isinstance(record, dict):
        raise ValueError("Skill audit returned an invalid record")
    return record
