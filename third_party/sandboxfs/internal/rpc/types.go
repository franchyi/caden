package rpc

import "time"

type CommandRequest struct {
	Argv      []string          `json:"argv"`
	Dir       string            `json:"dir,omitempty"`
	Env       map[string]string `json:"env,omitempty"`
	TimeoutMS int64             `json:"timeout_ms,omitempty"`
}

type CommandResponse struct {
	ExitCode   int    `json:"exit_code"`
	Stdout     string `json:"stdout,omitempty"`
	Stderr     string `json:"stderr,omitempty"`
	Error      string `json:"error,omitempty"`
	DurationNS int64  `json:"duration_ns"`
}

type HealthResponse struct {
	OK        bool   `json:"ok"`
	PID       int    `json:"pid"`
	StartedAt string `json:"started_at"`
}

type ErrorResponse struct {
	Error string `json:"error"`
}

type ShutdownResponse struct {
	OK bool `json:"ok"`
}

func (r CommandRequest) Timeout() time.Duration {
	if r.TimeoutMS <= 0 {
		return 0
	}
	return time.Duration(r.TimeoutMS) * time.Millisecond
}
