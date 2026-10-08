package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/franchyi/sandboxfs/internal/control"
	"github.com/franchyi/sandboxfs/internal/host"
)

func main() {
	root := flag.String("root", "/agent-xfs-t1", "active XFS campaign mount")
	stateDir := flag.String("state-dir", "/var/lib/sandboxfs", "durable daemon state")
	socket := flag.String("socket", "/run/sandboxfsd.sock", "host API Unix socket")
	sandboxUser := flag.String("sandbox-user", "ubuntu", "unprivileged sandbox account")
	rootFS := flag.String("rootfs", "/", "read-only root exposed through Bubblewrap")
	sandboxdPath := flag.String("sandboxd", "/usr/local/libexec/sandboxfs/sandboxd", "sandboxd path visible inside rootfs")
	startupTimeout := flag.Duration("startup-timeout", 15*time.Second, "sandbox startup deadline")
	commandTimeout := flag.Duration("command-timeout", 30*time.Second, "readiness command deadline")
	shutdownTimeout := flag.Duration("shutdown-timeout", 5*time.Second, "graceful shutdown deadline")
	memoryBytes := flag.Int64("default-memory-bytes", 8<<30, "default cgroup memory.max")
	pids := flag.Int64("default-pids", 512, "default cgroup pids.max")
	cpuQuota := flag.Int64("default-cpu-quota", 200000, "default cgroup CPU quota")
	cpuPeriod := flag.Int64("default-cpu-period", 100000, "default cgroup CPU period")
	disableCgroups := flag.Bool("disable-cgroups", false, "skip cgroup creation (development only)")
	flag.Parse()

	if os.Geteuid() != 0 {
		fmt.Fprintln(os.Stderr, "sandboxfsd must run as root")
		os.Exit(1)
	}

	manager, err := host.NewManager(host.Config{
		Root:            *root,
		StateDir:        *stateDir,
		SandboxUser:     *sandboxUser,
		RootFS:          *rootFS,
		SandboxdPath:    *sandboxdPath,
		StartupTimeout:  *startupTimeout,
		ShutdownTimeout: *shutdownTimeout,
		CommandTimeout:  *commandTimeout,
		DefaultLimits: control.Limits{
			MemoryBytes: *memoryBytes,
			Pids:        *pids,
			CPUQuota:    *cpuQuota,
			CPUPeriod:   *cpuPeriod,
		},
		DisableCgroups: *disableCgroups,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "sandboxfsd: initialize: %v\n", err)
		os.Exit(1)
	}
	server := host.NewServer(manager, *socket, 0o660)

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-signals
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		_ = server.Close(ctx)
	}()

	fmt.Printf("sandboxfsd: root=%s reflink=%t socket=%s\n", *root, manager.ReflinkEnabled(), *socket)
	if err := server.Serve(); err != nil {
		fmt.Fprintf(os.Stderr, "sandboxfsd: serve: %v\n", err)
		os.Exit(1)
	}
}
