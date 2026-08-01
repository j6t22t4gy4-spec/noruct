"""Loopback-only visual projection for Graph Blueprint and Job audit evidence.

This small local GUI is deliberately not another Graph control plane. It
consumes the same future-Job Blueprint and retained ACTIVE JOB projections
used by the Modern TUI. Its only writes are a next-Job constraint save through
the existing Graph control boundary and an exact pending-proposal decision
through the existing continuation boundary; Blueprint edits, Job starts and
every other authority transition remain outside the dashboard.
"""

from __future__ import annotations

import json
import hmac
import math
import re
import secrets
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import unquote, urlparse


SnapshotFactory = Callable[[], Mapping[str, object]]
JobSnapshotFactory = Callable[[str | None], Mapping[str, object]]
ProposalResolver = Callable[[str, str, bool], Mapping[str, object]]
FutureConstraintSaver = Callable[[Mapping[str, object]], Mapping[str, object]]
ReadyCallback = Callable[[str, int], None]


def _future_constraints_payload(payload: object) -> Mapping[str, object]:
    """Accept only a bounded next-Job constraint edit, never a live budget edit."""

    if not isinstance(payload, Mapping) or set(payload) != {
        "max_concurrency", "max_cost_usd", "max_wall_time_ms", "mutation_policy"
    }:
        raise ValueError("invalid future Graph constraints")

    def optional_int(value: object) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value < 1:
            raise ValueError("invalid future Graph integer constraint")
        return value

    raw_cost = payload["max_cost_usd"]
    if raw_cost is not None and (
        isinstance(raw_cost, bool)
        or not isinstance(raw_cost, (int, float))
        or not math.isfinite(float(raw_cost))
        or float(raw_cost) < 0
    ):
        raise ValueError("invalid future Graph cost constraint")
    policy = payload["mutation_policy"]
    if policy not in {"LOCKED", "PROPOSE", "BOUNDED_AUTO"}:
        raise ValueError("invalid future Graph mutation policy")
    return {
        "max_concurrency": optional_int(payload["max_concurrency"]),
        "max_cost_usd": None if raw_cost is None else float(raw_cost),
        "max_wall_time_ms": optional_int(payload["max_wall_time_ms"]),
        "mutation_policy": str(policy),
    }


