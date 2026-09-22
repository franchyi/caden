from __future__ import annotations

from typing import Any

import pytest

from experiments.speculative_restore.run_latency import reset_holder


class FakeExecution:
    def __init__(self, stdout: str = "1 65536\n") -> None:
        self.stdout = stdout
        self.commands: list[tuple[str, ...]] = []

    def exec(self, sandbox: str, argv: tuple[str, ...]) -> dict[str, Any]:
        assert sandbox == "sandbox-1"
        self.commands.append(argv)
        return {"exit_code": 0, "stdout": self.stdout, "stderr": ""}


def test_reset_holder_uses_fifo_reset_command() -> None:
    execution = FakeExecution()

    elapsed, checksum = reset_holder(execution, "sandbox-1", "fifo")  # type: ignore[arg-type]

    assert elapsed >= 0
    assert checksum == 65536
    assert "printf 'reset" in execution.commands[0][2]


def test_reset_holder_uses_signal_for_poll_transport() -> None:
    execution = FakeExecution()

    _elapsed, checksum = reset_holder(execution, "sandbox-1", "poll")  # type: ignore[arg-type]

    assert checksum == 65536
    assert "kill -USR2" in execution.commands[0][2]


def test_reset_holder_rejects_invalid_receipt() -> None:
    execution = FakeExecution(stdout="missing-checksum\n")

    with pytest.raises(RuntimeError, match="invalid holder reset receipt"):
        reset_holder(execution, "sandbox-1", "fifo")  # type: ignore[arg-type]
