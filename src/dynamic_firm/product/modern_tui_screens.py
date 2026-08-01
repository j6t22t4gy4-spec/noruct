"""Optional Textual modal components for the Noruct modern terminal.

These screens are presentation-only.  They return a bounded local command to
the owning terminal controller; Company state and authority remain outside the
Textual component tree.
"""

from __future__ import annotations

from typing import Any


def create_terminal_modal_screens() -> tuple[type[Any], ...]:
    """Create optional-framework modal classes after Textual is available."""

    from textual.app import ComposeResult
    from textual.css.query import NoMatches
    from textual.containers import Container, Grid, Horizontal
    from textual.screen import ModalScreen
    from textual.widgets import Button, Input, Static
    from dynamic_firm.product.modern_tui_secondary_screens import (
        create_secondary_terminal_screens,
    )
    from dynamic_firm.product.modern_tui_settings_screen import (
        create_settings_screen,
    )

    SettingsScreen = create_settings_screen(
        ComposeResult=ComposeResult,
        NoMatches=NoMatches,
        Container=Container,
        Grid=Grid,
        Horizontal=Horizontal,
        ModalScreen=ModalScreen,
        Button=Button,
        Input=Input,
        Static=Static,
    )
    ApprovalScreen, ModelScreen, GraphControlScreen, JobAuditScreen = create_secondary_terminal_screens(
        ComposeResult=ComposeResult,
        Container=Container,
        Grid=Grid,
        Horizontal=Horizontal,
        ModalScreen=ModalScreen,
        Button=Button,
        Input=Input,
        Static=Static,
    )


    return ApprovalScreen, SettingsScreen, ModelScreen, GraphControlScreen, JobAuditScreen
