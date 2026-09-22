"""Per-command bubblewrap sandbox (0.4.0-safe flags): the repo is bind-mounted at its real
path and --chdir'd into so the prebuilt venv's absolute paths resolve. Network is left on
(no --unshare-net) for v1."""

import os
import subprocess

# 0.4.0-safe flags only. No --overlay/--bind-fd/--disable-userns (those are newer).
# System dirs are bound only if present on this host (/lib64 is absent on aarch64, and bwrap
# hard-fails on a missing --ro-bind source).
_RO_PATHS = ("/usr", "/bin", "/lib", "/lib64", "/etc")
BASE_ARGS = [
    "--unshare-user-try",
    "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    "--die-with-parent",  # no orphaned sandboxes if the runner is killed (SSH drop, OOM, ^C)
    *[a for p in _RO_PATHS if os.path.exists(p) for a in ("--ro-bind", p, p)],
    "--tmpfs", "/tmp",
    "--proc", "/proc",
    "--dev", "/dev",
    "--new-session",
    "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
]


def build_bwrap_argv(repo: str, command: str, *, bwrap: str = "bwrap",
                     env: dict | None = None) -> list[str]:
    argv = [bwrap, *BASE_ARGS, "--bind", repo, repo, "--chdir", repo]
    for k, v in (env or {}).items():
        argv += ["--setenv", k, v]
    argv += ["bash", "-c", command]
    return argv


class BubblewrapEnvironment:
    def __init__(self, repo: str, *, bwrap: str = "bwrap", timeout: int = 600,
                 env: dict | None = None):
        self.repo = repo
        self.bwrap = bwrap
        self.timeout = timeout
        self.env = env or {}

    def execute(self, command: str) -> dict:
        argv = build_bwrap_argv(self.repo, command, bwrap=self.bwrap, env=self.env)
        try:
            r = subprocess.run(
                argv, text=True, timeout=self.timeout,
                stdin=subprocess.DEVNULL,  # interactive commands read EOF instead of blocking
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace",
            )
            return {"output": r.stdout, "returncode": r.returncode}
        except subprocess.TimeoutExpired as e:
            out = e.output or ""
            out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
            return {"output": out + f"\n[timeout after {self.timeout}s]", "returncode": -1}
        except OSError as e:  # bwrap missing, repo path gone, E2BIG from a >128KB command, ...
            return {"output": f"[sandbox error: {e}]", "returncode": -1}
