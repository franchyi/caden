"""The `Sandbox` abstraction — one isolated agent unit, backend-agnostic.

The two execution paths (CPU, memory) act on a Sandbox; the backend is swappable
(bubblewrap + cgroup v2 now; container / microVM / gVisor later). See the design doc.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import AgentTask, SandboxId


@runtime_checkable
class Sandbox(Protocol):
    id: SandboxId

    def start(self) -> None: ...   # launch the agent loop + run its tool calls inside
    def kill(self) -> None: ...    # terminate + free


class BubblewrapSandbox:
    """Default backend: unprivileged bubblewrap + a cgroup v2 subtree (reuses the
    agent-pipeline loop). A future microVM/container/gVisor backend just implements the
    same `Sandbox` Protocol — nothing above this layer changes.
    """

    def __init__(self, id: SandboxId, task: AgentTask) -> None:
        self.id = id
        self._task = task

    def start(self) -> None:
        # create the cgroup subtree; spawn bubblewrap; drive the loop (LLM call in the
        # runtime, tool calls inside the sandbox); emit stage transitions.
        raise NotImplementedError

    def kill(self) -> None:
        # terminate the agent; rmdir the cgroup (frees its memory).
        raise NotImplementedError
