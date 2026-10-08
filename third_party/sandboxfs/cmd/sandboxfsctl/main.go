package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/franchyi/sandboxfs/internal/control"
	"github.com/franchyi/sandboxfs/internal/host"
)

func main() {
	global := flag.NewFlagSet("sandboxfsctl", flag.ExitOnError)
	socket := global.String("socket", "/run/sandboxfsd.sock", "sandboxfsd Unix socket")
	timeout := global.Duration("timeout", 10*time.Minute, "API request timeout")
	global.Usage = usage
	_ = global.Parse(os.Args[1:])
	args := global.Args()
	if len(args) == 0 {
		usage()
		os.Exit(2)
	}

	client := host.NewClient(*socket, *timeout)
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	var err error
	switch args[0] {
	case "system":
		var value control.SystemResponse
		value, err = client.System(ctx)
		if err == nil {
			err = printJSON(value)
		}
	case "base-register":
		if len(args) != 3 {
			fatalUsage("base-register requires NAME PATH")
		}
		var value control.BaseRecord
		value, err = client.RegisterBase(ctx, control.RegisterBaseRequest{Name: args[1], Path: args[2]})
		if err == nil {
			err = printJSON(value)
		}
	case "base-list":
		var value control.ListBasesResponse
		value, err = client.ListBases(ctx)
		if err == nil {
			err = printJSON(value)
		}
	case "base-verify":
		if len(args) != 2 {
			fatalUsage("base-verify requires NAME")
		}
		var value control.VerifyBaseResponse
		value, err = client.VerifyBase(ctx, args[1])
		if err == nil {
			err = printJSON(value)
		}
	case "create":
		err = create(ctx, client, args[1:])
	case "list":
		var value control.ListSandboxesResponse
		value, err = client.List(ctx)
		if err == nil {
			err = printJSON(value)
		}
	case "get":
		if len(args) != 2 {
			fatalUsage("get requires ID")
		}
		var value control.SandboxState
		value, err = client.Get(ctx, args[1])
		if err == nil {
			err = printJSON(value)
		}
	case "exec":
		err = execute(ctx, client, args[1:])
	case "exec-json":
		err = executeJSON(ctx, client, args[1:])
	case "destroy":
		if len(args) != 2 {
			fatalUsage("destroy requires ID")
		}
		var value control.DestroyResponse
		value, err = client.Destroy(ctx, args[1])
		if err == nil {
			err = printJSON(value)
		}
	default:
		fatalUsage("unknown command: " + args[0])
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "sandboxfsctl: %v\n", err)
		os.Exit(1)
	}
}

func create(ctx context.Context, client *host.Client, args []string) error {
	flags := flag.NewFlagSet("create", flag.ContinueOnError)
	id := flags.String("id", "", "sandbox ID")
	base := flags.String("base", "", "registered base name")
	mode := flags.String("mode", "t1", "baseline, t0, or t1")
	memory := flags.Int64("memory-bytes", 0, "cgroup memory limit")
	pids := flags.Int64("pids", 0, "cgroup PID limit")
	cpuQuota := flags.Int64("cpu-quota", 0, "cgroup CPU quota")
	cpuPeriod := flags.Int64("cpu-period", 0, "cgroup CPU period")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *id == "" || *base == "" {
		return fmt.Errorf("create requires --id and --base")
	}
	request := control.CreateSandboxRequest{
		ID:   *id,
		Base: *base,
		Mode: control.Mode(*mode),
		Limits: control.Limits{
			MemoryBytes: *memory,
			Pids:        *pids,
			CPUQuota:    *cpuQuota,
			CPUPeriod:   *cpuPeriod,
		},
	}
	state, err := client.Create(ctx, request)
	if err != nil {
		return err
	}
	return printJSON(state)
}

func execute(ctx context.Context, client *host.Client, args []string) error {
	id, command, err := parseExec(args)
	if err != nil {
		return err
	}
	response, err := client.Exec(ctx, id, control.ExecRequest{Argv: command})
	if err != nil {
		return err
	}
	if response.Stdout != "" {
		_, _ = os.Stdout.WriteString(response.Stdout)
	}
	if response.Stderr != "" {
		_, _ = os.Stderr.WriteString(response.Stderr)
	}
	if response.ExitCode != 0 {
		return commandError{code: response.ExitCode, message: response.Error}
	}
	return nil
}

func executeJSON(ctx context.Context, client *host.Client, args []string) error {
	id, command, err := parseExec(args)
	if err != nil {
		return err
	}
	response, err := client.Exec(ctx, id, control.ExecRequest{Argv: command})
	if err != nil {
		return err
	}
	if err := printJSON(response); err != nil {
		return err
	}
	if response.ExitCode != 0 {
		return commandError{code: response.ExitCode, message: response.Error}
	}
	return nil
}

func parseExec(args []string) (string, []string, error) {
	if len(args) < 2 {
		return "", nil, fmt.Errorf("exec requires ID and command")
	}
	id := args[0]
	command := args[1:]
	if command[0] == "--" {
		command = command[1:]
	}
	if len(command) == 0 {
		return "", nil, fmt.Errorf("exec command must not be empty")
	}
	return id, command, nil
}

type commandError struct {
	code    int
	message string
}

func (e commandError) Error() string {
	return fmt.Sprintf("command exited %d: %s", e.code, e.message)
}

func printJSON(value any) error {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

func usage() {
	fmt.Fprint(os.Stderr, strings.TrimSpace(`
usage: sandboxfsctl [--socket PATH] COMMAND [ARGS]

commands:
  system
  base-register NAME PATH
  base-list
  base-verify NAME
  create --id ID --base NAME --mode baseline|t0|t1
  list
  get ID
  exec ID -- COMMAND [ARGS...]
  exec-json ID -- COMMAND [ARGS...]
  destroy ID
`)+"\n")
}

func fatalUsage(message string) {
	fmt.Fprintln(os.Stderr, message)
	usage()
	os.Exit(2)
}
