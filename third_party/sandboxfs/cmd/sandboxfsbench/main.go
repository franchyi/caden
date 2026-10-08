package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/franchyi/sandboxfs/internal/control"
	"github.com/franchyi/sandboxfs/internal/host"
)

type Config struct {
	Base          string         `json:"base"`
	Modes         []control.Mode `json:"modes"`
	Iterations    int            `json:"iterations"`
	Concurrencies []int          `json:"concurrencies"`
	DropCaches    bool           `json:"drop_caches"`
	ToolCommand   []string       `json:"tool_command,omitempty"`
}

type Result struct {
	Mode          control.Mode          `json:"mode"`
	Concurrency   int                   `json:"concurrency"`
	Iteration     int                   `json:"iteration"`
	SandboxID     string                `json:"sandbox_id"`
	ClientTotalNS int64                 `json:"client_total_ns"`
	ToolClientNS  int64                 `json:"tool_client_ns,omitempty"`
	ToolCommandNS int64                 `json:"tool_command_ns,omitempty"`
	ToolReadyNS   int64                 `json:"tool_ready_ns,omitempty"`
	CleanupNS     int64                 `json:"cleanup_ns"`
	State         *control.SandboxState `json:"state,omitempty"`
	Error         string                `json:"error,omitempty"`
}

type Percentiles struct {
	Count int   `json:"count"`
	P50NS int64 `json:"p50_ns"`
	P95NS int64 `json:"p95_ns"`
	P99NS int64 `json:"p99_ns"`
}

type Summary struct {
	Mode        control.Mode `json:"mode"`
	Concurrency int          `json:"concurrency"`
	Succeeded   int          `json:"succeeded"`
	Failed      int          `json:"failed"`
	Ready       Percentiles  `json:"ready"`
	Workspace   Percentiles  `json:"workspace"`
	Client      Percentiles  `json:"client"`
	ToolClient  Percentiles  `json:"tool_client"`
	ToolCommand Percentiles  `json:"tool_command"`
	ToolReady   Percentiles  `json:"tool_ready"`
	Cleanup     Percentiles  `json:"cleanup"`
}

type Report struct {
	Schema      string                 `json:"schema"`
	GeneratedAt string                 `json:"generated_at"`
	Host        string                 `json:"host"`
	GOOS        string                 `json:"goos"`
	GOARCH      string                 `json:"goarch"`
	System      control.SystemResponse `json:"system"`
	Config      Config                 `json:"config"`
	Results     []Result               `json:"results"`
	Summaries   []Summary              `json:"summaries"`
}