def _page(
    *,
    session_token: str,
    proposal_actions_enabled: bool,
    future_constraint_actions_enabled: bool,
) -> bytes:
    """Return a self-contained, data-only local workbench shell."""

    action_config = json.dumps(
        {
            "session_token": session_token,
            "proposal_actions_enabled": proposal_actions_enabled,
            "future_constraint_actions_enabled": future_constraint_actions_enabled,
        },
        ensure_ascii=True,
    )
    return ("""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Noruct Graph Workbench</title>
<style>
:root{color-scheme:dark;font-family:ui-sans-serif,system-ui,sans-serif;background:#10141c;color:#e8edf6}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 78% 0,#1d3151 0,transparent 38%),#10141c}
main{max-width:1320px;margin:auto;padding:34px 24px 64px}header{display:flex;gap:18px;align-items:baseline;justify-content:space-between;border-bottom:1px solid #293544;padding-bottom:18px}
h1{font-size:26px;margin:0;letter-spacing:-.03em}h2{font-size:15px;text-transform:uppercase;letter-spacing:.09em;color:#9fbbdf;margin:0 0 12px}p,.muted{color:#aebdcd;line-height:1.5}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px;margin-top:20px}.panel{border:1px solid #293544;background:#141b25cc;border-radius:12px;padding:18px;min-width:0}.wide{grid-column:span 8}.side{grid-column:span 4}.full{grid-column:1/-1}
button{font:inherit;background:#1f3655;border:1px solid #42678f;color:#e8edf6;border-radius:7px;padding:8px 10px;cursor:pointer;text-align:left;width:100%;margin:4px 0}button:hover{background:#294b76}input,select{width:100%;font:inherit;color:#e8edf6;background:#0d1219;border:1px solid #3a5676;border-radius:6px;padding:7px}label{display:grid;gap:5px;color:#aebdcd;font-size:12px}.constraint-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:12px}.constraint-grid button{grid-column:1/-1;text-align:center}code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}pre{white-space:pre-wrap;overflow:auto;max-height:360px;background:#0d1219;border:1px solid #293544;padding:12px;border-radius:8px}.rows{display:grid;gap:8px}.row{padding:10px;border-left:3px solid #477eae;background:#111822}.key{color:#8daed0}.empty{padding:20px;color:#9aaabd;border:1px dashed #3a4b5f;border-radius:8px}.tag{display:inline-block;border:1px solid #3a5676;border-radius:999px;padding:2px 8px;margin:2px;color:#b8d4f1}.notice{font-size:13px;border-left:3px solid #cf9a45;padding-left:10px;color:#e2c185}@media(max-width:850px){.wide,.side{grid-column:1/-1}.constraint-grid{grid-template-columns:1fr}main{padding:24px 15px}}
</style><main><header><div><h1>Graph Workbench</h1><p>Local visual projection. Blueprint changes and Job decisions remain explicit typed actions.</p></div><span class=mono id=updated>Loading</span></header>
<section class=grid><div class="panel wide"><h2>Future Job selection &amp; budget envelope</h2><div id=selection class=rows></div></div><div class="panel side"><h2>Retained Jobs</h2><div id=jobs class=rows></div></div><div class="panel full"><h2>Company operator state</h2><div id=operator class=rows></div></div><div class="panel full"><h2>Blueprint revisions &amp; structural diff</h2><div id=blueprints class=rows></div></div><div class="panel full"><h2>Selected Job audit</h2><div id=audit class=empty>Select a retained Job to inspect content-free lineage, leases, and proposal decisions.</div></div></section>
<p class=notice id=boundary>This page can save only next-Job budget constraints and decide a visible pending Graph proposal. It cannot edit a Blueprint, start a Job, or approve tools and external effects.</p></main>
<script>
const config=__ACTION_CONFIG__;
const by=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])), pretty=v=>JSON.stringify(v,null,2);
function row(label,value){return `<div class=row><span class=key>${esc(label)}</span><div class=mono>${esc(value)}</div></div>`}
function tags(values){return (values||[]).map(v=>String(v)).join(', ')||'none'}
function constraintEditor(s){if(!config.future_constraint_actions_enabled)return '';const value=v=>v??'';return `<form id=future-constraints class=constraint-grid><p class=notice style="grid-column:1/-1">Explicitly save limits for future Work Orders only. This does not alter an active Job or release/reserve any budget.</p><label>Max concurrency<input name=max_concurrency type=number min=1 step=1 value="${esc(value(s.max_concurrency))}" placeholder="Company default"></label><label>Cost ceiling (USD)<input name=max_cost_usd type=number min=0 step=any value="${esc(value(s.max_cost_usd))}" placeholder="Company hard limit"></label><label>Time ceiling (ms)<input name=max_wall_time_ms type=number min=1 step=1 value="${esc(value(s.max_wall_time_ms))}" placeholder="Company hard limit"></label><label>Mutation policy<select name=mutation_policy>${['LOCKED','PROPOSE','BOUNDED_AUTO'].map(v=>`<option value="${v}" ${s.mutation_policy===v?'selected':''}>${v}</option>`).join('')}</select></label><button type=submit>Save future-Job constraints</button></form>`}
function renderGraph(data){const s=data.selection||{};by('selection').innerHTML=[row('Blueprint',s.blueprint_id?`${s.blueprint_id}@${s.version}`:'No selected Blueprint'),row('Mutation policy',s.mutation_policy),row('Concurrency cap',s.max_concurrency??'Company default'),row('Cost ceiling',s.max_cost_usd??'Company hard limit'),row('Time ceiling',s.max_wall_time_ms??'Company hard limit'),row('Pinned employees',tags(s.pinned_employee_ids)),row('Excluded employees',tags(s.excluded_employee_ids)),row('Independent review',s.require_independent_review?'required':'not required')].join('')+constraintEditor(s);const form=by('future-constraints');if(form)form.onsubmit=e=>{e.preventDefault();saveFutureConstraints(new FormData(form))};const list=data.blueprints||[];by('blueprints').innerHTML=list.length?list.map(b=>{const d=b.revision_diff||{};const changed=(d.changed_tasks||[]).map(x=>`${x.task_id}: ${(x.fields||[]).join(', ')}`).join(' | ')||'none';const topology=(b.editor_tasks||[]).map(t=>`${t.task_id}${(t.depends_on||[]).length?' <- '+t.depends_on.join(', '):''}`).join(' -> ')||'empty';const receipt=(b.revision_receipts||[]).at(-1)||{};const rationale=receipt.rationale?`<div class=mono>user rationale: ${esc(receipt.rationale)}</div>`:'';return `<div class=row><strong>${esc(b.blueprint_id)}@${esc(b.version)}</strong> <span class=tag>${esc(b.origin)}</span><div class=muted>profiles ${esc(tags(b.execution_profiles))} / tasks ${esc(b.task_count)} / replicas ${esc(b.execution_replica_count)}</div><div class=mono>topology: ${esc(topology)}</div><div class=mono>diff +[${esc((d.added_task_ids||[]).join(', '))}] -[${esc((d.removed_task_ids||[]).join(', '))}] changed ${esc(changed)}</div>${rationale}</div>`}).join(''):'<div class=empty>No local Blueprint revision exists yet.</div>'}
function renderJobs(data){const jobs=data.jobs||[];by('jobs').innerHTML=jobs.length?jobs.map(j=>`<button data-job="${esc(j.job_id)}"><strong>${esc(j.job_id)}</strong><br><span class=muted>${esc(j.audit_status)} / ${esc(j.job_status)} / graph v${esc(j.final_graph_version)}</span></button>`).join(''):'<div class=empty>No retained Job.</div>';document.querySelectorAll('[data-job]').forEach(b=>b.onclick=()=>loadAudit(b.dataset.job))}
function renderOperator(data){const manager=data&&data.manager||{},execution=data&&data.execution||{},hold=data&&data.hold||{},approval=data&&data.approval||{},budget=data&&data.budget||{},attention=data&&data.attention||{};by('operator').innerHTML=[row('Manager',manager.status||'not configured'),row('Graph',execution.decision||'no active job'),row('Hold',hold.reason||'none'),row('Approval',approval.status||'none pending'),row('Budget',budget.summary||'not available'),row('Attention',attention.summary||'not scanned'),row('Next action',data&&data.next_action||'Inspect Company state before taking action.')].join('')}
function renderAudit(data){const audit=by('audit');if(!data.job){audit.className='empty';audit.textContent=data.error||'No retained ACTIVE JOB.';return}audit.className='';const g=data.graph||{};const summary=g.change_summary||{};const budget=data.frozen_budget_envelope||{};const observed=data.observed_execution||{};const revisions=g.revisions||[];const lineage=revisions.length?revisions.map(r=>`<div class=row><strong>r${esc(r.sequence)} ${esc(r.operation)}</strong><div class=mono>${esc(String(r.previous_digest||'').slice(0,16))} → ${esc(String(r.next_digest||'').slice(0,16))}</div><div class=muted>expected: ${esc(r.expected_impact)} · validated: ${esc(r.validation_receipt)} · terminal association: ${esc(r.observed_terminal_outcome)} · reserved Δ$${esc(r.budget_delta)}</div></div>`).join(''):'<div class=empty>No accepted topology revision; the initial Graph remained authoritative.</div>';const pending=(g.proposals||[]).filter(p=>p.status==='PENDING'&&p.proposal_id);const actions=config.proposal_actions_enabled&&pending.length?`<h2>Explicit proposal decision</h2><p class=notice>Approve applies the exact validated patch. Reject resumes the exact prior Graph. Either choice is one-shot and revalidates the retained Work Order, digest, lease, and policy. Tool or external-action approvals remain separate.</p>${pending.map(p=>`<div class=row><div class=mono>${esc(p.proposal_id)} / ${esc(p.operation)} / proposed lease ${esc(pretty(p.proposed_lease||{}))}</div><button data-proposal="${esc(p.proposal_id)}" data-decision="approve">Approve exact proposal</button><button data-proposal="${esc(p.proposal_id)}" data-decision="reject">Reject and resume prior Graph</button></div>`).join('')}`:'';audit.innerHTML=`<div class=rows>${row('Job',data.job.job_id)}${row('State',data.job.audit_status+' / '+data.job.job_status)}${row('Blueprint',g.blueprint)}${row('Graph revisions',summary.accepted_revision_count??0)}${row('Reserved cost delta',summary.total_reserved_cost_delta??0)}${row('Proposal decisions',(g.proposals||[]).map(p=>p.proposal_id+' '+p.status).join(' | ')||'none')}</div><h2 style="margin-top:18px">Frozen budget envelope</h2><pre>${esc(pretty(budget))}</pre>${actions}<h2 style="margin-top:18px">Accepted Graph revisions</h2>${lineage}<h2 style="margin-top:18px">Structural summary</h2><pre>${esc(pretty(summary))}</pre><h2>Observed execution (not causal impact)</h2><pre>${esc(pretty(observed))}</pre><h2>Checkpoints</h2><pre>${esc(pretty(data.checkpoints||[]))}</pre>`;document.querySelectorAll('[data-proposal]').forEach(b=>b.onclick=()=>resolveProposal(data.job.job_id,b.dataset.proposal,b.dataset.decision))}
async function loadAudit(id){const r=await fetch('/api/jobs/'+encodeURIComponent(id),{cache:'no-store'});renderAudit(await r.json())}
async function resolveProposal(jobId,proposalId,decision){if(!config.proposal_actions_enabled)return;const r=await fetch('/api/proposals/resolve',{method:'POST',headers:{'Content-Type':'application/json','X-Noruct-Local-Token':config.session_token},body:JSON.stringify({job_id:jobId,proposal_id:proposalId,decision})});if(!r.ok){by('boundary').textContent='Proposal decision was not applied. Inspect the terminal for the bounded failure path.';return}by('boundary').textContent='Proposal decision accepted by the shared continuation path. Refreshing retained audit…';await loadAudit(jobId)}
async function saveFutureConstraints(form){const integer=k=>{const raw=String(form.get(k)||'').trim();return raw===''?null:Number(raw)};const costRaw=String(form.get('max_cost_usd')||'').trim();const payload={max_concurrency:integer('max_concurrency'),max_cost_usd:costRaw===''?null:Number(costRaw),max_wall_time_ms:integer('max_wall_time_ms'),mutation_policy:String(form.get('mutation_policy')||'')};const r=await fetch('/api/future-constraints',{method:'POST',headers:{'Content-Type':'application/json','X-Noruct-Local-Token':config.session_token},body:JSON.stringify(payload)});if(!r.ok){by('boundary').textContent='Future-Job constraints were not saved. No active Job or budget lease changed.';return}by('boundary').textContent='Future-Job constraints saved through the shared Graph control service. Refreshing…';const graph=await (await fetch('/api/graph',{cache:'no-store'})).json();renderGraph(graph)}
async function load(){try{const [graph,jobs,operator]=await Promise.all(['/api/graph','/api/jobs','/api/operator'].map(async u=>{const r=await fetch(u,{cache:'no-store'});if(!r.ok)throw new Error('refresh failed');return r.json()}));renderGraph(graph);renderJobs(jobs);renderOperator(operator);by('updated').textContent='Updated '+new Date().toLocaleTimeString()}catch(e){by('updated').textContent='Refresh failed'}}load();setInterval(load,5000)
</script></html>""".replace("__ACTION_CONFIG__", action_config)).encode("utf-8")


