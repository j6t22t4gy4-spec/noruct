"""Immutable heterogeneous route plan; it cannot mutate the Kernel graph."""
from __future__ import annotations
import re
from dataclasses import dataclass

from dynamic_firm.kernel.models import TaskAssignmentEvent

_TOKEN=re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z"); _DIGEST=re.compile(r"[0-9a-f]{64}\Z")
def _token(value, field):
 if not isinstance(value,str) or not _TOKEN.fullmatch(value): raise ValueError(f"{field} is invalid")
 return value
def _digest(value, field):
 if not isinstance(value,str) or not _DIGEST.fullmatch(value): raise ValueError(f"{field} must be sha256")
 return value
@dataclass(frozen=True,slots=True)
class TaskRouteAssignment:
 task_id:str; employee_id:str; route_binding_digest:str; depends_on:tuple[str,...]=(); final:bool=False; expected_selection_receipt_digest:str|None=None
 def __post_init__(self):
  for field in ("task_id","employee_id"): object.__setattr__(self,field,_token(getattr(self,field),field))
  object.__setattr__(self,"route_binding_digest",_digest(self.route_binding_digest,"route_binding_digest"))
  if self.expected_selection_receipt_digest is not None:
   object.__setattr__(self,"expected_selection_receipt_digest",_digest(self.expected_selection_receipt_digest,"expected_selection_receipt_digest"))
  dependencies=tuple(_token(value,"dependency_task_id") for value in self.depends_on)
  if self.task_id in dependencies or len(dependencies)!=len(set(dependencies)): raise ValueError("task dependencies are invalid")
  object.__setattr__(self,"depends_on",dependencies)
@dataclass(frozen=True,slots=True)
class DependencyArtifactHandoff:
 source_task_id:str; target_task_id:str; artifact_digest:str
 def __post_init__(self):
  object.__setattr__(self,"source_task_id",_token(self.source_task_id,"source_task_id")); object.__setattr__(self,"target_task_id",_token(self.target_task_id,"target_task_id")); object.__setattr__(self,"artifact_digest",_digest(self.artifact_digest,"artifact_digest"))
  if self.source_task_id==self.target_task_id: raise ValueError("dependency handoff cannot target itself")
@dataclass(frozen=True,slots=True)
class MultiRouteJobPlan:
 graph_digest:str; assignments:tuple[TaskRouteAssignment,...]; handoffs:tuple[DependencyArtifactHandoff,...]; acting_integrator_id:str
 def __post_init__(self):
  object.__setattr__(self,"graph_digest",_digest(self.graph_digest,"graph_digest")); object.__setattr__(self,"acting_integrator_id",_token(self.acting_integrator_id,"acting_integrator_id"))
  if not self.assignments or any(not isinstance(item,TaskRouteAssignment) for item in self.assignments): raise ValueError("assignments must be nonempty and typed")
  by_id={item.task_id:item for item in self.assignments}
  if len(by_id)!=len(self.assignments) or sum(item.final for item in self.assignments)!=1: raise ValueError("plan requires unique tasks and exactly one final owner")
  final=next(item for item in self.assignments if item.final)
  if final.employee_id!=self.acting_integrator_id: raise ValueError("final owner must be the acting integrator")
  if any(dependency not in by_id for item in self.assignments for dependency in item.depends_on): raise ValueError("assignment depends on an unavailable task")
  pairs={(item.source_task_id,item.target_task_id) for item in self.handoffs}
  if len(pairs)!=len(self.handoffs) or any(not isinstance(item,DependencyArtifactHandoff) for item in self.handoffs): raise ValueError("handoffs must be unique and typed")
  expected_pairs={(source, assignment.task_id) for assignment in self.assignments for source in assignment.depends_on}
  if pairs != expected_pairs: raise ValueError("handoffs must exactly cover declared dependencies")


class MultiRouteAssignmentGuard:
 """Read-only Kernel assignment sink that returns the pre-frozen route digest."""
 def __init__(self, plan: MultiRouteJobPlan, *, graph_version: int|None=None, task_attempts: tuple[tuple[str,int],...]=()):
  if not isinstance(plan,MultiRouteJobPlan): raise TypeError("multi-route plan is required")
  if graph_version is not None and (type(graph_version) is not int or graph_version < 1): raise ValueError("approved graph version must be positive")
  if not isinstance(task_attempts,tuple) or any(not isinstance(item,tuple) or len(item)!=2 or not isinstance(item[0],str) or type(item[1]) is not int or item[1] < 1 for item in task_attempts): raise ValueError("approved task attempts are invalid")
  attempts=dict(task_attempts)
  if len(attempts)!=len(task_attempts): raise ValueError("approved task attempts must be unique")
  if graph_version is None and task_attempts: raise ValueError("approved task attempts require a graph version")
  if graph_version is not None and set(attempts)!={item.task_id for item in plan.assignments}: raise ValueError("approved graph state must cover the frozen plan")
  self._plan=plan; self._assignments={item.task_id:item for item in plan.assignments}; self._seen=set()
  self._graph_version=graph_version; self._task_attempts=attempts
 def accept(self,event: TaskAssignmentEvent)->str:
  if not isinstance(event,TaskAssignmentEvent): raise TypeError("Kernel assignment event is required")
  assignment=self._assignments.get(event.task_id)
  if (assignment is None or assignment.employee_id!=event.employee_id or assignment.final is not event.final_task or assignment.depends_on!=event.depends_on or (self._graph_version is not None and event.graph_version!=self._graph_version) or (self._graph_version is not None and event.attempt!=self._task_attempts.get(event.task_id))): raise ValueError("Kernel assignment does not match frozen multi-route plan")
  if event.task_id in self._seen: raise ValueError("multi-route task was assigned more than once")
  self._seen.add(event.task_id)
  return assignment.route_binding_digest