func main() {
	socket := flag.String("socket", "/run/sandboxfsd.sock", "sandboxfsd Unix socket")
	base := flag.String("base", "", "registered base name")
	modesText := flag.String("modes", "baseline,t1", "comma-separated modes")
	iterations := flag.Int("iterations", 100, "iterations per mode/concurrency")
	concurrencyText := flag.String("concurrency", "1,8", "comma-separated concurrency levels")
	output := flag.String("output", "", "JSON report path")
	dropCaches := flag.Bool("drop-caches", false, "drop Linux page cache before each configuration")
	toolCommandJSON := flag.String("tool-command-json", "", "JSON argv for the first tool command after minimal readiness")
	timeout := flag.Duration("timeout", 10*time.Minute, "per-operation API timeout")
	flag.Parse()
	var toolCommand []string
	if *toolCommandJSON != "" {
		if err := json.Unmarshal([]byte(*toolCommandJSON), &toolCommand); err != nil {
			fatal(fmt.Errorf("parse --tool-command-json: %w", err))
		}
		if len(toolCommand) == 0 || strings.TrimSpace(toolCommand[0]) == "" {
			fatal(fmt.Errorf("--tool-command-json must contain a non-empty argv"))
		}
	}

	modes, err := parseModes(*modesText)
	if err != nil {
		fatal(err)
	}
	concurrencies, err := parsePositiveInts(*concurrencyText)
	if err != nil {
		fatal(err)
	}
	if *base == "" || *iterations <= 0 || *output == "" {
		fatal(fmt.Errorf("--base, positive --iterations, and --output are required"))
	}
	client := host.NewClient(*socket, *timeout)
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	system, err := client.System(ctx)
	cancel()
	if err != nil {
		fatal(err)
	}

	hostname, _ := os.Hostname()
	report := Report{
		Schema:      "sandboxfs-benchmark-v1",
		GeneratedAt: control.Timestamp(time.Now()),
		Host:        hostname,
		GOOS:        runtime.GOOS,
		GOARCH:      runtime.GOARCH,
		System:      system,
		Config: Config{
			Base:          *base,
			Modes:         modes,
			Iterations:    *iterations,
			Concurrencies: concurrencies,
			DropCaches:    *dropCaches,
			ToolCommand:   toolCommand,
		},
	}

	for _, mode := range modes {
		for _, concurrency := range concurrencies {
			if *dropCaches {
				if err := os.WriteFile("/proc/sys/vm/drop_caches", []byte("3\n"), 0o644); err != nil {
					fatal(fmt.Errorf("drop caches: %w", err))
				}
			}
			results := runConfiguration(client, *base, mode, *iterations, concurrency, toolCommand, *timeout)
			report.Results = append(report.Results, results...)
			report.Summaries = append(report.Summaries, summarize(mode, concurrency, results))
			fmt.Fprintf(os.Stderr, "completed mode=%s concurrency=%d iterations=%d\n", mode, concurrency, *iterations)
		}
	}

	if err := os.MkdirAll(filepath.Dir(*output), 0o755); err != nil {
		fatal(err)
	}
	file, err := os.Create(*output)
	if err != nil {
		fatal(err)
	}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(report); err != nil {
		file.Close()
		fatal(err)
	}
	if err := file.Close(); err != nil {
		fatal(err)
	}
	for _, summary := range report.Summaries {
		fmt.Printf("mode=%s concurrency=%d success=%d failed=%d ready_p50_ms=%.3f ready_p95_ms=%.3f ready_p99_ms=%.3f\n",
			summary.Mode, summary.Concurrency, summary.Succeeded, summary.Failed,
			float64(summary.Ready.P50NS)/1e6, float64(summary.Ready.P95NS)/1e6, float64(summary.Ready.P99NS)/1e6)
		if len(toolCommand) > 0 {
			fmt.Printf("mode=%s concurrency=%d tool_ready_p50_ms=%.3f tool_ready_p95_ms=%.3f tool_ready_p99_ms=%.3f tool_command_p50_ms=%.3f\n",
				summary.Mode, summary.Concurrency,
				float64(summary.ToolReady.P50NS)/1e6, float64(summary.ToolReady.P95NS)/1e6,
				float64(summary.ToolReady.P99NS)/1e6, float64(summary.ToolCommand.P50NS)/1e6)
		}
	}
}

