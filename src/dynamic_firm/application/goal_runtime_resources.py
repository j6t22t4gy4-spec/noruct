"""Private Job-scoped resource lifetime bookkeeping for the goal runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dynamic_firm.application.cli_component_contract import cli


@dataclass(slots=True)
class _JobRuntimeResources:
    """Own the resources acquired for one Job and their teardown order."""

    company_store: Any
    graph_blueprint_registry: Any
    run_store: Any | None = None
    employee_service: Any | None = None
    session_recall_store: Any | None = None
    _closed: bool = False

    @classmethod
    def acquire(cls, state_path: Path) -> "_JobRuntimeResources":
        company_store = cli.CompanyStateStore(state_path)
        try:
            graph_blueprint_registry = cli.SQLiteGraphBlueprintRegistry(
                state_path.with_name(f"{state_path.stem}.graph-blueprints.db")
            )
        except BaseException:
            company_store.close()
            raise
        return cls(
            company_store=company_store,
            graph_blueprint_registry=graph_blueprint_registry,
        )

    async def acquire_run_store(self, state_path: Path) -> Any:
        try:
            self.run_store = cli.RunStore(state_path)
        except BaseException:
            await self.close()
            raise
        return self.run_store

    async def create_employee_service(self, **kwargs: Any) -> Any:
        from dynamic_firm.foundation.runtime import NoructEmployeeRuntimeService

        try:
            self.employee_service = NoructEmployeeRuntimeService(**kwargs)
        except BaseException:
            await self.close()
            raise
        return self.employee_service

    def set_employee_service(self, service: Any) -> Any:
        self.employee_service = service
        return service

    def set_session_recall_store(self, store: Any | None) -> Any | None:
        self.session_recall_store = store
        return store

    async def close(self) -> None:
        """Close each acquired resource once, in the established Job order."""

        if self._closed:
            return
        self._closed = True
        if self.employee_service is not None:
            await self.employee_service.close()
        if self.run_store is not None:
            self.run_store.close()
        if self.session_recall_store is not None:
            self.session_recall_store.close()
        self.graph_blueprint_registry.close()
        self.company_store.close()
