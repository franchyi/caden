"""Caden scheduler (L2) ingress: I1 (client -> Caden job queue) + the stage-report sink
(execution -> Caden). Policy (queue, admission, residency, CPU + memory) lives in the
implementation, which drives the execution layer via I2 (see execution.py). See the design doc.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from typing import Protocol, runtime_checkable

from .types import AgentTask, ReqId, SandboxId, Stage


class ReqState(enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class ReqStatus:
    state: ReqState
    result: str | None = None


@runtime_checkable
class Caden(Protocol):
    """L2 ingress. The thin client uses submit/poll/cancel (I1); the execution layer calls
    `on_report` to stream stage transitions."""

    # I1 — client -> Caden (job queue)
    def submit(self, task: AgentTask) -> ReqId: ...
    def poll(self, req: ReqId) -> ReqStatus: ...
    def cancel(self, req: ReqId) -> None: ...

    # execution -> Caden (stage-report sink)
    def on_report(self, sandbox: SandboxId, stage: Stage) -> None: ...
