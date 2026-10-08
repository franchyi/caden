package agent

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/franchyi/sandboxfs/internal/rpc"
)

func TestServerExecAndShutdown(t *testing.T) {
	tempDir, err := os.MkdirTemp("/tmp", "sfd-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(tempDir) })
	socketPath := filepath.Join(tempDir, "agent.sock")
	server := NewServer(socketPath)
	errCh := make(chan error, 1)
	go func() { errCh <- server.Serve() }()

	client := NewClient(socketPath, 2*time.Second)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	for {
		if _, err := client.Health(ctx); err == nil {
			break
		}
		select {
		case err := <-errCh:
			t.Fatalf("server exited before readiness: %v", err)
		case <-ctx.Done():
			t.Fatalf("server did not become ready: %v", ctx.Err())
		case <-time.After(10 * time.Millisecond):
		}
	}

	response, err := client.Exec(ctx, rpc.CommandRequest{
		Argv: []string{"sh", "-c", "printf %s \"$SANDBOXFS_TEST\""},
		Dir:  "/",
		Env:  map[string]string{"SANDBOXFS_TEST": "ok"},
	})
	if err != nil {
		t.Fatalf("exec: %v", err)
	}
	if response.ExitCode != 0 || response.Stdout != "ok" || response.Error != "" {
		t.Fatalf("unexpected response: %+v", response)
	}

	timedOut, err := client.Exec(ctx, rpc.CommandRequest{
		Argv:      []string{"sh", "-c", "sleep 2"},
		Dir:       "/",
		TimeoutMS: 20,
	})
	if err != nil {
		t.Fatalf("timeout exec request: %v", err)
	}
	if timedOut.ExitCode != 124 || !strings.Contains(timedOut.Error, "deadline") {
		t.Fatalf("unexpected timeout response: %+v", timedOut)
	}

	if err := client.Shutdown(ctx); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("serve: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("server did not stop")
	}
}
