"""Pluggable sandbox execution and resource-control interfaces.

The memory path controls only sandbox pages. Model/KV-cache scheduling is
intentionally outside this interface and the first implementation milestone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from .types import (
    AgentTask,
    CpuClass,
    DemotionRequest,
    DemotionResult,
    HostStat,
    RestoreRequest,
    RestoreResult,
    SandboxId,
    SandboxStat,
    Stage,
    StageContext,
    Tier,
)

StageReport = Callable[[SandboxId, Stage, StageContext | None], None]


@runtime_checkable
class CpuPath(Protocol):
    def set_cpu(self, sandbox: SandboxId, cls: CpuClass) -> None: ...


@runtime_checkable
class MemoryPath(Protocol):
    """Compatibility memory path implemented by existing execution plugins."""

    def demote(self, sandbox: SandboxId, tier: Tier) -> int: ...

    def restore(self, sandbox: SandboxId) -> None: ...


@runtime_checkable
class SelectiveMemoryPath(MemoryPath, Protocol):
    """Optional selective extension used by predictive residency.

    A speculative ``RestoreRequest`` is preparation-only: implementations must
    not thaw runnable work or dispatch a sandbox command before commitment.
    """

    def demote_selective(
        self, sandbox: SandboxId, request: DemotionRequest
    ) -> DemotionResult: ...

    def restore_selective(
        self, sandbox: SandboxId, request: RestoreRequest
    ) -> RestoreResult: ...


@runtime_checkable
class Execution(CpuPath, MemoryPath, Protocol):
    """Lifecycle, observation, CPU, and sandbox-memory control surface."""

    def run(self, task: AgentTask, on_report: StageReport) -> SandboxId: ...

    def revoke(self, sandbox: SandboxId) -> None: ...

    def stat(self, sandbox: SandboxId) -> SandboxStat: ...

    def host_stat(self) -> HostStat: ...
