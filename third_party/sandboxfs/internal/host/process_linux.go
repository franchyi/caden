//go:build linux

package host

import (
	"fmt"
	"os"
	"os/exec"
	"syscall"
)

// configureProcessCgroup places the initial child in its cgroup atomically at
// clone time. Moving only the Bubblewrap monitor after Start can race creation
// of sandboxd, leaving a descendant in the daemon cgroup.
func configureProcessCgroup(command *exec.Cmd, cgroupPath string) (func(), error) {
	attributes := &syscall.SysProcAttr{Setpgid: true}
	if cgroupPath == "" {
		command.SysProcAttr = attributes
		return func() {}, nil
	}
	directory, err := os.Open(cgroupPath)
	if err != nil {
		return nil, fmt.Errorf("open sandbox cgroup %s: %w", cgroupPath, err)
	}
	attributes.UseCgroupFD = true
	attributes.CgroupFD = int(directory.Fd())
	command.SysProcAttr = attributes
	return func() { _ = directory.Close() }, nil
}
