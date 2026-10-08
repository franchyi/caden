package control

import (
	"time"

	"github.com/franchyi/sandboxfs/internal/rpc"
)

type Mode string

const (
	ModeBaseline Mode = "baseline"
	ModeT0       Mode = "t0"
	ModeT1       Mode = "t1"
)

func (m Mode) Valid() bool {
	return m == ModeBaseline || m == ModeT0 || m == ModeT1
}

type Limits struct {
	MemoryBytes int64 `json:"memory_bytes,omitempty"`
	Pids        int64 `json:"pids,omitempty"`
	CPUQuota    int64 `json:"cpu_quota,omitempty"`
	CPUPeriod   int64 `json:"cpu_period,omitempty"`
}

type BaseRecord struct {
	Name         string `json:"name"`
	Path         string `json:"path"`
	Digest       string `json:"digest"`
	Files        int64  `json:"files"`
	LogicalBytes int64  `json:"logical_bytes"`
	RegisteredAt string `json:"registered_at"`
	References   int    `json:"references"`
}

type RegisterBaseRequest struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

type VerifyBaseRequest struct {
	Name string `json:"name"`
}

type VerifyBaseResponse struct {
	Name     string `json:"name"`
	Expected string `json:"expected"`
	Actual   string `json:"actual"`
	Match    bool   `json:"match"`
}

type CreateSandboxRequest struct {
	ID     string `json:"id"`
	Base   string `json:"base"`
	Mode   Mode   `json:"mode"`
	Limits Limits `json:"limits,omitempty"`
}

type PhaseTimings struct {
	RequestReceivedAt   string `json:"request_received_at"`
	WorkspaceStartNS    int64  `json:"workspace_start_ns"`
	WorkspaceReadyNS    int64  `json:"workspace_ready_ns"`
	ProcessStartNS      int64  `json:"process_start_ns"`
	ProcessSpawnedNS    int64  `json:"process_spawned_ns"`
	SocketReadyNS       int64  `json:"socket_ready_ns"`
	ReadinessStartNS    int64  `json:"readiness_start_ns"`
	ReadinessCompleteNS int64  `json:"readiness_complete_ns"`
	TotalNS             int64  `json:"total_ns"`
}

type SandboxState struct {
	ID          string       `json:"id"`
	Base        string       `json:"base"`
	BaseDigest  string       `json:"base_digest"`
	Mode        Mode         `json:"mode"`
	Status      string       `json:"status"`
	PID         int          `json:"pid"`
	Root        string       `json:"root"`
	Workspace   string       `json:"workspace"`
	Control     string       `json:"control"`
	CreatedAt   string       `json:"created_at"`
	Limits      Limits       `json:"limits"`
	Timings     PhaseTimings `json:"timings"`
	LastError   string       `json:"last_error,omitempty"`
	AllocatedKB int64        `json:"allocated_kb,omitempty"`
}

type ExecRequest struct {
	Argv      []string          `json:"argv"`
	Dir       string            `json:"dir,omitempty"`
	Env       map[string]string `json:"env,omitempty"`
	TimeoutMS int64             `json:"timeout_ms,omitempty"`
}

func (r ExecRequest) AgentRequest() rpc.CommandRequest {
	return rpc.CommandRequest{
		Argv:      r.Argv,
		Dir:       r.Dir,
		Env:       r.Env,
		TimeoutMS: r.TimeoutMS,
	}
}

type ExecResponse struct {
	rpc.CommandResponse
	SandboxID string `json:"sandbox_id"`
}

type DestroyResponse struct {
	ID        string `json:"id"`
	CleanupNS int64  `json:"cleanup_ns"`
}

type ListBasesResponse struct {
	Bases []BaseRecord `json:"bases"`
}

type ListSandboxesResponse struct {
	Sandboxes []SandboxState `json:"sandboxes"`
}

type SystemResponse struct {
	Root           string `json:"root"`
	ReflinkEnabled bool   `json:"reflink_enabled"`
}

type ErrorResponse struct {
	Error string `json:"error"`
}

func Timestamp(value time.Time) string {
	return value.UTC().Format(time.RFC3339Nano)
}
