from dataclasses import dataclass


@dataclass
class ResourceStats:
    min: float
    max: float
    avg: float


@dataclass
class ResourceSample:
    timestamp: str
    epoch: float
    mem_usage: str
    cpu_percent: str


@dataclass
class ResourceSummary:
    sample_count: int
    duration_seconds: float
    memory_mb: ResourceStats
    cpu_percent: ResourceStats


@dataclass
class ResourceData:
    samples: list[ResourceSample]
    summary: ResourceSummary


@dataclass
class SWEBenchRunResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclass
class SWEBenchResults:
    image: str
    start_time: str
    memory_limit: str | None
    cpu_limit: str | None
    model: str
    model_requested: str
    pull_time: float | None = None
    permission_fix_time: float | None = None
    image_info: dict | None = None
    output_dir: str | None = None
    claude_time: float | None = None
    model_actual: str | None = None
    claude_output: SWEBenchRunResult | None = None
    resource_samples: ResourceData | None = None
    disk_usage: str | None = None
    traces: dict | None = None
    cleaned: bool | None = None
    error: str | None = None
    total_time: float | None = None
    end_time: str | None = None
