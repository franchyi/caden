package host

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"time"

	"github.com/franchyi/sandboxfs/internal/control"
)

type Server struct {
	manager    *Manager
	socketPath string
	socketMode os.FileMode
	server     *http.Server
	listener   net.Listener
}

func NewServer(manager *Manager, socketPath string, socketMode os.FileMode) *Server {
	return &Server{manager: manager, socketPath: socketPath, socketMode: socketMode}
}

func (s *Server) Serve() error {
	if err := os.Remove(s.socketPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove stale API socket: %w", err)
	}
	listener, err := net.Listen("unix", s.socketPath)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", s.socketPath, err)
	}
	if err := os.Chmod(s.socketPath, s.socketMode); err != nil {
		listener.Close()
		return fmt.Errorf("chmod API socket: %w", err)
	}
	s.listener = listener

	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/system", s.handleSystem)
	mux.HandleFunc("GET /v1/bases", s.handleListBases)
	mux.HandleFunc("POST /v1/bases", s.handleRegisterBase)
	mux.HandleFunc("POST /v1/bases/{name}/verify", s.handleVerifyBase)
	mux.HandleFunc("GET /v1/sandboxes", s.handleListSandboxes)
	mux.HandleFunc("POST /v1/sandboxes", s.handleCreateSandbox)
	mux.HandleFunc("GET /v1/sandboxes/{id}", s.handleGetSandbox)
	mux.HandleFunc("DELETE /v1/sandboxes/{id}", s.handleDestroySandbox)
	mux.HandleFunc("POST /v1/sandboxes/{id}/exec", s.handleExec)
	s.server = &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}

	err = s.server.Serve(listener)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func (s *Server) Close(ctx context.Context) error {
	var failures []error
	if err := s.manager.Shutdown(ctx); err != nil {
		failures = append(failures, err)
	}
	if s.server != nil {
		if err := s.server.Shutdown(ctx); err != nil {
			failures = append(failures, err)
		}
	}
	if err := os.Remove(s.socketPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		failures = append(failures, err)
	}
	return errors.Join(failures...)
}

func (s *Server) handleSystem(writer http.ResponseWriter, _ *http.Request) {
	writeJSON(writer, http.StatusOK, control.SystemResponse{
		Root:           s.manager.config.Root,
		ReflinkEnabled: s.manager.ReflinkEnabled(),
	})
}

func (s *Server) handleListBases(writer http.ResponseWriter, _ *http.Request) {
	writeJSON(writer, http.StatusOK, control.ListBasesResponse{Bases: s.manager.ListBases()})
}

func (s *Server) handleRegisterBase(writer http.ResponseWriter, request *http.Request) {
	var payload control.RegisterBaseRequest
	if err := decodeJSON(writer, request, &payload); err != nil {
		writeError(writer, http.StatusBadRequest, err)
		return
	}
	record, err := s.manager.RegisterBase(payload)
	if err != nil {
		writeError(writer, http.StatusBadRequest, err)
		return
	}
	writeJSON(writer, http.StatusCreated, record)
}

func (s *Server) handleVerifyBase(writer http.ResponseWriter, request *http.Request) {
	verification, err := s.manager.VerifyBase(request.PathValue("name"))
	if err != nil {
		writeError(writer, http.StatusNotFound, err)
		return
	}
	writeJSON(writer, http.StatusOK, verification)
}

func (s *Server) handleListSandboxes(writer http.ResponseWriter, _ *http.Request) {
	writeJSON(writer, http.StatusOK, control.ListSandboxesResponse{Sandboxes: s.manager.List()})
}

func (s *Server) handleCreateSandbox(writer http.ResponseWriter, request *http.Request) {
	var payload control.CreateSandboxRequest
	if err := decodeJSON(writer, request, &payload); err != nil {
		writeError(writer, http.StatusBadRequest, err)
		return
	}
	state, err := s.manager.Create(request.Context(), payload)
	if err != nil {
		writeError(writer, http.StatusBadRequest, err)
		return
	}
	writeJSON(writer, http.StatusCreated, state)
}

func (s *Server) handleGetSandbox(writer http.ResponseWriter, request *http.Request) {
	state, found := s.manager.Get(request.PathValue("id"))
	if !found {
		writeError(writer, http.StatusNotFound, fmt.Errorf("unknown sandbox %q", request.PathValue("id")))
		return
	}
	writeJSON(writer, http.StatusOK, state)
}

func (s *Server) handleDestroySandbox(writer http.ResponseWriter, request *http.Request) {
	response, err := s.manager.Destroy(request.Context(), request.PathValue("id"))
	if err != nil {
		writeError(writer, http.StatusNotFound, err)
		return
	}
	writeJSON(writer, http.StatusOK, response)
}

func (s *Server) handleExec(writer http.ResponseWriter, request *http.Request) {
	var payload control.ExecRequest
	if err := decodeJSON(writer, request, &payload); err != nil {
		writeError(writer, http.StatusBadRequest, err)
		return
	}
	response, err := s.manager.Exec(request.Context(), request.PathValue("id"), payload)
	if err != nil {
		writeError(writer, http.StatusBadRequest, err)
		return
	}
	writeJSON(writer, http.StatusOK, response)
}

func decodeJSON(writer http.ResponseWriter, request *http.Request, value any) error {
	decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 4<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return fmt.Errorf("decode request: %w", err)
	}
	return nil
}

func writeError(writer http.ResponseWriter, status int, err error) {
	writeJSON(writer, status, control.ErrorResponse{Error: err.Error()})
}

func writeJSON(writer http.ResponseWriter, status int, value any) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(value)
}
