"""Network Settings Center staging actions kept outside the generic event router."""

from __future__ import annotations

from typing import Any


async def handle_network_settings_action(
    owner: Any,
    event: Any,
    *,
    Input: Any,
    Static: Any,
) -> bool:
    """Stage one Network action and report whether this handler consumed it.

    The caller retains the Settings draft and the explicit ``Done`` apply
    boundary.  This helper never installs, activates, rolls back, or contacts
    a Network source itself.
    """

    button_id = event.button.id or ""
    tabs = {
        "settings-network-sources",
        "settings-network-catalog",
        "settings-network-install",
        "settings-network-updates",
        "settings-network-permissions",
        "settings-network-trust",
    }
    if button_id in tabs:
        owner._network_kind = button_id.removeprefix("settings-network-")
        await owner.recompose()
        return True
    if button_id == "settings-network-open":
        commands = {
            "sources": "/network",
            "catalog": "/network search",
            "install": "/network install",
            "updates": "/network updates",
            "permissions": "/network permissions",
            "trust": "/network trust",
        }
        owner._pending["network:inspect"] = commands[owner._network_kind]
        event.button.add_class("settings-selected")
        owner.query_one("#settings-pending", Static).update(
            "Pending Network view. Done opens the selected local lifecycle projection; any source, stage, review, install, activation, rollback, or update-policy mutation still requires its own typed JSON command with confirm=true."
        )
        return True
    if button_id == "settings-network-source-stage":
        def value(selector: str) -> str:
            return owner.query_one(selector, Input).value.strip()

        source_id = value("#settings-network-source-id")
        publisher_class = value("#settings-network-publisher-class").upper()
        origin = value("#settings-network-origin")
        allowed_signers_path = value("#settings-network-allowed-signers")
        signer_principal = value("#settings-network-principal")
        ssh_keygen_path = value("#settings-network-ssh-keygen")
        operator_id = value("#settings-network-operator-id")
        credential_env = value("#settings-network-credential-env")
        private_registry_id = value("#settings-network-private-registry")
        loopback = value("#settings-network-loopback").lower()
        if not all((source_id, origin, allowed_signers_path, signer_principal, ssh_keygen_path, operator_id)):
            owner.query_one("#settings-pending", Static).update(
                "Source ID, origin, signer file, principal, ssh-keygen path, and operator ID are required."
            )
            return True
        if publisher_class not in {"FIRST_PARTY", "COMMUNITY", "PRIVATE_TEAM"}:
            owner.query_one("#settings-pending", Static).update(
                "Publisher class must be FIRST_PARTY, COMMUNITY, or PRIVATE_TEAM."
            )
            return True
        if loopback not in {"yes", "no"}:
            owner.query_one("#settings-pending", Static).update(
                "Insecure loopback must be yes or no."
            )
            return True
        if publisher_class == "PRIVATE_TEAM" and (not credential_env or not private_registry_id):
            owner.query_one("#settings-pending", Static).update(
                "A private-team source requires both its Registry ID and a credential environment-variable name."
            )
            return True
        owner._stage_network_mutation(
            key="source-add",
            action="source-add",
            payload={
                "source_id": source_id,
                "publisher_class": publisher_class,
                "origin": origin,
                "allowed_signers_path": allowed_signers_path,
                "signer_principal": signer_principal,
                "ssh_keygen_path": ssh_keygen_path,
                "operator_id": operator_id,
                "credential_env": credential_env or None,
                "private_registry_id": private_registry_id or None,
                "auto_update_enabled": False,
                "allow_insecure_loopback": loopback == "yes",
            },
            label="the trusted source registration",
            button=event.button,
        )
        return True
    if button_id == "settings-network-search-stage":
        query = owner.query_one("#settings-network-search-query", Input).value.strip()
        if not query:
            owner.query_one("#settings-pending", Static).update("Enter a local catalog search query.")
            return True
        owner._pending["network:search"] = "/network search " + query
        event.button.add_class("settings-selected")
        owner.query_one("#settings-pending", Static).update(
            "Pending local Network catalog search. Done reads only the existing local catalog."
        )
        return True
    if not button_id.startswith("settings-network-"):
        return False

    def value(selector: str) -> str:
        return owner.query_one(selector, Input).value.strip()

    source_id = value("#settings-network-action-source")
    registry_id = value("#settings-network-action-registry")
    snapshot_id = value("#settings-network-action-snapshot")
    operator_id = value("#settings-network-action-operator")
    reason = value("#settings-network-action-reason")
    artifact_id = value("#settings-network-action-artifact")
    version = value("#settings-network-action-version")
    scope_key = value("#settings-network-action-scope")
    capabilities = tuple(
        item.strip() for item in value("#settings-network-action-capabilities").split(",") if item.strip()
    )
    update_mode = value("#settings-network-action-update-mode").upper()
    if button_id == "settings-network-stage-registry":
        if not source_id or not registry_id:
            owner.query_one("#settings-pending", Static).update("Stage requires trusted source ID and Registry ID.")
            return True
        owner._stage_network_mutation(key="stage", action="stage", payload={"source_id": source_id, "registry_id": registry_id}, label="the verified Registry stage", button=event.button)
        return True
    if button_id in {"settings-network-review-approve", "settings-network-review-reject"}:
        if not snapshot_id or not operator_id or not reason:
            owner.query_one("#settings-pending", Static).update("Review requires snapshot ID, operator ID, and a reason.")
            return True
        owner._stage_network_mutation(key="review", action="review", payload={"snapshot_id": snapshot_id, "operator_id": operator_id, "decision": "APPROVE" if button_id.endswith("approve") else "REJECT", "reason": reason}, label="the Registry review", button=event.button)
        return True
    if button_id == "settings-network-install-artifact":
        if not snapshot_id or not artifact_id or not version:
            owner.query_one("#settings-pending", Static).update("Install requires snapshot ID, Artifact ID, and exact version.")
            return True
        owner._stage_network_mutation(key="install", action="install", payload={"snapshot_id": snapshot_id, "artifact_id": artifact_id, "version": version}, label="the inactive Artifact install", button=event.button)
        return True
    if button_id == "settings-network-activate-artifact":
        if not scope_key or not artifact_id or not version:
            owner.query_one("#settings-pending", Static).update("Activate requires scope, Artifact ID, and exact version.")
            return True
        owner._stage_network_mutation(key="activate", action="activate", payload={"scope_key": scope_key, "artifact_id": artifact_id, "version": version, "allowed_capabilities": list(capabilities)}, label="the future-Job Artifact activation", button=event.button)
        return True
    if button_id == "settings-network-rollback-artifact":
        if not scope_key:
            owner.query_one("#settings-pending", Static).update("Rollback requires an activation scope.")
            return True
        owner._stage_network_mutation(key="rollback", action="rollback", payload={"scope_key": scope_key, "artifact_id": artifact_id or None, "kind": None}, label="the future-Job Artifact rollback", button=event.button)
        return True
    if button_id == "settings-network-update-policy":
        if not scope_key or not artifact_id or not source_id:
            owner.query_one("#settings-pending", Static).update("Update policy requires scope, Artifact ID, and source ID.")
            return True
        if update_mode not in {"PINNED", "PROPOSE"}:
            owner.query_one("#settings-pending", Static).update("Update mode must be PINNED or PROPOSE; activation is always explicit.")
            return True
        owner._stage_network_mutation(key="update-mode", action="update-mode", payload={"scope_key": scope_key, "artifact_id": artifact_id, "source_id": source_id, "mode": update_mode}, label="the future-Job update policy", button=event.button)
    return True
