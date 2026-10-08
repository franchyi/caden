package host

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/franchyi/sandboxfs/internal/agent"
	"github.com/franchyi/sandboxfs/internal/basestore"
	"github.com/franchyi/sandboxfs/internal/cgroup"
	"github.com/franchyi/sandboxfs/internal/control"
	"github.com/franchyi/sandboxfs/internal/rpc"
)

var identifierPattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$`)

type Config struct {
	Root            string
	StateDir        string
	SandboxUser     string
	RootFS          string
	BubblewrapPath  string
	SandboxdPath    string
	CgroupRoot      string
	StartupTimeout  time.Duration
	ShutdownTimeout time.Duration
	CommandTimeout  time.Duration
	DefaultLimits   control.Limits
	DisableCgroups  bool
}

type Manager struct {
	mu       sync.RWMutex
	config   Config
	bases    *basestore.Store
	cgroups  cgroup.Manager
	runtimes map[string]*runtime
	uid      int
	gid      int
	reflink  bool
}

type runtime struct {
	state      control.SandboxState
	process    *exec.Cmd
	done       chan error
	agent      *agent.Client
	cgroupPath string
	logFile    *os.File
}

func NewManager(config Config) (*Manager, error) {
	if config.Root == "" || config.StateDir == "" {
		return nil, errors.New("root and state directory are required")
	}
	resolvedRoot, err := filepath.EvalSymlinks(config.Root)
	if err != nil {
		return nil, fmt.Errorf("resolve root: %w", err)
	}
	config.Root = filepath.Clean(resolvedRoot)
	if config.SandboxUser == "" {
		config.SandboxUser = "ubuntu"
	}
	if config.RootFS == "" {
		config.RootFS = "/"
	}
	if config.BubblewrapPath == "" {
		config.BubblewrapPath = "/usr/bin/bwrap"
	}
	if config.SandboxdPath == "" {
		config.SandboxdPath = "/usr/local/libexec/sandboxfs/sandboxd"
	}
	if config.CgroupRoot == "" {
		config.CgroupRoot, err = cgroup.PrepareCurrentSubtree("sandboxfs")
		if err != nil && !config.DisableCgroups {
			return nil, err
		}
	}
	if config.StartupTimeout <= 0 {
		config.StartupTimeout = 10 * time.Second
	}
	if config.ShutdownTimeout <= 0 {
		config.ShutdownTimeout = 5 * time.Second
	}
	if config.CommandTimeout <= 0 {
		config.CommandTimeout = 30 * time.Second
	}
	if err := os.MkdirAll(config.StateDir, 0o750); err != nil {
		return nil, fmt.Errorf("create state directory: %w", err)
	}
	if err := os.MkdirAll(filepath.Join(config.Root, "bases"), 0o755); err != nil {
		return nil, fmt.Errorf("create bases directory: %w", err)
	}
	if err := os.MkdirAll(filepath.Join(config.Root, "sandboxes"), 0o755); err != nil {
		return nil, fmt.Errorf("create sandboxes directory: %w", err)
	}
	if err := os.MkdirAll(filepath.Join(config.Root, "measurements"), 0o755); err != nil {
		return nil, fmt.Errorf("create measurements directory: %w", err)
	}

	account, err := user.Lookup(config.SandboxUser)
	if err != nil {
		return nil, fmt.Errorf("lookup sandbox user %q: %w", config.SandboxUser, err)
	}
	uid, err := strconv.Atoi(account.Uid)
	if err != nil {
		return nil, fmt.Errorf("parse sandbox uid: %w", err)
	}
	gid, err := strconv.Atoi(account.Gid)
	if err != nil {
		return nil, fmt.Errorf("parse sandbox gid: %w", err)
	}
	store, err := basestore.Open(filepath.Join(config.StateDir, "bases.json"))
	if err != nil {
		return nil, err
	}
	reflink, err := detectReflink(config.Root)
	if err != nil {
		return nil, err
	}
	manager := &Manager{
		config:   config,
		bases:    store,
		cgroups:  cgroup.Manager{Root: config.CgroupRoot},
		runtimes: make(map[string]*runtime),
		uid:      uid,
		gid:      gid,
		reflink:  reflink,
	}
	if err := manager.Reconcile(); err != nil {
		return nil, err
	}
	return manager, nil
}

func (m *Manager) ReflinkEnabled() bool {
	return m.reflink
}

func (m *Manager) RegisterBase(request control.RegisterBaseRequest) (control.BaseRecord, error) {
	if err := m.validateBasePath(request.Path); err != nil {
		return control.BaseRecord{}, err
	}
	return m.bases.Register(request.Name, request.Path)
}

func (m *Manager) VerifyBase(name string) (control.VerifyBaseResponse, error) {
	return m.bases.Verify(name)
}

func (m *Manager) ListBases() []control.BaseRecord {
	return m.bases.List()
}

func (m *Manager) Create(ctx context.Context, request control.CreateSandboxRequest) (control.SandboxState, error) {
	requestStarted := time.Now()
	if !identifierPattern.MatchString(request.ID) {
		return control.SandboxState{}, fmt.Errorf("invalid sandbox ID %q", request.ID)
	}
	if !request.Mode.Valid() {
		return control.SandboxState{}, fmt.Errorf("invalid mode %q", request.Mode)
	}
	if err := m.validateMode(request.Mode); err != nil {
		return control.SandboxState{}, err
	}
	base, found := m.bases.Get(request.Base)
	if !found {
		return control.SandboxState{}, fmt.Errorf("unknown base %q", request.Base)
	}
	if err := m.validateBasePath(base.Path); err != nil {
		return control.SandboxState{}, err
	}
	limits, err := m.effectiveLimits(request.Limits)
	if err != nil {
		return control.SandboxState{}, err
	}

	sandboxRoot := filepath.Join(m.config.Root, "sandboxes", request.ID)
	state := control.SandboxState{
		ID:         request.ID,
		Base:       base.Name,
		BaseDigest: base.Digest,
		Mode:       request.Mode,
		Status:     "creating",
		Root:       sandboxRoot,
		Control:    filepath.Join(sandboxRoot, "control"),
		CreatedAt:  control.Timestamp(requestStarted),
		Limits:     limits,
		Timings: control.PhaseTimings{
			RequestReceivedAt: control.Timestamp(requestStarted),
		},
	}
	run := &runtime{state: state, done: make(chan error, 1)}
	m.mu.Lock()
	if _, exists := m.runtimes[request.ID]; exists {
		m.mu.Unlock()
		return control.SandboxState{}, fmt.Errorf("sandbox %q already exists", request.ID)
	}
	if _, err := os.Stat(sandboxRoot); err == nil {
		m.mu.Unlock()
		return control.SandboxState{}, fmt.Errorf("sandbox path already exists: %s", sandboxRoot)
	} else if !errors.Is(err, os.ErrNotExist) {
		m.mu.Unlock()
		return control.SandboxState{}, fmt.Errorf("inspect sandbox path: %w", err)
	}
	m.runtimes[request.ID] = run
	m.mu.Unlock()

	failed := true
	defer func() {
		if failed {
			m.cleanupFailedCreate(run)
		}
	}()

	state.Timings.WorkspaceStartNS = time.Since(requestStarted).Nanoseconds()
	workspace, err := m.prepareWorkspace(ctx, state, base)
	if err != nil {
		return control.SandboxState{}, err
	}
	state.Workspace = workspace
	state.Timings.WorkspaceReadyNS = time.Since(requestStarted).Nanoseconds()
	state.Timings.ProcessStartNS = time.Since(requestStarted).Nanoseconds()
	run.state = state

	if !m.config.DisableCgroups {
		cgroupPath, err := m.cgroups.Create(request.ID, limits)
		if err != nil {
			return control.SandboxState{}, err
		}
		run.cgroupPath = cgroupPath
	}
	if err := m.startProcess(run); err != nil {
		return control.SandboxState{}, err
	}
	state = run.state
	state.Timings.ProcessSpawnedNS = time.Since(requestStarted).Nanoseconds()
	run.state = state

	if run.cgroupPath != "" {
		if err := m.cgroups.AddProcess(run.cgroupPath, run.process.Process.Pid); err != nil {
			return control.SandboxState{}, err
		}
	}

	agentClient, err := m.waitForAgent(ctx, run)
	if err != nil {
		return control.SandboxState{}, err
	}
	run.agent = agentClient
	state = run.state
	state.Timings.SocketReadyNS = time.Since(requestStarted).Nanoseconds()
	state.Timings.ReadinessStartNS = time.Since(requestStarted).Nanoseconds()
	run.state = state
	readinessContext, cancel := context.WithTimeout(context.Background(), m.config.CommandTimeout)
	readiness, err := agentClient.Exec(readinessContext, rpc.CommandRequest{Argv: []string{"/bin/true"}})
	cancel()
	if err != nil {
		return control.SandboxState{}, fmt.Errorf("readiness request: %w", err)
	}
	if readiness.ExitCode != 0 {
		return control.SandboxState{}, fmt.Errorf("readiness command failed: %s", readiness.Error)
	}
	state = run.state
	state.Timings.ReadinessCompleteNS = time.Since(requestStarted).Nanoseconds()
	state.Timings.TotalNS = state.Timings.ReadinessCompleteNS
	state.Status = "running"
	state.PID = run.process.Process.Pid
	state.AllocatedKB = allocatedKB(sandboxRoot)
	run.state = state
	if err := writeState(state); err != nil {
		return control.SandboxState{}, err
	}
	if err := m.bases.AddReference(base.Name, 1); err != nil {
		return control.SandboxState{}, err
	}
	failed = false
	return state, nil
}

func (m *Manager) Exec(ctx context.Context, id string, request control.ExecRequest) (control.ExecResponse, error) {
	if len(request.Argv) == 0 {
		return control.ExecResponse{}, errors.New("argv must not be empty")
	}
	m.mu.RLock()
	run, found := m.runtimes[id]
	if !found || run.state.Status != "running" {
		m.mu.RUnlock()
		return control.ExecResponse{}, fmt.Errorf("sandbox %q is not running", id)
	}
	agentClient := run.agent
	m.mu.RUnlock()
	response, err := agentClient.Exec(ctx, request.AgentRequest())
	if err != nil {
		return control.ExecResponse{}, err
	}
	return control.ExecResponse{CommandResponse: response, SandboxID: id}, nil
}

func (m *Manager) Destroy(ctx context.Context, id string) (control.DestroyResponse, error) {
	started := time.Now()
	m.mu.Lock()
	run, found := m.runtimes[id]
	if !found {
		m.mu.Unlock()
		return control.DestroyResponse{}, fmt.Errorf("unknown sandbox %q", id)
	}
	run.state.Status = "destroying"
	_ = writeState(run.state)
	m.mu.Unlock()

	shutdownContext, cancel := context.WithTimeout(ctx, m.config.ShutdownTimeout)
	if run.agent != nil {
		_ = run.agent.Shutdown(shutdownContext)
	}
	cancel()
	if run.process != nil && run.process.Process != nil {
		select {
		case <-run.done:
		case <-time.After(m.config.ShutdownTimeout):
			_ = syscall.Kill(-run.process.Process.Pid, syscall.SIGKILL)
			select {
			case <-run.done:
			case <-time.After(2 * time.Second):
			}
		}
	}
	cleanupErr := m.cleanupRuntime(run)
	m.mu.Lock()
	delete(m.runtimes, id)
	m.mu.Unlock()
	_ = m.bases.AddReference(run.state.Base, -1)
	if cleanupErr != nil {
		return control.DestroyResponse{}, cleanupErr
	}
	return control.DestroyResponse{ID: id, CleanupNS: time.Since(started).Nanoseconds()}, nil
}

func (m *Manager) Get(id string) (control.SandboxState, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	run, found := m.runtimes[id]
	if !found {
		return control.SandboxState{}, false
	}
	state := run.state
	state.AllocatedKB = allocatedKB(state.Root)
	return state, true
}

func (m *Manager) List() []control.SandboxState {
	m.mu.RLock()
	defer m.mu.RUnlock()
	result := make([]control.SandboxState, 0, len(m.runtimes))
	for _, run := range m.runtimes {
		state := run.state
		state.AllocatedKB = allocatedKB(state.Root)
		result = append(result, state)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
	return result
}

func (m *Manager) Shutdown(ctx context.Context) error {
	states := m.List()
	var failures []error
	for _, state := range states {
		if _, err := m.Destroy(ctx, state.ID); err != nil {
			failures = append(failures, err)
		}
	}
	return errors.Join(failures...)
}

func (m *Manager) Reconcile() error {
	if err := m.bases.ResetReferences(); err != nil {
		return err
	}
	sandboxesRoot := filepath.Join(m.config.Root, "sandboxes")
	entries, err := os.ReadDir(sandboxesRoot)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("scan stale sandboxes: %w", err)
	}
	var failures []error
	for _, entry := range entries {
		path := filepath.Join(sandboxesRoot, entry.Name())
		if !entry.IsDir() || !identifierPattern.MatchString(entry.Name()) {
			continue
		}
		state, _ := readState(filepath.Join(path, "state.json"))
		if state.PID > 1 && processLooksLikeSandbox(state.PID) {
			_ = syscall.Kill(-state.PID, syscall.SIGKILL)
		}
		merged := filepath.Join(path, "merged")
		if mounted(merged) {
			if err := unmount(merged); err != nil {
				failures = append(failures, err)
				continue
			}
		}
		if err := os.RemoveAll(path); err != nil {
			failures = append(failures, fmt.Errorf("remove stale sandbox %s: %w", path, err))
		}
	}
	return errors.Join(failures...)
}

func (m *Manager) prepareWorkspace(ctx context.Context, state control.SandboxState, base control.BaseRecord) (string, error) {
	if err := os.Mkdir(state.Root, 0o755); err != nil {
		return "", fmt.Errorf("create sandbox root: %w", err)
	}
	if err := os.Mkdir(state.Control, 0o700); err != nil {
		return "", fmt.Errorf("create control directory: %w", err)
	}
	if err := os.Chown(state.Control, m.uid, m.gid); err != nil {
		return "", fmt.Errorf("chown control directory: %w", err)
	}
	if state.Mode == control.ModeBaseline {
		workspace := filepath.Join(state.Root, "workspace")
		if err := os.Mkdir(workspace, 0o755); err != nil {
			return "", fmt.Errorf("create baseline workspace: %w", err)
		}
		// Preserve the literal trailing `/.`; filepath.Join would clean it away
		// and make cp nest the base directory inside the private workspace.
		copySource := base.Path + string(os.PathSeparator) + "."
		command := exec.CommandContext(ctx, "cp", "-a", "--reflink=never", copySource, workspace)
		output, err := command.CombinedOutput()
		if err != nil {
			return "", fmt.Errorf("full-copy baseline: %w: %s", err, strings.TrimSpace(string(output)))
		}
		return workspace, nil
	}
	upper := filepath.Join(state.Root, "upper")
	work := filepath.Join(state.Root, "work")
	merged := filepath.Join(state.Root, "merged")
	for _, directory := range []string{upper, work, merged} {
		if err := os.Mkdir(directory, 0o755); err != nil {
			return "", fmt.Errorf("create overlay directory %s: %w", directory, err)
		}
	}
	if err := os.Chown(upper, m.uid, m.gid); err != nil {
		return "", fmt.Errorf("chown upper directory: %w", err)
	}
	options := fmt.Sprintf("lowerdir=%s,upperdir=%s,workdir=%s", base.Path, upper, work)
	command := exec.CommandContext(ctx, "mount", "-t", "overlay", "overlay", "-o", options, merged)
	output, err := command.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("mount overlay: %w: %s", err, strings.TrimSpace(string(output)))
	}
	return merged, nil
}

func (m *Manager) startProcess(run *runtime) error {
	logPath := filepath.Join(run.state.Root, "sandboxd.log")
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
	if err != nil {
		return fmt.Errorf("open sandbox log: %w", err)
	}
	run.logFile = logFile
	args := []string{
		"--reuid", strconv.Itoa(m.uid),
		"--regid", strconv.Itoa(m.gid),
		"--init-groups",
		"--",
		m.config.BubblewrapPath,
		"--unshare-all",
		"--die-with-parent",
		"--new-session",
	}
	if filepath.Clean(m.config.RootFS) == "/" {
		// Never recursively bind the live host root. It contains other
		// sandboxes' transient OverlayFS mounts, and concurrent mount teardown
		// can race Bubblewrap's recursive read-only remount pass.
		args = append(args,
			"--ro-bind", "/usr", "/usr",
			"--ro-bind", "/etc", "/etc",
			"--symlink", "usr/bin", "/bin",
			"--symlink", "usr/sbin", "/sbin",
			"--symlink", "usr/lib", "/lib",
			"--symlink", "usr/lib64", "/lib64",
			"--dir", "/home",
			"--dir", "/opt",
			"--dir", "/var",
			"--dir", "/workspace",
		)
	} else {
		args = append(args, "--ro-bind", m.config.RootFS, "/")
	}
	args = append(args,
		"--proc", "/proc",
		"--dev", "/dev",
		"--tmpfs", "/tmp",
		"--tmpfs", "/run",
		"--bind", run.state.Workspace, "/workspace",
		"--dir", "/run/sandboxfs",
		"--bind", run.state.Control, "/run/sandboxfs",
		"--setenv", "HOME", "/workspace",
		"--setenv", "SANDBOXFS_ID", run.state.ID,
		"--chdir", "/workspace",
		"--", m.config.SandboxdPath,
		"--socket", "/run/sandboxfs/control.sock",
	)
	// setpriv performs a direct credential transition without opening a PAM
	// session. This avoids runuser/logind churn during concurrent cold starts.
	command := exec.Command("setpriv", args...)
	command.Stdout = logFile
	command.Stderr = logFile
	closeCgroup, err := configureProcessCgroup(command, run.cgroupPath)
	if err != nil {
		logFile.Close()
		return err
	}
	if err := command.Start(); err != nil {
		closeCgroup()
		logFile.Close()
		return fmt.Errorf("start bubblewrap: %w", err)
	}
	closeCgroup()
	run.process = command
	run.state.PID = command.Process.Pid
	go func() {
		err := command.Wait()
		_ = logFile.Close()
		run.done <- err
		close(run.done)
		m.mu.Lock()
		if current, found := m.runtimes[run.state.ID]; found && current == run && run.state.Status == "running" {
			run.state.Status = "exited"
			if err != nil {
				run.state.LastError = err.Error()
			}
			_ = writeState(run.state)
		}
		m.mu.Unlock()
	}()
	return nil
}

func (m *Manager) waitForAgent(parent context.Context, run *runtime) (*agent.Client, error) {
	deadline := time.Now().Add(m.config.StartupTimeout)
	socketPath := filepath.Join(run.state.Control, "control.sock")
	// Health probes already carry their own short context below.  Keep the
	// client's transport timeout long enough for normal exec requests; using a
	// 500 ms client timeout here made valid post-reclaim commands fail while
	// waiting for response headers.
	client := agent.NewClient(socketPath, m.config.CommandTimeout)
	var lastErr error
	for time.Now().Before(deadline) {
		select {
		case processErr := <-run.done:
			return nil, fmt.Errorf("sandbox process exited before readiness: %w; log: %s", processErr, readLogTail(filepath.Join(run.state.Root, "sandboxd.log"), 4096))
		default:
		}
		attemptContext, cancel := context.WithTimeout(parent, 400*time.Millisecond)
		_, err := client.Health(attemptContext)
		cancel()
		if err == nil {
			return client, nil
		}
		lastErr = err
		select {
		case <-parent.Done():
			return nil, parent.Err()
		case <-time.After(20 * time.Millisecond):
		}
	}
	return nil, fmt.Errorf("sandboxd did not become ready: %w", lastErr)
}

func (m *Manager) cleanupFailedCreate(run *runtime) {
	if run.process != nil && run.process.Process != nil {
		_ = syscall.Kill(-run.process.Process.Pid, syscall.SIGKILL)
		select {
		case <-run.done:
		case <-time.After(2 * time.Second):
		}
	}
	_ = m.cleanupRuntime(run)
	m.mu.Lock()
	delete(m.runtimes, run.state.ID)
	m.mu.Unlock()
}

func (m *Manager) cleanupRuntime(run *runtime) error {
	var failures []error
	if err := m.cgroups.KillAll(run.cgroupPath); err != nil {
		failures = append(failures, err)
	}
	merged := filepath.Join(run.state.Root, "merged")
	if mounted(merged) {
		if err := unmount(merged); err != nil {
			failures = append(failures, err)
		}
	}
	if err := m.cgroups.Remove(run.cgroupPath); err != nil {
		failures = append(failures, err)
	}
	if err := safeRemoveSandbox(m.config.Root, run.state.Root); err != nil {
		failures = append(failures, err)
	}
	return errors.Join(failures...)
}

func (m *Manager) validateMode(mode control.Mode) error {
	switch mode {
	case control.ModeBaseline:
		return nil
	case control.ModeT0:
		if m.reflink {
			return errors.New("T0 requires XFS formatted with reflink=0")
		}
	case control.ModeT1:
		if !m.reflink {
			return errors.New("T1 requires XFS formatted with reflink=1")
		}
	}
	return nil
}

func (m *Manager) validateBasePath(path string) error {
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		return fmt.Errorf("resolve base path: %w", err)
	}
	basesRoot := filepath.Join(m.config.Root, "bases")
	inside, err := pathWithin(basesRoot, resolved)
	if err != nil {
		return err
	}
	if !inside || filepath.Clean(resolved) == filepath.Clean(basesRoot) {
		return fmt.Errorf("base must be a child of %s", basesRoot)
	}
	return nil
}

func (m *Manager) effectiveLimits(request control.Limits) (control.Limits, error) {
	if request.MemoryBytes < 0 || request.Pids < 0 || request.CPUQuota < 0 || request.CPUPeriod < 0 {
		return control.Limits{}, errors.New("resource limits must not be negative")
	}
	result := request
	if result.MemoryBytes == 0 {
		result.MemoryBytes = m.config.DefaultLimits.MemoryBytes
	}
	if result.Pids == 0 {
		result.Pids = m.config.DefaultLimits.Pids
	}
	if result.CPUQuota == 0 {
		result.CPUQuota = m.config.DefaultLimits.CPUQuota
	}
	if result.CPUPeriod == 0 {
		result.CPUPeriod = m.config.DefaultLimits.CPUPeriod
	}
	return result, nil
}

func detectReflink(root string) (bool, error) {
	command := exec.Command("xfs_info", root)
	output, err := command.CombinedOutput()
	if err != nil {
		return false, fmt.Errorf("inspect XFS at %s: %w: %s", root, err, strings.TrimSpace(string(output)))
	}
	text := string(output)
	if strings.Contains(text, "reflink=1") {
		return true, nil
	}
	if strings.Contains(text, "reflink=0") {
		return false, nil
	}
	return false, errors.New("xfs_info output does not report reflink=0 or reflink=1")
}

func writeState(state control.SandboxState) error {
	path := filepath.Join(state.Root, "state.json")
	temp, err := os.CreateTemp(state.Root, ".state-*.json")
	if err != nil {
		return fmt.Errorf("create state file: %w", err)
	}
	tempName := temp.Name()
	defer os.Remove(tempName)
	encoder := json.NewEncoder(temp)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(state); err != nil {
		temp.Close()
		return fmt.Errorf("encode state: %w", err)
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return fmt.Errorf("sync state: %w", err)
	}
	if err := temp.Close(); err != nil {
		return fmt.Errorf("close state: %w", err)
	}
	if err := os.Rename(tempName, path); err != nil {
		return fmt.Errorf("replace state: %w", err)
	}
	return nil
}

func readState(path string) (control.SandboxState, error) {
	file, err := os.Open(path)
	if err != nil {
		return control.SandboxState{}, err
	}
	defer file.Close()
	var state control.SandboxState
	if err := json.NewDecoder(io.LimitReader(file, 1<<20)).Decode(&state); err != nil {
		return control.SandboxState{}, err
	}
	return state, nil
}

func mounted(path string) bool {
	if _, err := os.Stat(path); err != nil {
		return false
	}
	return exec.Command("mountpoint", "-q", path).Run() == nil
}

func unmount(path string) error {
	output, err := exec.Command("umount", path).CombinedOutput()
	if err != nil {
		return fmt.Errorf("unmount %s: %w: %s", path, err, strings.TrimSpace(string(output)))
	}
	return nil
}

func safeRemoveSandbox(root, sandboxRoot string) error {
	expected := filepath.Join(filepath.Clean(root), "sandboxes")
	inside, err := pathWithin(expected, sandboxRoot)
	if err != nil {
		return err
	}
	if !inside || filepath.Clean(sandboxRoot) == expected {
		return fmt.Errorf("refusing unsafe sandbox removal: %s", sandboxRoot)
	}
	if err := os.RemoveAll(sandboxRoot); err != nil {
		return fmt.Errorf("remove sandbox %s: %w", sandboxRoot, err)
	}
	return nil
}

func pathWithin(parent, child string) (bool, error) {
	parentAbsolute, err := filepath.Abs(parent)
	if err != nil {
		return false, err
	}
	childAbsolute, err := filepath.Abs(child)
	if err != nil {
		return false, err
	}
	relative, err := filepath.Rel(parentAbsolute, childAbsolute)
	if err != nil {
		return false, err
	}
	return relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)), nil
}

func allocatedKB(path string) int64 {
	output, err := exec.Command("du", "-sk", path).Output()
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(output))
	if len(fields) == 0 {
		return 0
	}
	value, _ := strconv.ParseInt(fields[0], 10, 64)
	return value
}

func processLooksLikeSandbox(pid int) bool {
	payload, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "cmdline"))
	if err != nil {
		return false
	}
	text := string(payload)
	return strings.Contains(text, "bwrap") || strings.Contains(text, "setpriv") || strings.Contains(text, "sandboxd")
}

func readLogTail(path string, limit int64) string {
	file, err := os.Open(path)
	if err != nil {
		return "unavailable: " + err.Error()
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return "unavailable: " + err.Error()
	}
	start := info.Size() - limit
	if start < 0 {
		start = 0
	}
	if _, err := file.Seek(start, io.SeekStart); err != nil {
		return "unavailable: " + err.Error()
	}
	payload, err := io.ReadAll(io.LimitReader(file, limit))
	if err != nil {
		return "unavailable: " + err.Error()
	}
	return strings.TrimSpace(string(payload))
}
