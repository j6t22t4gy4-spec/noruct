from __future__ import annotations

import asyncio
import os
import unittest

from dynamic_firm.providers.openai_compat import EnvironmentSecretResolver
from dynamic_firm.runtime.ports import ModelProviderError
from dynamic_firm.runtime.secrets import (
    employee_secret_scope,
    require_employee_secret_scope,
    resolve_secret,
)


class EmployeeSecretScopeTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        require_employee_secret_scope(False)

    async def test_parallel_employee_tasks_never_see_each_others_credentials(self) -> None:
        async def resolve_for_employee(value: str) -> str:
            with employee_secret_scope({"EMPLOYEE_MODEL_KEY": value}):
                await asyncio.sleep(0)
                return EnvironmentSecretResolver().resolve("EMPLOYEE_MODEL_KEY")

        first, second = await asyncio.gather(
            resolve_for_employee("employee-a-secret"),
            resolve_for_employee("employee-b-secret"),
        )

        self.assertEqual(first, "employee-a-secret")
        self.assertEqual(second, "employee-b-secret")

    async def test_nested_scope_restores_parent_and_is_authoritative(self) -> None:
        previous = os.environ.get("OTHER_EMPLOYEE_KEY")
        os.environ["OTHER_EMPLOYEE_KEY"] = "process-secret-must-not-leak"
        try:
            with employee_secret_scope({"EMPLOYEE_MODEL_KEY": "outer"}):
                self.assertEqual(resolve_secret("EMPLOYEE_MODEL_KEY"), "outer")
                self.assertIsNone(resolve_secret("OTHER_EMPLOYEE_KEY"))
                with employee_secret_scope({"EMPLOYEE_MODEL_KEY": "inner"}):
                    self.assertEqual(resolve_secret("EMPLOYEE_MODEL_KEY"), "inner")
                self.assertEqual(resolve_secret("EMPLOYEE_MODEL_KEY"), "outer")
        finally:
            if previous is None:
                os.environ.pop("OTHER_EMPLOYEE_KEY", None)
            else:
                os.environ["OTHER_EMPLOYEE_KEY"] = previous

    async def test_required_scope_fails_closed_without_echoing_environment_value(self) -> None:
        secret = "process-secret-must-not-escape"
        previous = os.environ.get("EMPLOYEE_MODEL_KEY")
        os.environ["EMPLOYEE_MODEL_KEY"] = secret
        require_employee_secret_scope(True)
        try:
            with self.assertRaises(ModelProviderError) as raised:
                EnvironmentSecretResolver().resolve("EMPLOYEE_MODEL_KEY")
        finally:
            if previous is None:
                os.environ.pop("EMPLOYEE_MODEL_KEY", None)
            else:
                os.environ["EMPLOYEE_MODEL_KEY"] = previous

        self.assertEqual(raised.exception.code, "MODEL_SECRET_SCOPE_MISSING")
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
