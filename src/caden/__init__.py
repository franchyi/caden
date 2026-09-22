"""Caden — stage-aware scheduling for dense AI-agent sandboxes.

Interfaces (see doc/scheduling-system-design.md):
  types     — shared value types
  sandbox   — Sandbox abstraction + BubblewrapSandbox backend
  execution — CpuPath / MemoryPath / Execution  (I2-cpu, I2-mem)
  scheduler — Caden  (I1)
"""

from .execution import (
    CpuPath,
    Execution,
    MemoryPath,
    SelectiveMemoryPath,
    StageReport,
)
from .policy import DecisionEvent, CadenPolicyConfig, StageAwareCaden
from .pool import ElasticReadyPool, PoolEvent, ReadyPool, ReadyPoolConfig
from .prediction import EmpiricalReturnEstimator, ReturnEstimate
from .sandbox import BubblewrapSandbox, Sandbox
from .sandboxfs_backend import (
    SandboxFSConfig,
    SandboxFSError,
    SandboxFSExecution,
    SandboxFSRecord,
)
from .scheduler import Caden, ReqState, ReqStatus
from .types import (
    AgentTask,
    CpuClass,
    DemotionRequest,
    DemotionResult,
    HostStat,
    ReclaimMode,
    ReqId,
    ResidencyMode,
    RestoreHint,
    RestoreRequest,
    RestoreResult,
    SandboxId,
    SandboxStat,
    Stage,
    StageContext,
    Tier,
)

__all__ = [  # noqa: RUF022 - grouped by public interface
    # types
    "AgentTask",
    "CpuClass",
    "DemotionRequest",
    "DemotionResult",
    "HostStat",
    "ReclaimMode",
    "ReqId",
    "ResidencyMode",
    "RestoreHint",
    "RestoreRequest",
    "RestoreResult",
    "SandboxId",
    "SandboxStat",
    "Stage",
    "StageContext",
    "Tier",
    # sandbox
    "Sandbox",
    "BubblewrapSandbox",
    # SandboxFS backend
    "SandboxFSConfig",
    "SandboxFSError",
    "SandboxFSExecution",
    "SandboxFSRecord",
    # elastic ready pool
    "ElasticReadyPool",
    "PoolEvent",
    "ReadyPool",
    "ReadyPoolConfig",
    # execution (I2)
    "CpuPath",
    "MemoryPath",
    "SelectiveMemoryPath",
    "Execution",
    "StageReport",
    # prediction and scheduler
    "EmpiricalReturnEstimator",
    "ReturnEstimate",
    "Caden",
    "ReqState",
    "ReqStatus",
    "DecisionEvent",
    "CadenPolicyConfig",
    "StageAwareCaden",
]
