import shlex

from agent_pipeline.remote_sandboxfs import build_remote_exec_command


def test_remote_command_preserves_multiline_shell_as_one_argument():
    command = "python - <<'PY'\nprint(\"a b\")\nPY"
    remote = build_remote_exec_command(
        ctl="/opt/sandboxfsctl",
        socket="/run/test.sock",
        sandbox_id="sandbox-1",
        command=command,
    )
    argv = shlex.split(remote)
    assert argv[:6] == [
        "/opt/sandboxfsctl",
        "--socket",
        "/run/test.sock",
        "exec-json",
        "sandbox-1",
        "--",
    ]
    assert argv[-2] == "-lc"
    assert argv[-1].startswith("cd /workspace/repository && python")
    assert "print(\"a b\")" in argv[-1]
