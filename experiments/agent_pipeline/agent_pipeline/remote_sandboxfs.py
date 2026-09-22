"""Execute mini-agent shell turns in one persistent remote SandboxFS sandbox."""

from __future__ import annotations

import json
import shlex
import subprocess


def build_remote_exec_command(
    *,
    ctl: str,
    socket: str,
    sandbox_id: str,
    command: str,
    workspace: str = "/workspace/repository",
) -> str:
    wrapped = f"cd {shlex.quote(workspace)} && {command}"
    return shlex.join(
        [
            ctl,
            "--socket",
            socket,
            "exec-json",
            sandbox_id,
            "--",
            "/bin/bash",
            "-lc",
            wrapped,
        ]
    )


class RemoteSandboxFSEnvironment:
    def __init__(
        self,
        *,
        host: str,
        ctl: str,
        socket: str,
        sandbox_id: str,
        workspace: str = "/workspace/repository",
        timeout: int = 180,
    ):
        self.host = host
        self.ctl = ctl
        self.socket = socket
        self.sandbox_id = sandbox_id
        self.workspace = workspace
        self.timeout = timeout

    def execute(self, command: str) -> dict:
        remote = build_remote_exec_command(
            ctl=self.ctl,
            socket=self.socket,
            sandbox_id=self.sandbox_id,
            command=command,
            workspace=self.workspace,
        )
        try:
            completed = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", self.host, remote],
                check=False,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", "replace")
            return {
                "output": output + f"\n[timeout after {self.timeout}s]",
                "returncode": -1,
            }
        except OSError as error:
            return {"output": f"[remote sandbox error: {error}]", "returncode": -1}

        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError:
            response = None
        if isinstance(response, dict) and "exit_code" in response:
            stdout = str(response.get("stdout", ""))
            stderr = str(response.get("stderr", ""))
            return {
                "output": stdout + stderr,
                "returncode": int(response["exit_code"]),
            }
        return {
            "output": (completed.stdout + completed.stderr).strip(),
            "returncode": completed.returncode if completed.returncode != 0 else -1,
        }
