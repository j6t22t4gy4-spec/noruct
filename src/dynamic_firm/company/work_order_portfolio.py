"""User-owned Work Order authority and deterministic local portfolio admission.

This module deliberately stops before a scheduler.  It retains the canonical
Work Order body in a user-local store, while the ACTIVE JOB audit keeps only
its digest.  Portfolio decisions decide which submitted orders may be handed
to the existing Company front door; they neither start a Kernel nor replace
the runtime Company's budget/effect checks.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from dynamic_firm.kernel.models import (
    CompanyRunRequest,
    GraphMutationLease,
    JobResult,
    JobStatus,
)
from dynamic_firm.kernel.request_codec import (
    company_run_request_from_envelope,
    request_envelope_payload,
)
from dynamic_firm.kernel.mutation import content_digest
from dynamic_firm.runtime.models import utc_now

from .frontdoor import WorkOrder
from .portfolio_lifecycle import PortfolioLifecycleOperations
from .portfolio_incremental_leases import PortfolioIncrementalLeaseOperations
from .portfolio_reestimate import (
    PortfolioReestimateOperations,
    initialize_portfolio_reestimates,
)
from .portfolio_scheduling_store import (
    initialize_portfolio_scheduling,
    reconcile_scheduling,
    retain_scheduling_envelope,
    scheduling_projection,
    transition_lifecycle,
)
from .work_order_portfolio_models import (
    PortfolioEntry,
    PortfolioIncrementalLease,
    PortfolioJobSettlement,
    PortfolioLifecycleState,
    PortfolioLeaseStatus,
    PortfolioPolicy,
    PortfolioSchedulingEnvelope,
    PortfolioSettlementStatus,
    PortfolioStatus,
    normalize_portfolio_capabilities,
    work_order_from_payload,
)


WORK_ORDER_AUTHORITY_SCHEMA_VERSION = 2
def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_work_order_read_only(path: str | Path, work_order_id: str) -> WorkOrder | None:
    """Read one canonical Work Order without initializing or mutating a store.

    Product projections may need the user-owned purpose bound to an ACTIVE JOB,
    but must never create a portfolio database, run schema setup, or produce a
    WAL side effect merely to render a report.  Missing and unreadable local
    authority therefore remain an explicit absence for the caller to show.
    """

    target = Path(path).expanduser().resolve()
    if not work_order_id or not target.is_file() or target.is_symlink():
        return None
    try:
        connection = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT content_digest, payload_json FROM canonical_work_orders "
                "WHERE work_order_id = ?",
                (work_order_id,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    try:
        order = work_order_from_payload(json.loads(str(row["payload_json"])))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return order if order.content_digest == str(row["content_digest"]) else None


class WorkOrderPortfolioStore(
    PortfolioReestimateOperations,
    PortfolioIncrementalLeaseOperations,
):
    """Local user-owned authority for canonical orders and portfolio decisions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lifecycle = PortfolioLifecycleOperations(self)
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "WorkOrderPortfolioStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _initialize(self) -> None:
        with self._transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_order_authority_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS canonical_work_orders (
                    work_order_id TEXT PRIMARY KEY,
                    content_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_entries (
                    work_order_id TEXT PRIMARY KEY REFERENCES canonical_work_orders(work_order_id),
                    job_id TEXT UNIQUE,
                    priority INTEGER NOT NULL CHECK(priority >= 0 AND priority <= 100),
                    reserved_cost_usd REAL NOT NULL CHECK(reserved_cost_usd >= 0),
                    status TEXT NOT NULL CHECK(status IN ('QUEUED','ADMITTED','DEFERRED','REJECTED','CLOSED')),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS portfolio_ready_idx
                    ON portfolio_entries(status, priority DESC, created_at ASC);
                CREATE TABLE IF NOT EXISTS portfolio_policy (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    max_active_jobs INTEGER NOT NULL,
                    max_reserved_cost_usd REAL NOT NULL,
                    max_incremental_model_calls INTEGER NOT NULL,
                    max_incremental_tool_calls INTEGER NOT NULL,
                    max_incremental_cost_usd REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_incremental_leases (
                    lease_id TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL REFERENCES canonical_work_orders(work_order_id),
                    job_id TEXT NOT NULL,
                    model_calls INTEGER NOT NULL CHECK(model_calls >= 0),
                    tool_calls INTEGER NOT NULL CHECK(tool_calls >= 0),
                    cost_usd REAL NOT NULL CHECK(cost_usd >= 0),
                    status TEXT NOT NULL CHECK(status IN ('RESERVED','SETTLED','RELEASED','FORFEITED')),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS portfolio_incremental_lease_active_idx
                    ON portfolio_incremental_leases(status, created_at, lease_id);
                CREATE INDEX IF NOT EXISTS portfolio_incremental_lease_job_idx
                    ON portfolio_incremental_leases(job_id, status);
                CREATE TABLE IF NOT EXISTS portfolio_job_settlements (
                    job_id TEXT PRIMARY KEY REFERENCES portfolio_entries(job_id),
                    status TEXT NOT NULL CHECK(status IN ('SETTLED','FORFEITED')),
                    terminal_status TEXT NOT NULL,
                    actual_model_calls INTEGER NOT NULL CHECK(actual_model_calls >= 0),
                    actual_tool_calls INTEGER NOT NULL CHECK(actual_tool_calls >= 0),
                    actual_cost_usd REAL NOT NULL CHECK(actual_cost_usd >= 0),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS continuation_request_envelopes (
                    job_id TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL REFERENCES canonical_work_orders(work_order_id),
                    request_id TEXT NOT NULL,
                    frozen_snapshot_hash TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS continuation_request_order_idx
                    ON continuation_request_envelopes(work_order_id);
                CREATE TABLE IF NOT EXISTS frozen_route_continuation_bundles (
                    job_id TEXT PRIMARY KEY REFERENCES continuation_request_envelopes(job_id),
                    request_id TEXT NOT NULL,
                    bundle_digest TEXT NOT NULL,
                    bundle_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            initialize_portfolio_scheduling(conn, now=utc_now().isoformat())
            initialize_portfolio_reestimates(conn)
            conn.execute(
                "INSERT OR REPLACE INTO work_order_authority_meta(key, value) VALUES('schema_version', ?)",
                (str(WORK_ORDER_AUTHORITY_SCHEMA_VERSION),),
            )

    def portfolio_policy(self) -> PortfolioPolicy:
        """Return the saved local planning policy, or the safe one-job default."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM portfolio_policy WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return PortfolioPolicy()
        return PortfolioPolicy(
            max_active_jobs=int(row["max_active_jobs"]),
            max_reserved_cost_usd=float(row["max_reserved_cost_usd"]),
            max_incremental_model_calls=int(row["max_incremental_model_calls"]),
            max_incremental_tool_calls=int(row["max_incremental_tool_calls"]),
            max_incremental_cost_usd=float(row["max_incremental_cost_usd"]),
            capability_slots=tuple(
                (str(item[0]), int(item[1]))
                for item in json.loads(str(row["capability_slots_json"]))
            ),
        )

    def save_portfolio_policy(self, policy: PortfolioPolicy) -> PortfolioPolicy:
        """Save only future local admission bounds; this never dispatches a Job."""

        now = utc_now().isoformat()
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO portfolio_policy(
                       singleton, max_active_jobs, max_reserved_cost_usd,
                       max_incremental_model_calls, max_incremental_tool_calls,
                       max_incremental_cost_usd, capability_slots_json, updated_at
                   ) VALUES(1,?,?,?,?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET
                       max_active_jobs=excluded.max_active_jobs,
                       max_reserved_cost_usd=excluded.max_reserved_cost_usd,
                       max_incremental_model_calls=excluded.max_incremental_model_calls,
                       max_incremental_tool_calls=excluded.max_incremental_tool_calls,
                       max_incremental_cost_usd=excluded.max_incremental_cost_usd,
                       capability_slots_json=excluded.capability_slots_json,
                       updated_at=excluded.updated_at""",
                (
                    policy.max_active_jobs,
                    policy.max_reserved_cost_usd,
                    policy.max_incremental_model_calls,
                    policy.max_incremental_tool_calls,
                    policy.max_incremental_cost_usd,
                    _json(list(policy.capability_slots)),
                    now,
                ),
            )
        return policy

    @staticmethod
    def _entry(row: sqlite3.Row) -> PortfolioEntry:
        return PortfolioEntry(
            work_order_id=str(row["work_order_id"]),
            work_order_digest=str(row["content_digest"]),
            job_id=None if row["job_id"] is None else str(row["job_id"]),
            priority=int(row["priority"]),
            reserved_cost_usd=float(row["reserved_cost_usd"]),
            status=PortfolioStatus(str(row["status"])),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _incremental_lease(row: sqlite3.Row) -> PortfolioIncrementalLease:
        return PortfolioIncrementalLease(
            lease_id=str(row["lease_id"]),
            work_order_id=str(row["work_order_id"]),
            job_id=str(row["job_id"]),
            mutation_lease=GraphMutationLease(
                model_calls=int(row["model_calls"]),
                tool_calls=int(row["tool_calls"]),
                cost_usd=float(row["cost_usd"]),
            ),
            status=PortfolioLeaseStatus(str(row["status"])),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _settlement(row: sqlite3.Row) -> PortfolioJobSettlement:
        return PortfolioJobSettlement(
            job_id=str(row["job_id"]),
            status=PortfolioSettlementStatus(str(row["status"])),
            terminal_status=str(row["terminal_status"]),
            actual_model_calls=int(row["actual_model_calls"]),
            actual_tool_calls=int(row["actual_tool_calls"]),
            actual_cost_usd=float(row["actual_cost_usd"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def retain_work_order(self, order: WorkOrder) -> WorkOrder:
        order.verify()
        payload = order.canonical_payload()
        serialized = _json(payload)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT content_digest, payload_json FROM canonical_work_orders WHERE work_order_id = ?",
                (order.work_order_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["content_digest"]) != order.content_digest or str(existing["payload_json"]) != serialized:
                    raise ValueError("Canonical Work Order identity conflicts")
                return order
            conn.execute(
                "INSERT INTO canonical_work_orders(work_order_id, content_digest, payload_json, created_at) VALUES(?,?,?,?)",
                (order.work_order_id, order.content_digest, serialized, now),
            )
        return order

    def work_order(self, work_order_id: str) -> WorkOrder:
        with self._lock:
            row = self._conn.execute(
                "SELECT content_digest, payload_json FROM canonical_work_orders WHERE work_order_id = ?",
                (work_order_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown Work Order: {work_order_id}")
        payload = json.loads(str(row["payload_json"]))
        order = work_order_from_payload(payload)
        if order.content_digest != str(row["content_digest"]):
            raise ValueError("Canonical Work Order digest is invalid")
        return order

    def retain_continuation_request(self, request: CompanyRunRequest) -> None:
        """Retain one exact, user-local request beside its canonical Work Order.

        This is the only durable source allowed to reconstruct a request for
        ADR-0198 continuation.  The ACTIVE JOB ledger receives only its
        digest, and remote Company coordination receives strictly less.
        """

        if not request.job_id or not request.request_id or not request.work_order_id:
            raise ValueError("Continuation request must bind Job, request, and Work Order")
        order = self.work_order(request.work_order_id)
        if request.work_order_digest != order.content_digest:
            raise ValueError("Continuation request Work Order digest is invalid")
        from dynamic_firm.kernel.mutation import frozen_snapshot_digest

        payload = request_envelope_payload(request)
        serialized = _json(payload)
        digest = content_digest(payload)
        snapshot = frozen_snapshot_digest(request)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT request_id, frozen_snapshot_hash, content_digest, payload_json "
                "FROM continuation_request_envelopes WHERE job_id = ?",
                (request.job_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["request_id"]) != request.request_id
                    or str(existing["frozen_snapshot_hash"]) != snapshot
                    or str(existing["content_digest"]) != digest
                    or str(existing["payload_json"]) != serialized
                ):
                    raise ValueError("Continuation request identity conflicts")
                return
            conn.execute(
                "INSERT INTO continuation_request_envelopes("
                "job_id, work_order_id, request_id, frozen_snapshot_hash, content_digest, payload_json, created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    request.job_id,
                    request.work_order_id,
                    request.request_id,
                    snapshot,
                    digest,
                    serialized,
                    now,
                ),
            )

    def continuation_request(self, job_id: str) -> CompanyRunRequest:
        """Load and verify one user-local continuation request envelope."""

        with self._lock:
            row = self._conn.execute(
                "SELECT work_order_id, request_id, frozen_snapshot_hash, content_digest, payload_json "
                "FROM continuation_request_envelopes WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"No local continuation request for ACTIVE JOB: {job_id}")
        payload = json.loads(str(row["payload_json"]))
        if content_digest(payload) != str(row["content_digest"]):
            raise ValueError("Continuation request envelope digest is invalid")
        request = company_run_request_from_envelope(payload)
        from dynamic_firm.kernel.mutation import frozen_snapshot_digest

        if (
            request.job_id != job_id
            or request.request_id != str(row["request_id"])
            or request.work_order_id != str(row["work_order_id"])
            or frozen_snapshot_digest(request) != str(row["frozen_snapshot_hash"])
        ):
            raise ValueError("Continuation request envelope identity is invalid")
        order = self.work_order(request.work_order_id)
        if request.work_order_digest != order.content_digest:
            raise ValueError("Continuation request no longer matches canonical Work Order")
        return request

    def retain_frozen_route_continuation_bundle(
        self,
        *,
        job_id: str,
        request_id: str,
        bundle_json: str,
        bundle_digest: str,
    ) -> None:
        """Retain immutable frozen-route continuation evidence beside a request."""
        if not all(isinstance(value, str) and value for value in (job_id, request_id, bundle_json)):
            raise ValueError("frozen route continuation bundle identity is invalid")
        if (
            not isinstance(bundle_digest, str)
            or len(bundle_digest) != 64
            or any(character not in "0123456789abcdef" for character in bundle_digest)
        ):
            raise ValueError("frozen route continuation bundle digest is invalid")
        try:
            payload = json.loads(bundle_json)
        except json.JSONDecodeError as exc:
            raise ValueError("frozen route continuation bundle JSON is invalid") from exc
        if _json(payload) != bundle_json or content_digest(payload) != bundle_digest:
            raise ValueError("frozen route continuation bundle is not canonical")
        now = utc_now().isoformat()
        with self._transaction() as conn:
            request = conn.execute(
                "SELECT request_id FROM continuation_request_envelopes WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if request is None or str(request["request_id"]) != request_id:
                raise ValueError("frozen route continuation bundle lacks its request")
            existing = conn.execute(
                "SELECT request_id, bundle_digest, bundle_json FROM frozen_route_continuation_bundles WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["request_id"]) != request_id
                    or str(existing["bundle_digest"]) != bundle_digest
                    or str(existing["bundle_json"]) != bundle_json
                ):
                    raise ValueError("frozen route continuation bundle identity conflicts")
                return
            conn.execute(
                "INSERT INTO frozen_route_continuation_bundles(job_id, request_id, bundle_digest, bundle_json, created_at) VALUES(?,?,?,?,?)",
                (job_id, request_id, bundle_digest, bundle_json, now),
            )

    def frozen_route_continuation_bundle(self, job_id: str) -> tuple[str, str] | None:
        """Return one verified opaque bundle JSON and digest, if the Job is routed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT bundle_digest, bundle_json FROM frozen_route_continuation_bundles WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        bundle_json = str(row["bundle_json"])
        try:
            payload = json.loads(bundle_json)
        except json.JSONDecodeError as exc:
            raise ValueError("persisted frozen route continuation bundle is invalid") from exc
        digest = str(row["bundle_digest"])
        if _json(payload) != bundle_json or content_digest(payload) != digest:
            raise ValueError("persisted frozen route continuation bundle digest is invalid")
        return bundle_json, digest

    def submit(
        self,
        order: WorkOrder,
        *,
        priority: int = 50,
        reserved_cost_usd: float | None = None,
        dependency_work_order_ids: tuple[str, ...] = (),
        deadline_at: datetime | None = None,
        required_capabilities: tuple[str, ...] = (),
    ) -> PortfolioEntry:
        if type(priority) is not int or not 0 <= priority <= 100:
            raise ValueError("Portfolio priority must be between 0 and 100")
        reserved = order.budget_snapshot.max_cost_usd if reserved_cost_usd is None else float(reserved_cost_usd)
        if not math.isfinite(reserved) or reserved < 0 or reserved > order.budget_snapshot.max_cost_usd + 1e-12:
            raise ValueError("Portfolio reserve must fit the Work Order cost ceiling")
        normalized_dependencies = tuple(sorted(set(dependency_work_order_ids)))
        if len(normalized_dependencies) != len(dependency_work_order_ids):
            raise ValueError("Portfolio dependencies must be unique")
        deadline = None
        if deadline_at is not None:
            if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
                raise ValueError("Portfolio deadline must be timezone-aware")
            deadline = deadline_at.isoformat()
        envelope = PortfolioSchedulingEnvelope(
            work_order_id=order.work_order_id,
            dependency_work_order_ids=normalized_dependencies,
            deadline_at=deadline,
            required_capabilities=normalize_portfolio_capabilities(
                required_capabilities
            ),
        )
        self.retain_work_order(order)
        now = utc_now().isoformat()
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT p.*, w.content_digest FROM portfolio_entries p
                   JOIN canonical_work_orders w USING(work_order_id) WHERE p.work_order_id = ?""",
                (order.work_order_id,),
            ).fetchone()
            if row is not None:
                existing = self._entry(row)
                if existing.priority != priority or abs(existing.reserved_cost_usd - reserved) > 1e-12:
                    raise ValueError("Portfolio Work Order submission conflicts")
                retain_scheduling_envelope(
                    conn, envelope, priority=priority, now=now
                )
                return existing
            conn.execute(
                """INSERT INTO portfolio_entries(work_order_id, job_id, priority, reserved_cost_usd, status, reason, created_at, updated_at)
                   VALUES(?,NULL,?,?,'QUEUED','SUBMITTED',?,?)""",
                (order.work_order_id, priority, reserved, now, now),
            )
            retain_scheduling_envelope(conn, envelope, priority=priority, now=now)
            row = conn.execute(
                """SELECT p.*, w.content_digest FROM portfolio_entries p
                   JOIN canonical_work_orders w USING(work_order_id) WHERE p.work_order_id = ?""",
                (order.work_order_id,),
            ).fetchone()
        assert row is not None
        return self._entry(row)

    def reconcile(self, policy: PortfolioPolicy) -> tuple[PortfolioEntry, ...]:
        """Deterministically admit/defer queued local orders; never dispatch them."""

        now = utc_now().isoformat()
        with self._transaction() as conn:
            reconcile_scheduling(conn, policy, now=now)
            rows = conn.execute(
                """SELECT p.*, w.content_digest FROM portfolio_entries p
                   JOIN canonical_work_orders w USING(work_order_id) ORDER BY priority DESC, created_at ASC"""
            ).fetchall()
        return tuple(self._entry(row) for row in rows)

    def bind_job(self, work_order_id: str, *, job_id: str) -> PortfolioEntry:
        if not job_id.strip() or len(job_id) > 160:
            raise ValueError("Portfolio Job identity is invalid")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM portfolio_entries WHERE work_order_id = ?", (work_order_id,)).fetchone()
            if row is None or str(row["status"]) != PortfolioStatus.ADMITTED.value:
                raise ValueError("Only an admitted Work Order may bind a Job")
            if row["job_id"] not in {None, job_id}:
                raise ValueError("Portfolio Work Order is already bound to another Job")
            now = utc_now().isoformat()
            conn.execute("UPDATE portfolio_entries SET job_id = ?, updated_at = ? WHERE work_order_id = ?", (job_id, now, work_order_id))
            transition_lifecycle(
                conn,
                work_order_id=work_order_id,
                target=PortfolioLifecycleState.RUNNING,
                reason="EXPLICIT_DISPATCH_BOUND",
                job_id=job_id,
                now=now,
                allowed_from=frozenset(
                    {PortfolioLifecycleState.QUEUED, PortfolioLifecycleState.BLOCKED}
                ),
            )
            result = conn.execute("""SELECT p.*, w.content_digest FROM portfolio_entries p JOIN canonical_work_orders w USING(work_order_id) WHERE p.work_order_id = ?""", (work_order_id,)).fetchone()
        assert result is not None
        return self._entry(result)

    def defer_work_order(self, work_order_id: str, *, reason: str) -> PortfolioEntry:
        """Return one unbound admission to the deterministic queue safely."""

        normalized = reason.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("Portfolio deferral reason is invalid")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_entries WHERE work_order_id = ?", (work_order_id,)
            ).fetchone()
            if (
                row is None
                or str(row["status"]) != PortfolioStatus.ADMITTED.value
                or row["job_id"] is not None
            ):
                raise ValueError("Only an unbound admitted Work Order may defer")
            conn.execute(
                "UPDATE portfolio_entries SET status = 'DEFERRED', reason = ?, updated_at = ? WHERE work_order_id = ?",
                (normalized, utc_now().isoformat(), work_order_id),
            )
            transition_lifecycle(
                conn,
                work_order_id=work_order_id,
                target=PortfolioLifecycleState.QUEUED,
                reason=normalized,
                job_id=None,
                now=utc_now().isoformat(),
                allowed_from=frozenset({PortfolioLifecycleState.QUEUED}),
            )
            result = conn.execute(
                """SELECT p.*, w.content_digest FROM portfolio_entries p
                   JOIN canonical_work_orders w USING(work_order_id) WHERE p.work_order_id = ?""",
                (work_order_id,),
            ).fetchone()
        assert result is not None
        return self._entry(result)

    def defer_bound_budget_denial(
        self,
        *,
        work_order_id: str,
        job_id: str,
        reason: str = "COMPANY_BUDGET_ADMISSION_DEFERRED",
    ) -> PortfolioEntry:
        """Return a no-lease budget denial to the unbound local queue.

        The ordinary Front Door can report ``BUDGET_EXHAUSTED`` before any
        Company lease or Employee attempt exists.  That terminal audit is
        still durable, but it must not consume this Work Order's local slot or
        be mistaken for an execution result.  A later explicit drain receives
        a new Job id and rechecks the live Company budget.
        """

        normalized = reason.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("Portfolio budget deferral reason is invalid")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_entries WHERE work_order_id = ?", (work_order_id,)
            ).fetchone()
            if (
                row is None
                or str(row["status"]) != PortfolioStatus.ADMITTED.value
                or str(row["job_id"] or "") != job_id
            ):
                raise ValueError("Only the exact bound admitted Work Order may defer after budget denial")
            settled = conn.execute(
                "SELECT 1 FROM portfolio_job_settlements WHERE job_id = ?", (job_id,)
            ).fetchone()
            if settled is not None:
                raise ValueError("A settled portfolio Job cannot return to the queue")
            conn.execute(
                """UPDATE portfolio_entries SET job_id = NULL, status = 'DEFERRED',
                   reason = ?, updated_at = ? WHERE work_order_id = ?""",
                (normalized, utc_now().isoformat(), work_order_id),
            )
            transition_lifecycle(
                conn,
                work_order_id=work_order_id,
                target=PortfolioLifecycleState.BLOCKED,
                reason=normalized,
                job_id=job_id,
                now=utc_now().isoformat(),
                allowed_from=frozenset({PortfolioLifecycleState.RUNNING}),
            )
            result = conn.execute(
                """SELECT p.*, w.content_digest FROM portfolio_entries p
                   JOIN canonical_work_orders w USING(work_order_id) WHERE p.work_order_id = ?""",
                (work_order_id,),
            ).fetchone()
        assert result is not None
        return self._entry(result)

    def close_job(self, job_id: str, *, reason: str) -> PortfolioEntry:
        if not reason.strip() or len(reason) > 128:
            raise ValueError("Portfolio closure reason is invalid")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM portfolio_entries WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown portfolio Job: {job_id}")
            now = utc_now().isoformat()
            conn.execute("UPDATE portfolio_entries SET status = 'CLOSED', reason = ?, updated_at = ? WHERE job_id = ?", (reason, now, job_id))
            transition_lifecycle(
                conn,
                work_order_id=str(row["work_order_id"]),
                target=PortfolioLifecycleState.TERMINAL,
                reason=reason,
                job_id=job_id,
                now=now,
                allowed_from=frozenset(
                    {PortfolioLifecycleState.RUNNING, PortfolioLifecycleState.PAUSED}
                ),
            )
            result = conn.execute("""SELECT p.*, w.content_digest FROM portfolio_entries p JOIN canonical_work_orders w USING(work_order_id) WHERE p.job_id = ?""", (job_id,)).fetchone()
        assert result is not None
        return self._entry(result)

    def pause_job(self, job_id: str, *, reason: str) -> PortfolioLifecycleState:
        """Pause an exact bound Job without releasing its conservative capacity."""
        return self._lifecycle.pause_job(job_id, reason=reason)

    def resume_job(self, job_id: str, *, reason: str) -> PortfolioLifecycleState:
        """Resume only the exact paused binding; this does not reconstruct a Job."""
        return self._lifecycle.resume_job(job_id, reason=reason)

    def cancel_job(
        self,
        job_id: str,
        *,
        reason: str,
        terminal_confirmed: bool,
    ) -> PortfolioLifecycleState:
        """Mirror a confirmed runtime cancellation without assuming zero effects."""
        return self._lifecycle.cancel_job(
            job_id,
            reason=reason,
            terminal_confirmed=terminal_confirmed,
        )

    def scheduling_envelope(self, work_order_id: str) -> PortfolioSchedulingEnvelope:
        return self._lifecycle.scheduling_envelope(work_order_id)

    def replay_lifecycle(self, work_order_id: str) -> tuple[PortfolioLifecycleState, str]:
        """Rebuild one lifecycle from its append-only events and verify current state."""
        return self._lifecycle.replay_lifecycle(work_order_id)

    def settle_job_result(self, result: JobResult) -> PortfolioJobSettlement:
        """Close one bound Job from its exact Kernel result.

        The Company-budget authority remains the only cost authority and has
        already settled or forfeited its lease before callers receive the
        terminal result.  This local mirror has a narrower role: preserve
        observed aggregate usage, close portfolio capacity, and forfeit any
        unclaimed cross-Job mutation reservation.  It never treats a failed
        or interrupted result as zero cost and never releases a lease as
        unused merely because the Job ended.
        """

        if not result.job_id.strip():
            raise ValueError("Portfolio settlement requires a Job identity")
        usage = result.metrics.usage
        if (
            type(usage.model_calls) is not int
            or type(usage.tool_calls) is not int
            or usage.model_calls < 0
            or usage.tool_calls < 0
            or isinstance(usage.cost_usd, bool)
            or not isinstance(usage.cost_usd, (int, float))
            or not math.isfinite(float(usage.cost_usd))
            or float(usage.cost_usd) < 0
        ):
            raise ValueError("Portfolio settlement usage is invalid")
        terminal_status = result.status.value
        status = (
            PortfolioSettlementStatus.SETTLED
            if result.status is JobStatus.SUCCEEDED
            else PortfolioSettlementStatus.FORFEITED
        )
        reason = (
            "KERNEL_TERMINAL_USAGE_SETTLED"
            if status is PortfolioSettlementStatus.SETTLED
            else f"KERNEL_TERMINAL_{terminal_status}_RESERVATION_FORFEITED"
        )
        now = utc_now().isoformat()
        with self._transaction() as conn:
            entry = conn.execute(
                "SELECT status FROM portfolio_entries WHERE job_id = ?", (result.job_id,)
            ).fetchone()
            if entry is None:
                raise KeyError(f"Unknown portfolio Job: {result.job_id}")
            existing = conn.execute(
                "SELECT * FROM portfolio_job_settlements WHERE job_id = ?", (result.job_id,)
            ).fetchone()
            if existing is not None:
                prior = self._settlement(existing)
                expected = (
                    status,
                    terminal_status,
                    usage.model_calls,
                    usage.tool_calls,
                    float(usage.cost_usd),
                    reason,
                )
                actual = (
                    prior.status,
                    prior.terminal_status,
                    prior.actual_model_calls,
                    prior.actual_tool_calls,
                    prior.actual_cost_usd,
                    prior.reason,
                )
                if actual != expected:
                    raise ValueError("Portfolio Job settlement identity conflicts")
                return prior
            if str(entry["status"]) != PortfolioStatus.ADMITTED.value:
                raise ValueError("Only an admitted portfolio Job may settle")
            conn.execute(
                """INSERT INTO portfolio_job_settlements(
                       job_id, status, terminal_status, actual_model_calls,
                       actual_tool_calls, actual_cost_usd, reason, created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    result.job_id,
                    status.value,
                    terminal_status,
                    usage.model_calls,
                    usage.tool_calls,
                    float(usage.cost_usd),
                    reason,
                    now,
                    now,
                ),
            )
            # A local mutation reservation is only RELEASED with an explicit
            # Kernel-confirmed unused receipt. Ordinary terminal completion
            # must conservatively forfeit its remaining promise.
            conn.execute(
                """UPDATE portfolio_incremental_leases
                   SET status = 'FORFEITED', reason = ?, updated_at = ?
                   WHERE job_id = ? AND status = 'RESERVED'""",
                ("JOB_TERMINAL_UNSETTLED_INCREMENTAL_RESERVATION", now, result.job_id),
            )
            conn.execute(
                """UPDATE portfolio_entries SET status = 'CLOSED', reason = ?, updated_at = ?
                   WHERE job_id = ?""",
                (reason, now, result.job_id),
            )
            work_order_row = conn.execute(
                "SELECT work_order_id FROM portfolio_entries WHERE job_id = ?",
                (result.job_id,),
            ).fetchone()
            assert work_order_row is not None
            transition_lifecycle(
                conn,
                work_order_id=str(work_order_row["work_order_id"]),
                target=PortfolioLifecycleState.TERMINAL,
                reason=reason,
                job_id=result.job_id,
                now=now,
                allowed_from=frozenset(
                    {PortfolioLifecycleState.RUNNING, PortfolioLifecycleState.PAUSED}
                ),
            )
            row = conn.execute(
                "SELECT * FROM portfolio_job_settlements WHERE job_id = ?", (result.job_id,)
            ).fetchone()
        assert row is not None
        return self._settlement(row)

    def settlement_projection(self) -> tuple[Mapping[str, object], ...]:
        """Return terminal aggregate usage without request or result content."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM portfolio_job_settlements ORDER BY created_at, job_id"
            ).fetchall()
        return tuple(
            {
                "job_id": item.job_id,
                "status": item.status.value,
                "terminal_status": item.terminal_status,
                "actual_model_calls": item.actual_model_calls,
                "actual_tool_calls": item.actual_tool_calls,
                "actual_cost_usd": item.actual_cost_usd,
                "reason": item.reason,
                "updated_at": item.updated_at,
            }
            for item in (self._settlement(row) for row in rows)
        )

    def operator_projection(self) -> tuple[Mapping[str, object], ...]:
        """Return status-only records suitable for CLI, TUI, or a future GUI."""

        with self._lock:
            scheduling = scheduling_projection(self._conn)
            rows = self._conn.execute(
                """SELECT p.*, w.content_digest FROM portfolio_entries p
                   JOIN canonical_work_orders w USING(work_order_id) ORDER BY priority DESC, created_at ASC"""
            ).fetchall()
        return tuple(
            {
                "work_order_id": str(row["work_order_id"]),
                "work_order_digest": str(row["content_digest"]),
                "job_id": None if row["job_id"] is None else str(row["job_id"]),
                "priority": int(row["priority"]),
                "reserved_cost_usd": float(row["reserved_cost_usd"]),
                "status": str(row["status"]),
                "reason": str(row["reason"]),
                "updated_at": str(row["updated_at"]),
                **scheduling[str(row["work_order_id"])],
            }
            for row in rows
        )


__all__ = [
    "PortfolioEntry",
    "PortfolioIncrementalLease",
    "PortfolioLeaseStatus",
    "PortfolioJobSettlement",
    "PortfolioLifecycleState",
    "PortfolioPolicy",
    "PortfolioSchedulingEnvelope",
    "PortfolioSettlementStatus",
    "PortfolioStatus",
    "WORK_ORDER_AUTHORITY_SCHEMA_VERSION",
    "WorkOrderPortfolioStore",
    "work_order_from_payload",
]
