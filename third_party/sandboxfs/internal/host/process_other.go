//go:build !linux

package host

import (
	"os/exec"
	"syscall"
)

func configureProcessCgroup(command *exec.Cmd, _ string) (func(), error) {
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	return func() {}, nil
}
