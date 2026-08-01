"""Lazy Textual future-Job Graph control modal."""

from __future__ import annotations

from typing import Any, Mapping


def create_graph_control_screen(
    *,
    ComposeResult: Any,
    Container: Any,
    Grid: Any,
    Horizontal: Any,
    ModalScreen: Any,
    Button: Any,
    Input: Any,
    Static: Any,
) -> type[Any]:
    """Create the inert future-Job Graph control modal after Textual is present."""

    class GraphControlScreen(ModalScreen[dict[str, object] | None]):
        """Stage future-Job Graph preferences without owning Company state."""

        # Four editable rows keep the terminal workbench usable on ordinary
        # screens.  The backing Blueprint contract accepts up to 64 tasks;
        # paging is a view concern and never truncates the immutable topology.
        TOPOLOGY_TASK_SLOTS = 4
        MAX_TOPOLOGY_TASKS = 64

        CSS = """
        GraphControlScreen { align: center middle; }
        #graph-control-card { width: 104; max-width: 98%; height: 88%; border: heavy $accent;
          background: $surface; padding: 1 2; overflow-y: auto; }
        #graph-blueprint-grid { grid-size: 2 8; grid-columns: 1fr 1fr;
          grid-rows: 4 4 4 4 4 4 4 4; height: auto; margin-top: 1; }
        .graph-blueprint { background: $panel; color: $text; text-align: left; }
        .graph-selected { background: #315fbe; color: #ffffff; text-style: bold; }
        .graph-policy { background: $panel; color: $text; }
        .graph-policy.graph-selected { background: #2f9e63; color: #ffffff; text-style: bold; }
        #graph-control-status { height: auto; color: $text-muted; margin-top: 1; }
        #graph-replica-detail { height: auto; color: $text-muted; margin-top: 1; }
        #graph-blueprint-lineage { height: auto; color: $text-muted; margin-top: 1; }
        .graph-field { margin-top: 1; }
        """

        def __init__(self, snapshot: Mapping[str, object]) -> None:
            super().__init__()
            self._snapshot = snapshot
            selection = snapshot.get("selection", {})
            self._selected_id = (
                str(selection.get("blueprint_id", ""))
                if isinstance(selection, Mapping)
                else ""
            )
            self._selected_version = (
                int(selection.get("version", 0))
                if isinstance(selection, Mapping) and str(selection.get("version", "")).isdigit()
                else 0
            )
            self._policy = (
                str(selection.get("mutation_policy", "BOUNDED_AUTO"))
                if isinstance(selection, Mapping)
                else "BOUNDED_AUTO"
            )
            self._review = bool(selection.get("require_independent_review", False)) if isinstance(selection, Mapping) else False
            selected = next(
                (item for item in self._blueprints() if self._selected(item)),
                None,
            )
            self._topology_tasks: list[dict[str, object]] = []
            self._topology_page = 0
            self._load_topology(selected)

        def _blueprints(self) -> tuple[Mapping[str, object], ...]:
            items = self._snapshot.get("blueprints", ())
            if not isinstance(items, (tuple, list)):
                return ()
            return tuple(item for item in items if isinstance(item, Mapping))[:16]

        def _selected(self, item: Mapping[str, object]) -> bool:
            return (
                str(item.get("blueprint_id")) == self._selected_id
                and int(item.get("version", 0)) == self._selected_version
            )

        @staticmethod
        def _replica_detail(item: Mapping[str, object] | None) -> str:
            if item is None:
                return "Select a Blueprint to inspect its tasks and same-Employee execution replicas."
            groups = item.get("execution_replica_groups", ())
            if not isinstance(groups, (tuple, list)) or not groups:
                return (
                    "Replica plan · none. This Blueprint uses ordinary persistent Employee tasks only."
                )
            lines = [
                "Replica plan · Job-local runs of one persistent Employee; not extra employees or independent reviewers."
            ]
            for group in groups:
                if not isinstance(group, Mapping):
                    continue
                members = ", ".join(str(value) for value in group.get("member_task_ids", ()))
                lines.append(
                    f"{group.get('group_id')} · {group.get('strategy')} · [{members}] → "
                    f"{group.get('aggregation_task_id')} ({group.get('aggregation')})"
                )
                lines.append(f"Value hypothesis · {group.get('marginal_value_reason_template')}")
            return "\n".join(lines)

        @staticmethod
        def _lineage_detail(item: Mapping[str, object] | None) -> str:
            if item is None:
                return "No Blueprint selected. You can author a small Draft below, then explicitly select it."
            parent = item.get("parent")
            parent_label = (
                f"{parent.get('blueprint_id')}@{parent.get('version')}"
                if isinstance(parent, Mapping)
                else "none"
            )
            profiles = ", ".join(str(value) for value in item.get("execution_profiles", ()))
            receipts = item.get("revision_receipts", ())
            receipt_label = (
                ", ".join(
                    f"v{entry.get('candidate_version')} {entry.get('status')}"
                    for entry in receipts
                    if isinstance(entry, Mapping)
                )
                or "no local revision receipt"
            )
            diff = item.get("revision_diff")
            if isinstance(diff, Mapping):
                changed_tasks = ", ".join(
                    f"{entry.get('task_id')}[{','.join(str(field) for field in entry.get('fields', ())) }]"
                    for entry in diff.get("changed_tasks", ())
                    if isinstance(entry, Mapping)
                )
                change_label = (
                    f"+{','.join(str(value) for value in diff.get('added_task_ids', ())) or '—'} "
                    f"−{','.join(str(value) for value in diff.get('removed_task_ids', ())) or '—'} "
                    f"Δ{changed_tasks or '—'} "
                    f"envelope={','.join(str(value) for value in diff.get('changed_envelope_fields', ())) or '—'}"
                )
            else:
                change_label = "initial revision"
            return (
                f"Revision lineage · parent {parent_label} · profiles {profiles or 'none'}\n"
                f"Revision receipt · {receipt_label}\n"
                f"Structural diff · {change_label}"
            )

        def _workbench_submission(self, action: str) -> dict[str, object]:
            return {
                "intent": "blueprint-action",
                "action": action,
                "source_blueprint_id": self._selected_id or None,
                "source_version": self._selected_version or None,
                "blueprint_id": self.query_one("#graph-draft-id", Input).value.strip(),
                "objective_class": self.query_one("#graph-draft-objective-class", Input).value.strip(),
                "execution_profiles": self.query_one("#graph-draft-profiles", Input).value.strip(),
                "required_capabilities": self.query_one("#graph-draft-capabilities", Input).value.strip(),
                "objective_template": self.query_one("#graph-draft-objective", Input).value.strip(),
                "acceptance_template": self.query_one("#graph-draft-acceptance", Input).value.strip(),
                "rationale": self.query_one("#graph-revision-rationale", Input).value.strip(),
            }

        def _selection_submission(self) -> dict[str, object]:
            return {
                "blueprint_id": self._selected_id or None,
                "version": self._selected_version or None,
                "pinned_employee_ids": self._identifiers(self.query_one("#graph-pinned-employees", Input).value),
                "excluded_employee_ids": self._identifiers(self.query_one("#graph-excluded-employees", Input).value),
                "max_concurrency": self._optional_int(self.query_one("#graph-max-concurrency", Input).value, "Maximum concurrency"),
                "max_cost_usd": self._optional_float(self.query_one("#graph-max-cost", Input).value, "Cost ceiling"),
                "max_wall_time_ms": self._optional_int(self.query_one("#graph-max-wall-time", Input).value, "Time ceiling"),
                "require_independent_review": self._review,
                "mutation_policy": self._policy,
            }

        def _topology_submission(self, action: str) -> dict[str, object]:
            self._flush_topology_page()
            tasks = [
                dict(task)
                for task in self._topology_tasks
                if self._task_has_content(task)
            ]
            if len(tasks) > self.MAX_TOPOLOGY_TASKS:
                raise ValueError("A Graph Blueprint may contain at most 64 tasks")
            return {
                "intent": "blueprint-action",
                "action": action,
                "source_blueprint_id": self._selected_id or None,
                "source_version": self._selected_version or None,
                "blueprint_id": self.query_one("#graph-draft-id", Input).value.strip(),
                "objective_class": self.query_one("#graph-draft-objective-class", Input).value.strip(),
                "execution_profiles": self.query_one("#graph-draft-profiles", Input).value.strip(),
                "rationale": self.query_one("#graph-revision-rationale", Input).value.strip(),
                "topology": {
                    "parameters": self.query_one("#graph-topology-parameters", Input).value.strip(),
                    "final_task_id": self.query_one("#graph-topology-final", Input).value.strip(),
                    "tasks": tasks,
                },
            }

        @staticmethod
        def _task_has_content(task: Mapping[str, object]) -> bool:
            return any(
                bool(task.get(key))
                for key in (
                    "task_id",
                    "objective_template",
                    "depends_on",
                    "required_capabilities",
                    "acceptance_templates",
                )
            )

        @staticmethod
        def _empty_task() -> dict[str, object]:
            return {
                "task_id": "",
                "objective_template": "",
                "depends_on": (),
                "required_capabilities": (),
                "acceptance_templates": (),
                "risk_level": "LOW",
            }

        def _load_topology(self, item: Mapping[str, object] | None) -> None:
            tasks = item.get("editor_tasks", ()) if isinstance(item, Mapping) else ()
            self._topology_tasks = [
                {
                    "task_id": str(task.get("task_id", "")),
                    "objective_template": str(task.get("objective_template", "")),
                    "depends_on": tuple(str(value) for value in task.get("depends_on", ())),
                    "required_capabilities": tuple(str(value) for value in task.get("required_capabilities", ())),
                    "acceptance_templates": tuple(str(value) for value in task.get("acceptance_templates", ())),
                    "risk_level": "LOW",
                }
                for task in tasks
                if isinstance(task, Mapping)
            ][: self.MAX_TOPOLOGY_TASKS]
            if not self._topology_tasks:
                self._topology_tasks = [
                    {
                        "task_id": "execute",
                        "objective_template": "Complete {{objective}}",
                        "depends_on": (),
                        "required_capabilities": ("analysis",),
                        "acceptance_templates": ("Complete {{requested_outcome}}",),
                        "risk_level": "LOW",
                    }
                ]
            self._topology_page = 0

        def _page_start(self) -> int:
            return self._topology_page * self.TOPOLOGY_TASK_SLOTS

        def _page_count(self) -> int:
            return max(
                1,
                self._topology_page + 1,
                (max(len(self._topology_tasks), 1) + self.TOPOLOGY_TASK_SLOTS - 1)
                // self.TOPOLOGY_TASK_SLOTS,
            )

        def _task_for_slot(self, slot: int) -> Mapping[str, object]:
            index = self._page_start() + slot
            return (
                self._topology_tasks[index]
                if index < len(self._topology_tasks)
                else self._empty_task()
            )

        def _flush_topology_page(self) -> None:
            """Stage visible fields before a page/action changes the topology."""

            start = self._page_start()
            for index in range(self.TOPOLOGY_TASK_SLOTS):
                absolute = start + index
                values: dict[str, object] = {
                    "task_id": self.query_one(f"#graph-topology-{index}-id", Input).value.strip(),
                    "objective_template": self.query_one(f"#graph-topology-{index}-objective", Input).value.strip(),
                    "depends_on": self._identifiers(self.query_one(f"#graph-topology-{index}-depends", Input).value),
                    "required_capabilities": self._identifiers(self.query_one(f"#graph-topology-{index}-capabilities", Input).value),
                    "acceptance_templates": self._identifiers(self.query_one(f"#graph-topology-{index}-acceptance", Input).value),
                    "risk_level": "LOW",
                }
                if absolute < len(self._topology_tasks):
                    self._topology_tasks[absolute] = values
                elif self._task_has_content(values):
                    while len(self._topology_tasks) < absolute:
                        self._topology_tasks.append(self._empty_task())
                    self._topology_tasks.append(values)

        def _render_topology_page(self) -> None:
            start = self._page_start()
            for index in range(self.TOPOLOGY_TASK_SLOTS):
                task = self._task_for_slot(index)
                values = {
                    "id": str(task.get("task_id", "")),
                    "objective": str(task.get("objective_template", "")),
                    "depends": ", ".join(str(value) for value in task.get("depends_on", ())),
                    "capabilities": ", ".join(str(value) for value in task.get("required_capabilities", ())),
                    "acceptance": ", ".join(str(value) for value in task.get("acceptance_templates", ())),
                }
                for field, value in values.items():
                    self.query_one(f"#graph-topology-{index}-{field}", Input).value = value
                self.query_one(f"#graph-topology-{index}-label", Static).update(
                    f"TASK {start + index + 1}"
                )
            self.query_one("#graph-topology-page", Static).update(
                f"Tasks {start + 1}–{min(start + self.TOPOLOGY_TASK_SLOTS, self.MAX_TOPOLOGY_TASKS)} "
                f"of 64 · page {self._topology_page + 1}/{self._page_count()}"
            )

        def _populate_topology_editor(self, item: Mapping[str, object] | None) -> None:
            self._flush_topology_page()
            self._load_topology(item)
            parameters = item.get("parameters", ()) if isinstance(item, Mapping) else ()
            self.query_one("#graph-topology-parameters", Input).value = ", ".join(
                str(value) for value in parameters
            ) or "objective, requested_outcome"
            self.query_one("#graph-topology-final", Input).value = (
                str(item.get("final_task_id", "execute"))
                if isinstance(item, Mapping)
                else "execute"
            )
            self._render_topology_page()

        def compose(self) -> ComposeResult:
            selection = self._snapshot.get("selection", {})
            constraints = selection if isinstance(selection, Mapping) else {}
            with Container(id="graph-control-card"):
                yield Static("FUTURE JOB GRAPH CONTROLS", classes="modal-title")
                yield Static(
                    "These are local defaults for a future Job. They never rewrite an active Job, "
                    "grant permission, reserve budget, or start a provider call.",
                    classes="settings-entry", markup=False,
                )
                with Horizontal(classes="settings-actions"):
                    yield Button("Close", id="graph-close-top", variant="primary")
                yield Static("BLUEPRINT", classes="settings-section")
                with Grid(id="graph-blueprint-grid"):
                    for index, item in enumerate(self._blueprints()):
                        label = (
                            f"{item.get('blueprint_id')}@{item.get('version')} · {item.get('origin')}\n"
                            f"{item.get('objective_class')} · {item.get('task_count')} task(s) · "
                            f"{item.get('execution_replica_count', 0)} replica(s)"
                        )
                        yield Button(
                            label,
                            id=f"graph-blueprint-{index}",
                            classes="graph-blueprint graph-selected" if self._selected(item) else "graph-blueprint",
                        )
                with Horizontal(classes="settings-actions"):
                    yield Button("No Blueprint", id="graph-clear-selection", classes="graph-policy")
                selected_item = next(
                    (item for item in self._blueprints() if self._selected(item)),
                    None,
                )
                yield Static(
                    self._replica_detail(selected_item),
                    id="graph-replica-detail",
                    markup=False,
                )
                yield Static(
                    self._lineage_detail(selected_item),
                    id="graph-blueprint-lineage",
                    markup=False,
                )
                yield Static("BLUEPRINT WORKBENCH", classes="settings-section")
                yield Static(
                    "Create saves a small one-task Draft. Fork preserves the selected immutable structure. "
                    "Envelope revision changes only objective class or execution profile; detailed topology remains a typed CLI import/revise operation.",
                    classes="settings-entry",
                    markup=False,
                )
                yield Input(
                    placeholder="New Blueprint id (lowercase, e.g. release_review)",
                    id="graph-draft-id", classes="graph-field",
                )
                yield Input(
                    value=str(selected_item.get("objective_class", "general")) if selected_item else "general",
                    placeholder="Objective class (lowercase identifier)",
                    id="graph-draft-objective-class", classes="graph-field",
                )
                yield Input(
                    value=", ".join(str(value) for value in selected_item.get("execution_profiles", ("read_only",))) if selected_item else "read_only",
                    placeholder="Execution profiles, comma separated",
                    id="graph-draft-profiles", classes="graph-field",
                )
                yield Input(
                    placeholder="Required capabilities, comma separated (e.g. analysis)",
                    id="graph-draft-capabilities", classes="graph-field",
                )
                yield Input(
                    value="Complete {{objective}}",
                    placeholder="Draft task objective template",
                    id="graph-draft-objective", classes="graph-field",
                )
                yield Input(
                    value="Complete {{requested_outcome}}",
                    placeholder="Draft acceptance template",
                    id="graph-draft-acceptance", classes="graph-field",
                )
                yield Input(
                    placeholder="Required for an envelope revision",
                    id="graph-revision-rationale", classes="graph-field",
                )
                with Horizontal(classes="settings-actions"):
                    yield Button("Create Draft", id="graph-create-draft", classes="graph-policy")
                    yield Button("Fork selected", id="graph-fork-selected", classes="graph-policy")
                    yield Button("Revise envelope", id="graph-revise-envelope", classes="graph-policy")
                yield Static("TYPED TOPOLOGY EDITOR", classes="settings-section")
                yield Static(
                    "Each non-empty row is a task. Dependencies, capabilities, parameters, and final task are schema-validated before an immutable Draft or revision is saved.",
                    classes="settings-entry",
                    markup=False,
                )
                yield Input(
                    value=", ".join(str(value) for value in selected_item.get("parameters", ("objective", "requested_outcome"))) if selected_item else "objective, requested_outcome",
                    placeholder="Declared parameters, comma separated",
                    id="graph-topology-parameters", classes="graph-field",
                )
                editor_tasks = [self._task_for_slot(index) for index in range(self.TOPOLOGY_TASK_SLOTS)]
                for index in range(self.TOPOLOGY_TASK_SLOTS):
                    task = editor_tasks[index]
                    task = task if isinstance(task, Mapping) else {}
                    yield Static(f"TASK {index + 1}", id=f"graph-topology-{index}-label", classes="settings-section")
                    yield Input(value=str(task.get("task_id", "execute" if index == 0 and not editor_tasks else "")), placeholder="Task id", id=f"graph-topology-{index}-id", classes="graph-field")
                    yield Input(value=str(task.get("objective_template", "Complete {{objective}}" if index == 0 and not editor_tasks else "")), placeholder="Objective template", id=f"graph-topology-{index}-objective", classes="graph-field")
                    yield Input(value=", ".join(str(value) for value in task.get("depends_on", ())), placeholder="Dependencies, comma separated", id=f"graph-topology-{index}-depends", classes="graph-field")
                    yield Input(value=", ".join(str(value) for value in task.get("required_capabilities", ("analysis",) if index == 0 and not editor_tasks else ())), placeholder="Required capabilities, comma separated", id=f"graph-topology-{index}-capabilities", classes="graph-field")
                    yield Input(value=", ".join(str(value) for value in task.get("acceptance_templates", ("Complete {{requested_outcome}}",) if index == 0 and not editor_tasks else ())), placeholder="Acceptance templates, comma separated", id=f"graph-topology-{index}-acceptance", classes="graph-field")
                with Horizontal(classes="settings-actions"):
                    yield Button("Previous tasks", id="graph-topology-previous", classes="graph-policy")
                    yield Static("Tasks 1–4 of 64 · page 1/1", id="graph-topology-page", classes="settings-entry")
                    yield Button("Next tasks", id="graph-topology-next", classes="graph-policy")
                yield Input(
                    value=str(selected_item.get("final_task_id", "execute")) if selected_item else "execute",
                    placeholder="Final task id",
                    id="graph-topology-final", classes="graph-field",
                )
                with Horizontal(classes="settings-actions"):
                    yield Button("Save topology Draft", id="graph-save-topology-draft", classes="graph-policy")
                    yield Button("Revise selected topology", id="graph-revise-topology", classes="graph-policy")
                yield Static("READ-ONLY FUTURE JOB PREVIEW", classes="settings-section")
                yield Input(
                    placeholder="Future Company goal to bind and validate (does not start a Job)",
                    id="graph-preview-goal", classes="graph-field",
                )
                with Horizontal(classes="settings-actions"):
                    yield Button("Preview saved selection", id="graph-preview-selected", classes="graph-policy")
                yield Static("MUTATION POLICY", classes="settings-section")
                with Horizontal(classes="settings-actions"):
                    for policy in ("LOCKED", "PROPOSE", "BOUNDED_AUTO"):
                        yield Button(
                            policy.replace("_", " ").title(),
                            id=f"graph-policy-{policy.lower()}",
                            classes=(
                                "graph-policy graph-policy-choice graph-selected"
                                if self._policy == policy
                                else "graph-policy graph-policy-choice"
                            ),
                        )
                yield Static("CONSTRAINTS", classes="settings-section")
                yield Input(
                    value=", ".join(constraints.get("pinned_employee_ids", ())),
                    placeholder="Preferred employee ids, comma separated",
                    id="graph-pinned-employees", classes="graph-field",
                )
                yield Input(
                    value=", ".join(constraints.get("excluded_employee_ids", ())),
                    placeholder="Excluded employee ids, comma separated",
                    id="graph-excluded-employees", classes="graph-field",
                )
                yield Input(
                    value=str(constraints.get("max_concurrency") or ""),
                    placeholder="Maximum parallel employees (optional)",
                    id="graph-max-concurrency", classes="graph-field",
                )
                yield Input(
                    value=str(constraints.get("max_cost_usd") or ""),
                    placeholder="Future Job cost ceiling USD (optional)",
                    id="graph-max-cost", classes="graph-field",
                )
                yield Input(
                    value=str(constraints.get("max_wall_time_ms") or ""),
                    placeholder="Future Job time ceiling ms (optional)",
                    id="graph-max-wall-time", classes="graph-field",
                )
                with Horizontal(classes="settings-actions"):
                    yield Button(
                        "Independent review: on" if self._review else "Independent review: off",
                        id="graph-toggle-review",
                        classes="graph-policy graph-selected" if self._review else "graph-policy",
                    )
                    yield Button("Cancel", id="graph-close")
                    yield Button("Done", id="graph-done", variant="success")
                    yield Button("Save & Preview", id="graph-save-preview", variant="primary")
                yield Static("Choose Done to save local future-Job defaults.", id="graph-control-status", markup=False)

        @staticmethod
        def _identifiers(value: str) -> tuple[str, ...]:
            return tuple(item.strip() for item in value.split(",") if item.strip())

        @staticmethod
        def _optional_int(value: str, label: str) -> int | None:
            if not value.strip():
                return None
            parsed = int(value)
            if parsed < 1:
                raise ValueError(f"{label} must be a positive integer")
            return parsed

        @staticmethod
        def _optional_float(value: str, label: str) -> float | None:
            if not value.strip():
                return None
            parsed = float(value)
            if parsed < 0:
                raise ValueError(f"{label} must be non-negative")
            return parsed

        def on_button_pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id or ""
            if button_id in {"graph-close", "graph-close-top"}:
                self.dismiss(None)
                return
            if button_id.startswith("graph-blueprint-"):
                try:
                    selected = self._blueprints()[int(button_id.removeprefix("graph-blueprint-"))]
                except (IndexError, ValueError):
                    return
                self._selected_id = str(selected.get("blueprint_id", ""))
                self._selected_version = int(selected.get("version", 0))
                for button in self.query(".graph-blueprint"):
                    button.remove_class("graph-selected")
                event.button.add_class("graph-selected")
                self.query_one("#graph-replica-detail", Static).update(
                    self._replica_detail(selected)
                )
                self.query_one("#graph-blueprint-lineage", Static).update(
                    self._lineage_detail(selected)
                )
                self._populate_topology_editor(selected)
                return
            if button_id == "graph-clear-selection":
                self._selected_id = ""
                self._selected_version = 0
                for button in self.query(".graph-blueprint"):
                    button.remove_class("graph-selected")
                self.query_one("#graph-replica-detail", Static).update(
                    self._replica_detail(None)
                )
                self.query_one("#graph-blueprint-lineage", Static).update(
                    self._lineage_detail(None)
                )
                self._populate_topology_editor(None)
                return
            if button_id in {"graph-topology-previous", "graph-topology-next"}:
                self._flush_topology_page()
                if button_id == "graph-topology-previous":
                    self._topology_page = max(0, self._topology_page - 1)
                else:
                    maximum_page = (self.MAX_TOPOLOGY_TASKS - 1) // self.TOPOLOGY_TASK_SLOTS
                    self._topology_page = min(maximum_page, self._topology_page + 1)
                self._render_topology_page()
                return
            actions = {
                "graph-create-draft": "create_draft",
                "graph-fork-selected": "fork",
                "graph-revise-envelope": "revise_envelope",
            }
            if button_id in actions:
                if actions[button_id] != "create_draft" and not self._selected_id:
                    self.query_one("#graph-control-status", Static).update(
                        "Choose an exact Blueprint revision before forking or revising it."
                    )
                    return
                self.dismiss(self._workbench_submission(actions[button_id]))
                return
            if button_id in {"graph-save-topology-draft", "graph-revise-topology"}:
                if button_id == "graph-revise-topology" and not self._selected_id:
                    self.query_one("#graph-control-status", Static).update(
                        "Choose an exact Blueprint revision before revising its topology."
                    )
                    return
                self.dismiss(
                    self._topology_submission(
                        "revise_topology" if button_id == "graph-revise-topology" else "save_topology_draft"
                    )
                )
                return
            if button_id == "graph-preview-selected":
                goal = self.query_one("#graph-preview-goal", Input).value.strip()
                if not goal:
                    self.query_one("#graph-control-status", Static).update(
                        "Enter a future Company goal to preview the saved selection."
                    )
                    return
                self.dismiss({"intent": "preview", "goal": goal})
                return
            if button_id.startswith("graph-policy-"):
                self._policy = button_id.removeprefix("graph-policy-").upper()
                for button in self.query(".graph-policy-choice"):
                    button.remove_class("graph-selected")
                event.button.add_class("graph-selected")
                return
            if button_id == "graph-toggle-review":
                self._review = not self._review
                event.button.label = "Independent review: on" if self._review else "Independent review: off"
                event.button.set_class(self._review, "graph-selected")
                return
            if button_id not in {"graph-done", "graph-save-preview"}:
                return
            try:
                submission = self._selection_submission()
            except ValueError as exc:
                self.query_one("#graph-control-status", Static).update(str(exc))
                return
            if button_id == "graph-save-preview":
                goal = self.query_one("#graph-preview-goal", Input).value.strip()
                if not goal:
                    self.query_one("#graph-control-status", Static).update(
                        "Enter a future Company goal before saving and previewing."
                    )
                    return
                submission["intent"] = "selection-preview"
                submission["preview_goal"] = goal
            self.dismiss(submission)
    return GraphControlScreen

