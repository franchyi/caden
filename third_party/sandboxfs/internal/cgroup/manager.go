package cgroup

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/franchyi/sandboxfs/internal/control"
)

type Manager struct {
	Root string
}

func PrepareCurrentSubtree(name string) (string, error) {
	file, err := os.Open("/proc/self/cgroup")
	if err != nil {
		return "", fmt.Errorf("open current cgroup: %w", err)
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "0::") {
			continue
		}
		relative := strings.TrimPrefix(strings.TrimPrefix(line, "0::"), "/")
		serviceRoot := filepath.Clean(filepath.Join("/sys/fs/cgroup", relative))
		return prepareSubtree(serviceRoot, name)
	}
	if err := scanner.Err(); err != nil {
		return "", fmt.Errorf("read current cgroup: %w", err)
	}
	return "", errors.New("unified cgroup v2 entry not found")
}

func prepareSubtree(serviceRoot, name string) (string, error) {
	daemonLeaf := filepath.Join(serviceRoot, "daemon")
	if err := os.MkdirAll(daemonLeaf, 0o755); err != nil {
		return "", fmt.Errorf("create daemon cgroup leaf: %w", err)
	}
	if err := write(daemonLeaf, "cgroup.procs", strconv.Itoa(os.Getpid())); err != nil {
		return "", fmt.Errorf("move daemon to leaf cgroup: %w", err)
	}
	if err := enableControllers(serviceRoot); err != nil {
		return "", err
	}
	sandboxRoot := filepath.Join(serviceRoot, name)
	if err := os.MkdirAll(sandboxRoot, 0o755); err != nil {
		return "", fmt.Errorf("create sandbox cgroup parent: %w", err)
	}
	if err := enableControllers(sandboxRoot); err != nil {
		return "", err
	}
	return sandboxRoot, nil
}

func enableControllers(path string) error {
	available, err := os.ReadFile(filepath.Join(path, "cgroup.controllers"))
	if err != nil {
		return fmt.Errorf("read controllers at %s: %w", path, err)
	}
	availableSet := make(map[string]bool)
	for _, controller := range strings.Fields(string(available)) {
		availableSet[controller] = true
	}
	wanted := make([]string, 0, 3)
	for _, controller := range []string{"cpu", "memory", "pids"} {
		if availableSet[controller] {
			wanted = append(wanted, "+"+controller)
		}
	}
	if len(wanted) == 0 {
		return fmt.Errorf("no cpu, memory, or pids controllers available at %s", path)
	}
	if err := os.WriteFile(filepath.Join(path, "cgroup.subtree_control"), []byte(strings.Join(wanted, " ")), 0o644); err != nil {
		return fmt.Errorf("enable controllers at %s: %w", path, err)
	}
	return nil
}

func (m Manager) Available() bool {
	_, err := os.Stat(filepath.Join(filepath.Dir(m.Root), "cgroup.controllers"))
	return err == nil
}

func (m Manager) Create(id string, limits control.Limits) (string, error) {
	if !m.Available() {
		return "", errors.New("cgroup v2 is not mounted")
	}
	if err := os.MkdirAll(m.Root, 0o755); err != nil {
		return "", fmt.Errorf("create cgroup root: %w", err)
	}
	path := filepath.Join(m.Root, id)
	if err := os.Mkdir(path, 0o755); err != nil {
		return "", fmt.Errorf("create sandbox cgroup: %w", err)
	}
	fail := func(err error) (string, error) {
		_ = os.Remove(path)
		return "", err
	}
	if limits.MemoryBytes > 0 {
		if err := write(path, "memory.max", strconv.FormatInt(limits.MemoryBytes, 10)); err != nil {
			return fail(err)
		}
	}
	if limits.Pids > 0 {
		if err := write(path, "pids.max", strconv.FormatInt(limits.Pids, 10)); err != nil {
			return fail(err)
		}
	}
	if limits.CPUQuota > 0 {
		period := limits.CPUPeriod
		if period <= 0 {
			period = 100000
		}
		if err := write(path, "cpu.max", fmt.Sprintf("%d %d", limits.CPUQuota, period)); err != nil {
			return fail(err)
		}
	}
	return path, nil
}

func (m Manager) AddProcess(path string, pid int) error {
	return write(path, "cgroup.procs", strconv.Itoa(pid))
}

func (m Manager) Remove(path string) error {
	if path == "" {
		return nil
	}
	err := os.Remove(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("remove cgroup %s: %w", path, err)
	}
	return nil
}

// KillAll terminates every process in one sandbox cgroup, including detached
// grandchildren that are not members of sandboxd's process group. cgroup.kill
// is the atomic cgroup-v2 mechanism; the PID loop is a compatibility fallback
// for older kernels.
func (m Manager) KillAll(path string) error {
	if path == "" {
		return nil
	}
	killPath := filepath.Join(path, "cgroup.kill")
	if err := os.WriteFile(killPath, []byte("1"), 0o644); err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("kill cgroup %s: %w", path, err)
		}
		if err := killProcesses(path); err != nil {
			return err
		}
	}

	deadline := time.Now().Add(2 * time.Second)
	for {
		processes, err := os.ReadFile(filepath.Join(path, "cgroup.procs"))
		if errors.Is(err, os.ErrNotExist) || len(strings.TrimSpace(string(processes))) == 0 {
			return nil
		}
		if err != nil {
			return fmt.Errorf("read cgroup processes %s: %w", path, err)
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("cgroup %s remained populated after kill", path)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func killProcesses(path string) error {
	payload, err := os.ReadFile(filepath.Join(path, "cgroup.procs"))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read cgroup processes %s: %w", path, err)
	}
	for _, field := range strings.Fields(string(payload)) {
		pid, err := strconv.Atoi(field)
		if err != nil {
			return fmt.Errorf("parse cgroup PID %q: %w", field, err)
		}
		if err := syscall.Kill(pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
			return fmt.Errorf("kill cgroup PID %d: %w", pid, err)
		}
	}
	return nil
}

func write(directory, name, value string) error {
	path := filepath.Join(directory, name)
	if err := os.WriteFile(path, []byte(value), 0o644); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return nil
}
