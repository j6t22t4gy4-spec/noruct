"""Read-only verification graph projection from explicit retained receipts.

This module deliberately treats receipt identifiers, source identifiers, and
digests as opaque values.  It does not read runtime state or make any
authority decision.  A graph is rejected when an explicit relationship is
ambiguous, dangling, or cyclic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


VERIFICATION_GRAPH_SCHEMA = "noruct.verification-graph.v1"
NODE_KINDS = frozenset(
    {"GENERATOR", "VERIFIER", "EVIDENCE_SOURCE", "VALIDATOR", "FINAL_WRITER"}
)
EDGE_KINDS = frozenset(
    {"VERIFIES", "USES_EVIDENCE", "VALIDATES", "CONTRIBUTES_TO"}
)
INDEPENDENCE_STATUSES = frozenset({"INDEPENDENT", "NOT_INDEPENDENT", "UNKNOWN"})


class VerificationGraphError(ValueError):
    """The supplied explicit receipts cannot form a safe graph projection."""


@dataclass(frozen=True, slots=True)
class VerificationNode:
    """One receipt-backed graph node and its opaque source binding."""

    id: str
    kind: str
    receipt_id: str
    source_id: str
    source_digest: str
    drill_down_id: str


@dataclass(frozen=True, slots=True)
class VerificationEdge:
    """One explicit receipt relationship between two graph nodes."""

    from_node: str
    to_node: str
    kind: str
    receipt_id: str
    source_id: str
    source_digest: str
    drill_down_id: str


@dataclass(frozen=True, slots=True)
class VerificationIndependence:
    """A conservative, receipt-derived independence classification."""

    subject_id: str
    reviewed_id: str
    status: str
    evidence_source_id: str
    evidence_source_digest: str
    drill_down_id: str

    def __post_init__(self) -> None:
        if self.status not in INDEPENDENCE_STATUSES:
            raise VerificationGraphError(
                f"unsupported independence status: {self.status!r}"
            )


@dataclass(frozen=True, slots=True)
class VerificationGraph:
    """Immutable read-only verification graph projection."""

    schema: str
    nodes: tuple[VerificationNode, ...]
    edges: tuple[VerificationEdge, ...]
    independence: tuple[VerificationIndependence, ...]

    def __post_init__(self) -> None:
        if self.schema != VERIFICATION_GRAPH_SCHEMA:
            raise VerificationGraphError(f"unsupported graph schema: {self.schema!r}")

    def node(self, node_id: str) -> VerificationNode | None:
        """Return a node by id without exposing mutable graph state."""

        return next((node for node in self.nodes if node.id == node_id), None)

    def drill_down(self, drill_down_id: str) -> tuple[VerificationNode | VerificationEdge, ...]:
        """Return receipt-backed items carrying the supplied opaque identifier."""

        return tuple(
            item
            for item in (*self.nodes, *self.edges)
            if item.drill_down_id == drill_down_id
        )


_KIND_PREFIX = {
    "GENERATOR": "generator",
    "VERIFIER": "verifier",
    "EVIDENCE_SOURCE": "evidence-source",
    "VALIDATOR": "validator",
    "FINAL_WRITER": "final-writer",
}


def _opaque(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationGraphError(f"missing opaque {field}")
    return value


def _items(receipts: object, field: str) -> tuple[Mapping[str, object], ...]:
    if receipts is None:
        return ()
    if isinstance(receipts, (str, bytes, Mapping)) or not isinstance(receipts, Iterable):
        raise VerificationGraphError(f"{field} must be an iterable of receipts")
    result: list[Mapping[str, object]] = []
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise VerificationGraphError(f"{field} contains a non-mapping receipt")
        result.append(receipt)
    return tuple(result)


def _receipt_id(receipt: Mapping[str, object]) -> str:
    value = receipt.get("id", receipt.get("receipt_id"))
    return _opaque(value, "receipt id")


def _first(receipt: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in receipt:
            return receipt[key]
    return None


def _binding(
    receipt: Mapping[str, object],
    *,
    evidence_binding: bool = False,
) -> tuple[str, str]:
    if evidence_binding:
        source = _first(receipt, "evidence_source_id", "evidence_source")
        digest = _first(receipt, "evidence_source_digest", "evidence_digest")
    else:
        source = _first(receipt, "source_id", "source")
        digest = _first(receipt, "source_digest", "digest")
    return _opaque(source, "source id"), _opaque(digest, "source digest")


def _node_id(kind: str, receipt_id: str) -> str:
    return f"{_KIND_PREFIX[kind]}:{receipt_id}"


def _relation(receipt: Mapping[str, object], *keys: str) -> str | None:
    value = _first(receipt, *keys)
    if value is None:
        return None
    return _opaque(value, "relationship id")


def _deterministic(receipt: Mapping[str, object]) -> bool:
    value = _first(receipt, "deterministic", "validator_kind")
    if value is True:
        return True
    return isinstance(value, str) and value.upper() == "DETERMINISTIC"


def _actor(receipt: Mapping[str, object]) -> str | None:
    value = _first(receipt, "actor_id", "actor")
    return value if isinstance(value, str) and value else None


def _profile(receipt: Mapping[str, object]) -> str | None:
    value = _first(receipt, "profile_digest", "profile_id", "profile")
    return value if isinstance(value, str) and value else None


def _make_node(
    kind: str,
    receipt: Mapping[str, object],
    *,
    evidence_binding: bool = False,
) -> VerificationNode:
    receipt_id = _receipt_id(receipt)
    source_id, source_digest = _binding(receipt, evidence_binding=evidence_binding)
    drill_down_id = _opaque(
        _first(receipt, "drill_down_id", "evidence_id", "receipt_id", "id"),
        "drill-down id",
    )
    return VerificationNode(
        id=_node_id(kind, receipt_id),
        kind=kind,
        receipt_id=receipt_id,
        source_id=source_id,
        source_digest=source_digest,
        drill_down_id=drill_down_id,
    )


def _edge(
    from_node: VerificationNode,
    to_node: VerificationNode,
    kind: str,
) -> VerificationEdge:
    return VerificationEdge(
        from_node=from_node.id,
        to_node=to_node.id,
        kind=kind,
        receipt_id=from_node.receipt_id,
        source_id=from_node.source_id,
        source_digest=from_node.source_digest,
        drill_down_id=from_node.drill_down_id,
    )


def _resolve(
    relation_id: str,
    expected_kind: str,
    by_kind: Mapping[str, Mapping[str, VerificationNode]],
) -> VerificationNode:
    node = by_kind[expected_kind].get(relation_id)
    if node is None:
        raise VerificationGraphError(
            f"dangling {expected_kind.lower()} binding: {relation_id!r}"
        )
    return node


def _has_cycle(nodes: Iterable[VerificationNode], edges: Iterable[VerificationEdge]) -> bool:
    adjacency: dict[str, list[str]] = {node.id: [] for node in nodes}
    for edge in edges:
        adjacency[edge.from_node].append(edge.to_node)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        if any(visit(child) for child in adjacency[node_id]):
            return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in adjacency)


def _independence(
    verifier_receipt: Mapping[str, object],
    verifier: VerificationNode,
    generator_receipt: Mapping[str, object],
    generator: VerificationNode,
) -> str:
    verifier_actor = _actor(verifier_receipt)
    generator_actor = _actor(generator_receipt)
    if verifier_actor is not None and verifier_actor == generator_actor:
        return "NOT_INDEPENDENT"

    verifier_profile = _profile(verifier_receipt)
    generator_profile = _profile(generator_receipt)
    if (
        verifier_profile is not None
        and generator_profile is not None
        and verifier_profile != generator_profile
        and verifier.source_id == generator.source_id
        and verifier.source_digest == generator.source_digest
    ):
        # A changed profile label does not establish a separate verification
        # route when the bound source is unchanged.
        return "NOT_INDEPENDENT"
    return "UNKNOWN"


def project_verification_graph(
    *,
    generator_receipts: object = (),
    verifier_receipts: object = (),
    evidence_source_receipts: object = (),
    validator_receipts: object = (),
    final_writer_receipts: object = (),
) -> VerificationGraph:
    """Build an immutable graph from explicit receipt collections only."""

    receipt_sets = {
        "GENERATOR": _items(generator_receipts, "generator_receipts"),
        "VERIFIER": _items(verifier_receipts, "verifier_receipts"),
        "EVIDENCE_SOURCE": _items(
            evidence_source_receipts, "evidence_source_receipts"
        ),
        "VALIDATOR": _items(validator_receipts, "validator_receipts"),
        "FINAL_WRITER": _items(final_writer_receipts, "final_writer_receipts"),
    }
    nodes_by_kind: dict[str, dict[str, VerificationNode]] = {}
    receipts_by_kind: dict[str, dict[str, Mapping[str, object]]] = {}
    all_nodes: list[VerificationNode] = []
    for kind in (
        "GENERATOR",
        "VERIFIER",
        "EVIDENCE_SOURCE",
        "VALIDATOR",
        "FINAL_WRITER",
    ):
        nodes_by_kind[kind] = {}
        receipts_by_kind[kind] = {}
        for receipt in receipt_sets[kind]:
            receipt_id = _receipt_id(receipt)
            if receipt_id in nodes_by_kind[kind]:
                raise VerificationGraphError(
                    f"duplicate {kind.lower()} receipt id: {receipt_id!r}"
                )
            if kind in {"VERIFIER", "VALIDATOR"}:
                evidence_binding = (
                    "evidence_source_id" in receipt
                    or "evidence_source" in receipt
                )
                node = _make_node(
                    kind, receipt, evidence_binding=evidence_binding
                )
            else:
                node = _make_node(kind, receipt)
            nodes_by_kind[kind][receipt_id] = node
            receipts_by_kind[kind][receipt_id] = receipt
            all_nodes.append(node)

    edges: list[VerificationEdge] = []
    independence: list[VerificationIndependence] = []

    for receipt_id, receipt in receipts_by_kind["VERIFIER"].items():
        verifier = nodes_by_kind["VERIFIER"][receipt_id]
        generator_id = _relation(receipt, "generator_id", "generator_receipt_id")
        if generator_id is not None:
            generator = _resolve(generator_id, "GENERATOR", nodes_by_kind)
            edges.append(_edge(verifier, generator, "VERIFIES"))
            status = _independence(receipt, verifier, receipts_by_kind["GENERATOR"][generator_id], generator)
            independence.append(
                VerificationIndependence(
                    subject_id=verifier.id,
                    reviewed_id=generator.id,
                    status=status,
                    evidence_source_id=verifier.source_id,
                    evidence_source_digest=verifier.source_digest,
                    drill_down_id=verifier.drill_down_id,
                )
            )
        evidence_id = _relation(
            receipt, "evidence_source_id", "evidence_source_receipt_id"
        )
        if evidence_id is not None:
            evidence = _resolve(evidence_id, "EVIDENCE_SOURCE", nodes_by_kind)
            if verifier.source_id != evidence.source_id or verifier.source_digest != evidence.source_digest:
                raise VerificationGraphError("verifier evidence binding does not match source receipt")
            edges.append(_edge(verifier, evidence, "USES_EVIDENCE"))

    # A reciprocal explicit verifier binding is retained as an edge so the
    # common malformed-cycle shape is rejected instead of being ignored.
    for receipt_id, receipt in receipts_by_kind["GENERATOR"].items():
        verifier_id = _relation(receipt, "verifier_id", "verifier_receipt_id")
        if verifier_id is not None:
            verifier = _resolve(verifier_id, "VERIFIER", nodes_by_kind)
            edges.append(_edge(nodes_by_kind["GENERATOR"][receipt_id], verifier, "CONTRIBUTES_TO"))

    for receipt_id, receipt in receipts_by_kind["VALIDATOR"].items():
        validator = nodes_by_kind["VALIDATOR"][receipt_id]
        verifier_id = _relation(receipt, "verifier_id", "verifier_receipt_id")
        if verifier_id is not None:
            verifier = _resolve(verifier_id, "VERIFIER", nodes_by_kind)
            edges.append(_edge(validator, verifier, "VALIDATES"))
        evidence_id = _relation(
            receipt, "evidence_source_id", "evidence_source_receipt_id"
        )
        if evidence_id is None:
            raise VerificationGraphError("validator lacks an exact evidence binding")
        evidence = _resolve(evidence_id, "EVIDENCE_SOURCE", nodes_by_kind)
        if validator.source_id != evidence.source_id or validator.source_digest != evidence.source_digest:
            raise VerificationGraphError("validator evidence binding does not match source receipt")
        edges.append(_edge(validator, evidence, "USES_EVIDENCE"))
        if _deterministic(receipt) and (
            verifier_id is None
            or evidence.source_id != _resolve(
                _relation(receipts_by_kind["VERIFIER"][verifier_id], "generator_id", "generator_receipt_id"),
                "GENERATOR",
                nodes_by_kind,
            ).source_id
        ):
            reviewed_id = verifier_id or evidence_id
            independence.append(
                VerificationIndependence(
                    subject_id=validator.id,
                    reviewed_id=_node_id("VERIFIER", reviewed_id) if verifier_id else evidence.id,
                    status="INDEPENDENT",
                    evidence_source_id=evidence.source_id,
                    evidence_source_digest=evidence.source_digest,
                    drill_down_id=validator.drill_down_id,
                )
            )
        else:
            independence.append(
                VerificationIndependence(
                    subject_id=validator.id,
                    reviewed_id=_node_id("VERIFIER", verifier_id) if verifier_id else evidence.id,
                    status="UNKNOWN",
                    evidence_source_id=evidence.source_id,
                    evidence_source_digest=evidence.source_digest,
                    drill_down_id=validator.drill_down_id,
                )
            )

    for receipt_id, receipt in receipts_by_kind["FINAL_WRITER"].items():
        writer = nodes_by_kind["FINAL_WRITER"][receipt_id]
        for key, kind in (
            ("generator_id", "GENERATOR"),
            ("verifier_id", "VERIFIER"),
            ("validator_id", "VALIDATOR"),
        ):
            related_id = _relation(receipt, key, f"{key[:-3]}receipt_id")
            if related_id is not None:
                related = _resolve(related_id, kind, nodes_by_kind)
                edges.append(_edge(related, writer, "CONTRIBUTES_TO"))

    if _has_cycle(all_nodes, edges):
        raise VerificationGraphError("verification graph contains a cycle")
    return VerificationGraph(
        schema=VERIFICATION_GRAPH_SCHEMA,
        nodes=tuple(all_nodes),
        edges=tuple(edges),
        independence=tuple(independence),
    )


def verification_graph(**receipts: object) -> VerificationGraph:
    """Short alias for the named receipt projection."""

    return project_verification_graph(**receipts)