def serve_graph_workbench_dashboard(
    *,
    graph_snapshot: SnapshotFactory,
    job_catalog: SnapshotFactory,
    job_snapshot: JobSnapshotFactory,
    operator_snapshot: SnapshotFactory | None = None,
    resolve_proposal: ProposalResolver | None = None,
    save_future_constraints: FutureConstraintSaver | None = None,
    port: int = 0,
    maximum_requests: int | None = None,
    session_token: str | None = None,
    on_ready: ReadyCallback | None = None,
) -> tuple[str, int]:
    """Serve a loopback Graph workbench with two narrow typed actions.

    ``maximum_requests`` exists only for deterministic local diagnostics and
    tests. The dashboard cannot execute Jobs, edit Blueprints, or alter active
    leases; it can only save next-Job constraints through the injected Graph
    control boundary and resolve an exact visible pending proposal.
    """

    if not 0 <= port <= 65535:
        raise ValueError("Graph workbench port must be between 0 and 65535")
    if maximum_requests is not None and not 1 <= maximum_requests <= 100_000:
        raise ValueError("Graph workbench maximum requests must be between 1 and 100000")
    token = session_token or secrets.token_urlsafe(32)
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", token):
        raise ValueError("Graph workbench session token is invalid")
    served = 0

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, content_type: str, status: HTTPStatus) -> None:
            nonlocal served
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)
            served += 1

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self._send(
                    _page(
                        session_token=token,
                        proposal_actions_enabled=resolve_proposal is not None,
                        future_constraint_actions_enabled=save_future_constraints is not None,
                    ),
                    "text/html; charset=utf-8",
                    HTTPStatus.OK,
                )
                return
            if path == "/api/graph":
                payload: Mapping[str, object] = graph_snapshot()
            elif path == "/api/jobs":
                payload = job_catalog()
            elif path == "/api/operator":
                payload = (
                    operator_snapshot()
                    if operator_snapshot is not None
                    else {
                        "schema": "noruct.operator-surface.v1",
                        "attention": {"summary": "not configured"},
                    }
                )
            elif path.startswith("/api/jobs/"):
                payload = job_snapshot(unquote(path.removeprefix("/api/jobs/")))
            else:
                self._send(b'{"error":"not_found"}', "application/json; charset=utf-8", HTTPStatus.NOT_FOUND)
                return
            self._send(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.OK,
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in {"/api/proposals/resolve", "/api/future-constraints"}:
                self._send(b'{"error":"read_only"}', "application/json; charset=utf-8", HTTPStatus.METHOD_NOT_ALLOWED)
                return
            supplied = self.headers.get("X-Noruct-Local-Token", "")
            if not hmac.compare_digest(supplied, token):
                self._send(b'{"error":"forbidden"}', "application/json; charset=utf-8", HTTPStatus.FORBIDDEN)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if not 2 <= content_length <= 4096:
                self._send(b'{"error":"invalid_request"}', "application/json; charset=utf-8", HTTPStatus.BAD_REQUEST)
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if not isinstance(payload, Mapping):
                self._send(b'{"error":"invalid_request"}', "application/json; charset=utf-8", HTTPStatus.BAD_REQUEST)
                return
            if self.path == "/api/future-constraints":
                if save_future_constraints is None:
                    self._send(b'{"error":"read_only"}', "application/json; charset=utf-8", HTTPStatus.METHOD_NOT_ALLOWED)
                    return
                try:
                    selection = save_future_constraints(_future_constraints_payload(payload))
                except (TypeError, ValueError):
                    self._send(b'{"error":"invalid_constraints"}', "application/json; charset=utf-8", HTTPStatus.BAD_REQUEST)
                    return
                except Exception:
                    self._send(b'{"error":"constraint_save_failed"}', "application/json; charset=utf-8", HTTPStatus.CONFLICT)
                    return
                self._send(
                    json.dumps({"selection": selection}, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                    "application/json; charset=utf-8",
                    HTTPStatus.OK,
                )
                return
            if resolve_proposal is None:
                self._send(b'{"error":"read_only"}', "application/json; charset=utf-8", HTTPStatus.METHOD_NOT_ALLOWED)
                return
            job_id = str(payload.get("job_id", "")).strip()
            proposal_id = str(payload.get("proposal_id", "")).strip()
            decision = str(payload.get("decision", "")).strip()
            if not job_id or not proposal_id or decision not in {"approve", "reject"}:
                self._send(b'{"error":"invalid_request"}', "application/json; charset=utf-8", HTTPStatus.BAD_REQUEST)
                return
            snapshot = job_snapshot(job_id)
            job = snapshot.get("job") if isinstance(snapshot, Mapping) else None
            graph = snapshot.get("graph") if isinstance(snapshot, Mapping) else None
            proposals = graph.get("proposals", ()) if isinstance(graph, Mapping) else ()
            pending = any(
                isinstance(item, Mapping)
                and item.get("proposal_id") == proposal_id
                and item.get("status") == "PENDING"
                for item in proposals
            )
            if not isinstance(job, Mapping) or job.get("job_id") != job_id or not pending:
                self._send(b'{"error":"proposal_not_pending"}', "application/json; charset=utf-8", HTTPStatus.CONFLICT)
                return
            try:
                result = resolve_proposal(job_id, proposal_id, decision == "approve")
            except Exception:
                self._send(b'{"error":"proposal_resolution_failed"}', "application/json; charset=utf-8", HTTPStatus.CONFLICT)
                return
            response = {
                "job_id": job_id,
                "proposal_id": proposal_id,
                "decision": decision,
                "job_status": str(result.get("job_status", "unknown"))[:64],
            }
            self._send(
                json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.OK,
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", port), Handler)
    host, bound_port = server.server_address[:2]
    try:
        if on_ready is not None:
            on_ready(str(host), int(bound_port))
        while maximum_requests is None or served < maximum_requests:
            server.handle_request()
    finally:
        server.server_close()
    return str(host), int(bound_port)
