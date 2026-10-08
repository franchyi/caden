package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/franchyi/sandboxfs/internal/rpc"
)

type Server struct {
	socketPath string
	startedAt  time.Time
	server     *http.Server
	listener   net.Listener
	shutdown   chan struct{}
	closeOnce  sync.Once
}

func NewServer(socketPath string) *Server {
	return &Server{
		socketPath: socketPath,
		startedAt:  time.Now().UTC(),
		shutdown:   make(chan struct{}),
	}
}

func (s *Server) Serve() error {
	if err := os.MkdirAll(filepathDir(s.socketPath), 0o700); err != nil {
		return fmt.Errorf("create socket directory: %w", err)
	}
	if err := os.Remove(s.socketPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove stale socket: %w", err)
	}
	listener, err := net.Listen("unix", s.socketPath)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", s.socketPath, err)
	}
	if err := os.Chmod(s.socketPath, 0o600); err != nil {
		listener.Close()
		return fmt.Errorf("chmod socket: %w", err)
	}
	s.listener = listener

	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/health", s.handleHealth)
	mux.HandleFunc("POST /v1/exec", s.handleExec)
	mux.HandleFunc("POST /v1/shutdown", s.handleShutdown)
	s.server = &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	err = s.server.Serve(listener)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (s *Server) Done() <-chan struct{} {
	return s.shutdown
}

func (s *Server) Close(ctx context.Context) error {
	var closeErr error
	s.closeOnce.Do(func() {
		close(s.shutdown)
		if s.server != nil {
			closeErr = s.server.Shutdown(ctx)
		}
		_ = os.Remove(s.socketPath)
	})
	return closeErr
}

func (s *Server) handleHealth(writer http.ResponseWriter, _ *http.Request) {
	writeJSON(writer, http.StatusOK, rpc.HealthResponse{
		OK:        true,
		PID:       os.Getpid(),
		StartedAt: s.startedAt.Format(time.RFC3339Nano),
	})
}

func (s *Server) handleExec(writer http.ResponseWriter, request *http.Request) {
	var commandRequest rpc.CommandRequest
	decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 4<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&commandRequest); err != nil {
		writeError(writer, http.StatusBadRequest, fmt.Errorf("decode command: %w", err))
		return
	}
	if len(commandRequest.Argv) == 0 || strings.TrimSpace(commandRequest.Argv[0]) == "" {
		writeError(writer, http.StatusBadRequest, errors.New("argv must not be empty"))
		return
	}

	ctx := request.Context()
	cancel := func() {}
	if timeout := commandRequest.Timeout(); timeout > 0 {
		ctx, cancel = context.WithTimeout(ctx, timeout)
	}
	defer cancel()

	started := time.Now()
	command := exec.CommandContext(ctx, commandRequest.Argv[0], commandRequest.Argv[1:]...)
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error {
		if command.Process == nil {
			return nil
		}
		return syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
	}
	command.WaitDelay = 2 * time.Second
	if commandRequest.Dir != "" {
		command.Dir = commandRequest.Dir
	} else {
		command.Dir = "/workspace"
	}
	command.Env = mergedEnvironment(os.Environ(), commandRequest.Env)

	var stdout, stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	err := command.Run()
	response := rpc.CommandResponse{
		ExitCode:   0,
		Stdout:     stdout.String(),
		Stderr:     stderr.String(),
		DurationNS: time.Since(started).Nanoseconds(),
	}
	if err != nil {
		response.Error = err.Error()
		var exitError *exec.ExitError
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			response.ExitCode = 124
			response.Error = context.DeadlineExceeded.Error()
		} else if errors.As(err, &exitError) {
			response.ExitCode = exitError.ExitCode()
		} else {
			response.ExitCode = 127
		}
	}
	writeJSON(writer, http.StatusOK, response)
}

func (s *Server) handleShutdown(writer http.ResponseWriter, _ *http.Request) {
	writeJSON(writer, http.StatusOK, rpc.ShutdownResponse{OK: true})
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = s.Close(ctx)
	}()
}

func mergedEnvironment(base []string, overrides map[string]string) []string {
	values := make(map[string]string, len(base)+len(overrides))
	for _, entry := range base {
		key, value, found := strings.Cut(entry, "=")
		if found {
			values[key] = value
		}
	}
	for key, value := range overrides {
		values[key] = value
	}
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := make([]string, 0, len(keys))
	for _, key := range keys {
		result = append(result, key+"="+values[key])
	}
	return result
}

func writeError(writer http.ResponseWriter, status int, err error) {
	writeJSON(writer, status, rpc.ErrorResponse{Error: err.Error()})
}

func writeJSON(writer http.ResponseWriter, status int, value any) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(value)
}

func filepathDir(path string) string {
	index := strings.LastIndexByte(path, '/')
	if index <= 0 {
		return "."
	}
	return path[:index]
}
