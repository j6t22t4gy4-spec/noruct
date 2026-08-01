"""Content-free durable model-invocation receipts owned by ``RunStore``."""

from __future__ import annotations

import sqlite3
import uuid

from dynamic_firm.company.model_invocation_receipt import ModelInvocationReceipt

from .models import EventType, RunStatus, utc_now


class FrozenDispatcherLeaseConflict(ValueError):
    """A frozen run's local dispatcher ownership cannot be inferred safely."""


class RunStoreModelInvocationReceiptMixin:
    """Persist receipts only when their exact frozen binding is durable."""

    def begin_frozen_run_with_dispatch_lease(
        self, run_id: str, *, dispatch_epoch: str
    ) -> RunStatus:
        """Atomically make a frozen run visible as running and locally owned.

        ``RUN_STARTED`` must never be committed for a frozen run without the
        matching dispatcher lease.  A second Store may therefore observe
        either a CREATED frozen run (which ordinary startup recovery is not
        authorized to terminalize) or a running run with its lease, but never
        the unsafe intermediate state.  The lease has no expiry or takeover
        meaning.
        """

        if not isinstance(dispatch_epoch, str) or not dispatch_epoch:
            raise ValueError("model invocation dispatch epoch is required")
        event = None
        with self._transaction() as conn:
            run = conn.execute(
                "SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            current = RunStatus(str(run["status"]))
            if current.terminal:
                return current
            if self._get_frozen_route_binding_in_transaction(conn, run_id) is None:
                raise ValueError("frozen dispatcher lifecycle requires a frozen route binding")
            existing = conn.execute(
                """
                SELECT dispatch_epoch
                FROM employee_run_model_invocation_dispatch_leases
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if existing is not None and str(existing["dispatch_epoch"]) != dispatch_epoch:
                raise FrozenDispatcherLeaseConflict(
                    "Model invocation dispatch has an active different-epoch dispatcher lease"
                )
            if existing is None and current != RunStatus.CREATED:
                raise FrozenDispatcherLeaseConflict(
                    "Model invocation dispatcher lease can first be created only for a CREATED run"
                )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO employee_run_model_invocation_dispatch_leases(
                        run_id, dispatch_epoch, acquired_at
                    ) VALUES (?, ?, datetime('now'))
                    """,
                    (run_id, dispatch_epoch),
                )
            if current == RunStatus.CREATED:
                now = utc_now().isoformat()
                conn.execute(
                    """
                    UPDATE employee_runs
                    SET status = ?, started_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (RunStatus.RUNNING.value, now, now, run_id),
                )
                updated = conn.execute(
                    "SELECT * FROM employee_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                event = self._insert_event(conn, updated, EventType.RUN_STARTED, {})
                current = RunStatus.RUNNING
        if event is not None:
            self._notify(event)
        return current

    @staticmethod
    def _model_invocation_receipt_from_row(
        row: sqlite3.Row,
    ) -> ModelInvocationReceipt:
        raw_json = str(row["receipt_json"])
        receipt = ModelInvocationReceipt.from_canonical_json(raw_json)
        if receipt.canonical_json() != raw_json:
            raise ValueError("Persisted model invocation receipt is not canonical")
        if receipt.digest != str(row["receipt_digest"]):
            raise ValueError("Persisted model invocation receipt digest does not match")
        return receipt

    def store_model_invocation_receipt(
        self, run_id: str, receipt: ModelInvocationReceipt
    ) -> bool:
        """Store one receipt and return whether it was newly appended.

        The store intentionally accepts no provider, model, configuration,
        credential, prompt, or output value.  Attribution is accepted only
        when the receipt repeats the exact immutable binding already frozen
        with this physical run.
        """

        if not isinstance(receipt, ModelInvocationReceipt):
            raise TypeError("receipt must be a ModelInvocationReceipt")
        with self._transaction() as conn:
            run = conn.execute(
                "SELECT 1 FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            binding = self._get_frozen_route_binding_in_transaction(conn, run_id)
            if binding is None:
                raise ValueError("Model invocation receipts require a frozen route binding")
            if receipt.route_binding_digest != binding.digest:
                raise ValueError("Model invocation receipt binding does not match run")

            existing = conn.execute(
                """
                SELECT receipt_json, receipt_digest
                FROM employee_run_model_invocation_receipts
                WHERE run_id = ? AND invocation_id = ?
                """,
                (run_id, receipt.invocation_id),
            ).fetchone()
            if existing is not None:
                persisted = self._model_invocation_receipt_from_row(existing)
                if persisted != receipt:
                    raise ValueError("Invocation id was reused with a different receipt")
                return False

            conn.execute(
                """
                INSERT INTO employee_run_model_invocation_receipts(
                    run_id, invocation_id, receipt_json, receipt_digest, created_at
                ) VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (
                    run_id,
                    receipt.invocation_id,
                    receipt.canonical_json(),
                    receipt.digest,
                ),
            )
        return True

    @staticmethod
    def _assert_reservation_matches_receipt(
        row: sqlite3.Row, receipt: ModelInvocationReceipt
    ) -> None:
        if (
            receipt.invocation_id != str(row["invocation_id"])
            or receipt.route_binding_digest != str(row["route_binding_digest"])
            or receipt.context_projection_digest
            != str(row["context_projection_digest"])
            or receipt.attempt_id != str(row["attempt_id"])
        ):
            raise ValueError("Model invocation receipt does not match dispatch reservation")

    @staticmethod
    def _store_receipt_in_transaction(
        conn: sqlite3.Connection, run_id: str, receipt: ModelInvocationReceipt
    ) -> bool:
        existing = conn.execute(
            """
            SELECT receipt_json, receipt_digest
            FROM employee_run_model_invocation_receipts
            WHERE run_id = ? AND invocation_id = ?
            """,
            (run_id, receipt.invocation_id),
        ).fetchone()
        if existing is not None:
            persisted = RunStoreModelInvocationReceiptMixin._model_invocation_receipt_from_row(existing)
            if persisted != receipt:
                raise ValueError("Invocation id was reused with a different receipt")
            return False
        conn.execute(
            """
            INSERT INTO employee_run_model_invocation_receipts(
                run_id, invocation_id, receipt_json, receipt_digest, created_at
            ) VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (
                run_id,
                receipt.invocation_id,
                receipt.canonical_json(),
                receipt.digest,
            ),
        )
        return True

    def reserve_model_invocation_dispatch(
        self,
        run_id: str,
        *,
        dispatch_epoch: str,
        route_binding_digest: str,
        context_projection_digest: str,
        attempt_id: str,
    ) -> str:
        """Durably reserve a unique frozen physical invocation before dispatch.

        Reservations created in this epoch can coexist for fan-out and must
        finalize independently.  A different epoch is not proof that its
        process died: it is left untouched and blocks a new dispatch until an
        explicit recovery authority resolves it.
        """

        with self._transaction() as conn:
            if not isinstance(dispatch_epoch, str) or not dispatch_epoch:
                raise ValueError("model invocation dispatch epoch is required")
            run = conn.execute(
                "SELECT status FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run["status"]) in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "BUDGET_EXHAUSTED",
            }:
                raise ValueError("Model invocation reservations require a nonterminal run")
            binding = self._get_frozen_route_binding_in_transaction(conn, run_id)
            if binding is None:
                raise ValueError("Model invocation reservations require a frozen route binding")
            if route_binding_digest != binding.digest or attempt_id != binding.attempt_id:
                raise ValueError("Model invocation reservation does not match run binding")
            lease = conn.execute(
                """
                SELECT dispatch_epoch
                FROM employee_run_model_invocation_dispatch_leases
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if lease is None:
                raise ValueError("Model invocation dispatch requires a dispatcher lease")
            if str(lease["dispatch_epoch"]) != dispatch_epoch:
                raise ValueError(
                    "Model invocation dispatch has an active different-epoch dispatcher lease"
                )
            foreign_epoch = conn.execute(
                """
                SELECT invocation_id
                FROM employee_run_model_invocation_dispatch_reservations
                WHERE run_id = ? AND dispatch_epoch != ?
                ORDER BY invocation_id
                """,
                (run_id, dispatch_epoch),
            ).fetchall()
            if foreign_epoch:
                raise ValueError(
                    "Model invocation dispatch has an unresolved different-epoch reservation"
                )
            invocation_id = f"inv-{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO employee_run_model_invocation_dispatch_reservations(
                    run_id, invocation_id, dispatch_epoch, route_binding_digest,
                    context_projection_digest, attempt_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    run_id,
                    invocation_id,
                    dispatch_epoch,
                    route_binding_digest,
                    context_projection_digest,
                    attempt_id,
                ),
            )
        return invocation_id

    def acquire_model_invocation_dispatch_lease(
        self, run_id: str, *, dispatch_epoch: str
    ) -> bool:
        """Acquire the exact frozen run's non-expiring local dispatcher lease.

        The opaque epoch identifies one live service process only.  A missing
        or foreign lease is never interpreted as evidence that another
        process, provider request, or reservation has stopped; recovery stays
        outside this ordinary dispatch path.
        """

        if not isinstance(dispatch_epoch, str) or not dispatch_epoch:
            raise ValueError("model invocation dispatch epoch is required")
        with self._transaction() as conn:
            run = conn.execute(
                "SELECT status FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run["status"]) in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "BUDGET_EXHAUSTED",
            }:
                raise ValueError("Model invocation dispatcher lease requires a nonterminal run")
            binding = self._get_frozen_route_binding_in_transaction(conn, run_id)
            if binding is None:
                raise ValueError("Model invocation dispatcher lease requires a frozen route binding")
            existing = conn.execute(
                """
                SELECT dispatch_epoch
                FROM employee_run_model_invocation_dispatch_leases
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["dispatch_epoch"]) != dispatch_epoch:
                    raise FrozenDispatcherLeaseConflict(
                        "Model invocation dispatch has an active different-epoch dispatcher lease"
                    )
                return False
            conn.execute(
                """
                INSERT INTO employee_run_model_invocation_dispatch_leases(
                    run_id, dispatch_epoch, acquired_at
                ) VALUES (?, ?, datetime('now'))
                """,
                (run_id, dispatch_epoch),
            )
        return True

    def has_model_invocation_dispatch_lease(self, run_id: str) -> bool:
        """Return whether this run retains an opaque frozen dispatcher lease."""

        with self._lock:
            run = self._conn.execute(
                "SELECT 1 FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            lease = self._conn.execute(
                """
                SELECT 1
                FROM employee_run_model_invocation_dispatch_leases
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return lease is not None

    def release_model_invocation_dispatch_lease(
        self, run_id: str, *, dispatch_epoch: str
    ) -> bool:
        """Release an owned lease only after a clean terminal run lifecycle.

        ``False`` means the exact lease remains necessary (the run is not
        terminal, no lease exists, or a physical invocation is still
        reserved).  A foreign epoch is an authority contradiction and is
        refused rather than treated as recovery permission.
        """

        if not isinstance(dispatch_epoch, str) or not dispatch_epoch:
            raise ValueError("model invocation dispatch epoch is required")
        with self._transaction() as conn:
            lease = conn.execute(
                """
                SELECT dispatch_epoch
                FROM employee_run_model_invocation_dispatch_leases
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if lease is None:
                return False
            if str(lease["dispatch_epoch"]) != dispatch_epoch:
                raise ValueError(
                    "Model invocation dispatcher lease belongs to a different epoch"
                )
            run = conn.execute(
                "SELECT status FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            if str(run["status"]) not in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "BUDGET_EXHAUSTED",
            }:
                return False
            outstanding = conn.execute(
                """
                SELECT 1
                FROM employee_run_model_invocation_dispatch_reservations
                WHERE run_id = ?
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if outstanding is not None:
                return False
            conn.execute(
                "DELETE FROM employee_run_model_invocation_dispatch_leases WHERE run_id = ?",
                (run_id,),
            )
        return True

    def finalize_model_invocation_receipt(
        self, run_id: str, receipt: ModelInvocationReceipt
    ) -> bool:
        """Atomically replace one exact dispatch reservation with its receipt."""

        if not isinstance(receipt, ModelInvocationReceipt):
            raise TypeError("receipt must be a ModelInvocationReceipt")
        with self._transaction() as conn:
            recovery = conn.execute(
                "SELECT 1 FROM employee_run_frozen_route_recovery_claims WHERE run_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
            if recovery is not None:
                raise ValueError("Model invocation receipt is sealed by frozen run recovery")
            binding = self._get_frozen_route_binding_in_transaction(conn, run_id)
            if binding is None or receipt.route_binding_digest != binding.digest:
                raise ValueError("Model invocation receipt binding does not match run")
            reservation = conn.execute(
                """
                SELECT invocation_id, route_binding_digest, context_projection_digest, attempt_id
                FROM employee_run_model_invocation_dispatch_reservations
                WHERE run_id = ? AND invocation_id = ?
                """,
                (run_id, receipt.invocation_id),
            ).fetchone()
            if reservation is None:
                existing = conn.execute(
                    """
                    SELECT receipt_json, receipt_digest
                    FROM employee_run_model_invocation_receipts
                    WHERE run_id = ? AND invocation_id = ?
                    """,
                    (run_id, receipt.invocation_id),
                ).fetchone()
                if existing is None:
                    raise ValueError("Model invocation receipt has no dispatch reservation")
                persisted = self._model_invocation_receipt_from_row(existing)
                if persisted != receipt:
                    raise ValueError("Invocation id was reused with a different receipt")
                return False
            self._assert_reservation_matches_receipt(reservation, receipt)
            added = self._store_receipt_in_transaction(conn, run_id, receipt)
            conn.execute(
                """
                DELETE FROM employee_run_model_invocation_dispatch_reservations
                WHERE run_id = ? AND invocation_id = ?
                """,
                (run_id, receipt.invocation_id),
            )
        return added

    def list_model_invocation_dispatch_reservations(self, run_id: str) -> list[str]:
        """Expose only opaque outstanding IDs for recovery verification."""

        with self._lock:
            run = self._conn.execute(
                "SELECT 1 FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            return [
                str(row["invocation_id"])
                for row in self._conn.execute(
                    """
                    SELECT invocation_id
                    FROM employee_run_model_invocation_dispatch_reservations
                    WHERE run_id = ? ORDER BY invocation_id
                    """,
                    (run_id,),
                ).fetchall()
            ]

    def list_model_invocation_receipts(
        self, run_id: str
    ) -> list[ModelInvocationReceipt]:
        """Return strictly verified receipts in stable invocation-id order."""

        with self._lock:
            run = self._conn.execute(
                "SELECT 1 FROM employee_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            rows = self._conn.execute(
                """
                SELECT receipt_json, receipt_digest
                FROM employee_run_model_invocation_receipts
                WHERE run_id = ?
                ORDER BY invocation_id
                """,
                (run_id,),
            ).fetchall()
            if not rows:
                return []
            binding = self._get_frozen_route_binding_in_transaction(self._conn, run_id)
            if binding is None:
                raise ValueError("Model invocation receipts require a frozen route binding")
            receipts = [self._model_invocation_receipt_from_row(row) for row in rows]
        if any(receipt.route_binding_digest != binding.digest for receipt in receipts):
            raise ValueError("Persisted model invocation receipt binding does not match run")
        return receipts
