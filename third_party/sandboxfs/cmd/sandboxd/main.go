package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/franchyi/sandboxfs/internal/agent"
)

func main() {
	socketPath := flag.String("socket", "/run/sandboxfs/control.sock", "Unix socket path")
	flag.Parse()

	server := agent.NewServer(*socketPath)
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		select {
		case <-signals:
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			_ = server.Close(ctx)
		case <-server.Done():
		}
	}()

	if err := server.Serve(); err != nil {
		fmt.Fprintf(os.Stderr, "sandboxd: %v\n", err)
		os.Exit(1)
	}
}
