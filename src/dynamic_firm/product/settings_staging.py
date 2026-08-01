"""Framework-free staged command state for the Settings Center.

The Settings Center never applies a change while a user is selecting it.  It
collects bounded local commands and returns them only when the operator chooses
``Done``.  Keeping that state outside the optional Textual screen makes the
transaction order testable by the standard provider-free suite and reusable by
a future GUI host.
"""

from __future__ import annotations

from collections.abc import ValuesView
from dataclasses import dataclass, field


_SERVICE_ACTION_PREFIXES = ("/gateway-service ", "/schedule-service ")


@dataclass(slots=True)
class SettingsCommandDraft:
    """One replace-by-key draft of future local settings commands.

    A later edit to the same logical setting replaces the earlier staged
    command.  Configuration always precedes an explicit local service action:
    starting a gateway or schedule service must observe the just-staged
    configuration when the Settings Center is dismissed.
    """

    _commands: dict[str, str] = field(default_factory=dict)

    def __setitem__(self, key: str, command: str) -> None:
        self._commands[key] = command

    def values(self) -> ValuesView[str]:
        """Expose command values for the presentation layer only."""

        return self._commands.values()

    def __len__(self) -> int:
        return len(self._commands)

    def clear(self) -> None:
        """Discard every staged command without applying it."""

        self._commands.clear()

    def ordered(self) -> tuple[str, ...]:
        """Return the atomic local command batch in safe application order."""

        return tuple(
            command
            for command in sorted(
                self._commands.values(),
                key=lambda value: value.startswith(_SERVICE_ACTION_PREFIXES),
            )
        )

    def summary(self) -> str:
        """Return the bounded command-only representation for the TUI."""

        return " · ".join(self._commands.values())