func runConfiguration(client *host.Client, base string, mode control.Mode, iterations, concurrency int, toolCommand []string, timeout time.Duration) []Result {
	jobs := make(chan int)
	results := make(chan Result, iterations)
	var workers sync.WaitGroup
	runID := time.Now().UnixNano()
	for worker := 0; worker < concurrency; worker++ {
		workers.Add(1)
		go func(worker int) {
			defer workers.Done()
			for iteration := range jobs {
				id := fmt.Sprintf("bench-%s-c%d-%d-%d-%d", mode, concurrency, runID, worker, iteration)
				result := Result{Mode: mode, Concurrency: concurrency, Iteration: iteration, SandboxID: id}
				ctx, cancel := context.WithTimeout(context.Background(), timeout)
				started := time.Now()
				state, err := client.Create(ctx, control.CreateSandboxRequest{ID: id, Base: base, Mode: mode})
				result.ClientTotalNS = time.Since(started).Nanoseconds()
				cancel()
				if err != nil {
					result.Error = err.Error()
					results <- result
					continue
				}
				result.State = &state
				if len(toolCommand) > 0 {
					ctx, cancel = context.WithTimeout(context.Background(), timeout)
					toolStarted := time.Now()
					response, toolErr := client.Exec(ctx, id, control.ExecRequest{Argv: toolCommand})
					result.ToolClientNS = time.Since(toolStarted).Nanoseconds()
					result.ToolReadyNS = time.Since(started).Nanoseconds()
					cancel()
					if toolErr != nil {
						result.Error = "tool: " + toolErr.Error()
					} else if response.ExitCode != 0 {
						result.Error = fmt.Sprintf("tool exited %d: %s", response.ExitCode, response.Error)
					} else {
						result.ToolCommandNS = response.DurationNS
					}
				}
				ctx, cancel = context.WithTimeout(context.Background(), timeout)
				destroy, destroyErr := client.Destroy(ctx, id)
				cancel()
				if destroyErr != nil {
					result.Error = "destroy: " + destroyErr.Error()
				} else {
					result.CleanupNS = destroy.CleanupNS
				}
				results <- result
			}
		}(worker)
	}
	go func() {
		for iteration := 0; iteration < iterations; iteration++ {
			jobs <- iteration
		}
		close(jobs)
		workers.Wait()
		close(results)
	}()
	collected := make([]Result, 0, iterations)
	for result := range results {
		collected = append(collected, result)
	}
	sort.Slice(collected, func(i, j int) bool { return collected[i].Iteration < collected[j].Iteration })
	return collected
}

func summarize(mode control.Mode, concurrency int, results []Result) Summary {
	var ready, workspace, client, toolClient, toolCommand, toolReady, cleanup []int64
	failed := 0
	for _, result := range results {
		if result.Error != "" || result.State == nil {
			failed++
			continue
		}
		ready = append(ready, result.State.Timings.TotalNS)
		workspace = append(workspace, result.State.Timings.WorkspaceReadyNS-result.State.Timings.WorkspaceStartNS)
		client = append(client, result.ClientTotalNS)
		if result.ToolReadyNS > 0 {
			toolClient = append(toolClient, result.ToolClientNS)
			toolCommand = append(toolCommand, result.ToolCommandNS)
			toolReady = append(toolReady, result.ToolReadyNS)
		}
		cleanup = append(cleanup, result.CleanupNS)
	}
	return Summary{
		Mode:        mode,
		Concurrency: concurrency,
		Succeeded:   len(ready),
		Failed:      failed,
		Ready:       percentiles(ready),
		Workspace:   percentiles(workspace),
		Client:      percentiles(client),
		ToolClient:  percentiles(toolClient),
		ToolCommand: percentiles(toolCommand),
		ToolReady:   percentiles(toolReady),
		Cleanup:     percentiles(cleanup),
	}
}

func percentiles(values []int64) Percentiles {
	if len(values) == 0 {
		return Percentiles{}
	}
	sorted := append([]int64(nil), values...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	return Percentiles{
		Count: len(sorted),
		P50NS: nearestRank(sorted, 0.50),
		P95NS: nearestRank(sorted, 0.95),
		P99NS: nearestRank(sorted, 0.99),
	}
}

func nearestRank(values []int64, percentile float64) int64 {
	index := int(float64(len(values))*percentile+0.999999) - 1
	if index < 0 {
		index = 0
	}
	if index >= len(values) {
		index = len(values) - 1
	}
	return values[index]
}

func parseModes(text string) ([]control.Mode, error) {
	var modes []control.Mode
	for _, field := range strings.Split(text, ",") {
		mode := control.Mode(strings.TrimSpace(field))
		if !mode.Valid() {
			return nil, fmt.Errorf("invalid mode %q", mode)
		}
		modes = append(modes, mode)
	}
	return modes, nil
}

func parsePositiveInts(text string) ([]int, error) {
	var values []int
	for _, field := range strings.Split(text, ",") {
		value, err := strconv.Atoi(strings.TrimSpace(field))
		if err != nil || value <= 0 {
			return nil, fmt.Errorf("invalid positive integer %q", field)
		}
		values = append(values, value)
	}
	return values, nil
}

func fatal(err error) {
	fmt.Fprintf(os.Stderr, "sandboxfsbench: %v\n", err)
	os.Exit(1)
}
