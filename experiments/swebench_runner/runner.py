"""
Runner class that orchestrates a SWE-Bench benchmark run.

Delegates to:
  - ImageBuilder: preparing the container image
  - Container: container lifecycle
  - ResourceMonitor: sampling CPU/memory during the run
  - TraceCollector: reading Claude Code traces
"""

import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from experiments.swebench_runner.bench_types import (
    ResourceData,
    SWEBenchResults,
    SWEBenchRunResult,
)
from experiments.swebench_runner.image_builder import ImageBuilder
from experiments.swebench_runner.podman import (
    Container,
    ContainerOptions,
    get_image_info,
    pull_image,
)
from experiments.swebench_runner.trace_collector import TraceCollector


WORKFLOW_PROMPT = """Fix this issue: $(cat /issue.md)

IMPORTANT: You must complete the FULL workflow:
1. Read and understand the issue thoroughly
2. Explore the codebase to find relevant files
3. Implement the fix
4. Run the test suite to verify your fix
5. If ANY test fails, analyze the error and fix it
6. Repeat steps 4-5 until ALL tests pass
7. Only stop when tests are passing

DO NOT stop until you have:
- Made code changes that fix the issue
- Run the tests and confirmed they pass
- Shown the final git diff

If you encounter test failures, debug and fix them. Keep trying until successful.

CRITICAL REQUIREMENTS FOR TESTING:
- You MUST run the project's ORIGINAL test suite (pytest, unittest, tox, etc.)
- Do NOT write custom test scripts or verification scripts to bypass tests
- Do NOT claim success based on your own "All checks passed" output
- The test output MUST show real pytest format: "X passed, Y failed in Z seconds"
- If tests fail with ImportError or collection errors, fix the environment/import issue first
- Success means the project's actual test suite passes, not custom verification

WHAT COUNTS AS SUCCESS:
- Real pytest/unittest output showing tests passed
- Example: "===== 150 passed, 0 failed in 10.5s ====="

WHAT DOES NOT COUNT:
- Your own verification scripts saying "All checks passed"
- Manual testing or print statements
- Skipping tests due to import errors

In the output, you need to summary your change and
summary how your test the application to check the fix,
and what's the test status.
"""

_MARKER_GIT_DIFF = "=== GIT DIFF ==="
_MARKER_DISK_USAGE = "=== DISK USAGE ==="
_MARKER_TOOL_LOG = "=== TOOL CALL LOG ==="


class SWEBenchRunner:
    """
    Runs a single SWE-Bench instance inside a Podman container.

    Parameters
    ----------
    image_name:
        Docker Hub image, e.g. ``"swebench/django:1234"``.
    memory_limit:
        Podman ``--memory`` value, e.g. ``"4g"``.
    cpu_limit:
        Podman ``--cpus`` value, e.g. ``"2"``.
    output_dir:
        Directory where results, traces, and logs are written.
    replay_session:
        The ``leafUuid`` token used to authenticate with the mock server.
    """

    def __init__(
        self,
        image_name: str,
        cgroup_parent: str,
        memory_limit: str | None = "4g",
        cpu_limit: str | None = "2",
        output_dir: str | Path = None,
        replay_session: str | None = None,
    ):
        if output_dir is None:
            raise ValueError("output_dir must be specified")
        if not replay_session:
            raise ValueError(
                "replay_session (leafUuid) must be set to run SWEBenchRunner"
            )

        self.image_name = image_name
        self.cgroup_parent = cgroup_parent
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.output_dir = Path(output_dir)
        self.replay_session = replay_session
        self.home = os.environ.get("HOME", f"/home/{os.environ.get('USER', 'user')}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def prepare(self, model: str = "haiku") -> str:
        self.start_time = time.time()
        self.model = model
        self.results = SWEBenchResults(
            image=self.image_name,
            start_time=datetime.now().isoformat(),
            memory_limit=self.memory_limit,
            cpu_limit=self.cpu_limit,
            model=model,
            model_requested=model,
        )

        self.tmp_dir_obj = tempfile.TemporaryDirectory(prefix="swebench_claude_")
        self.tmp_claude = Path(self.tmp_dir_obj.name)

        src_claude = Path(self.home) / ".claude"
        if src_claude.exists():
            self.tmp_claude.mkdir(parents=True, exist_ok=True)
            settings_file = src_claude / "settings.json"
            if settings_file.exists():
                shutil.copy2(settings_file, self.tmp_claude / "settings.json")

        print(f"[1/7] Pulling image: {self.image_name}")
        pull_image(self.image_name)
        self.results.pull_time = time.time() - self.start_time

        print("[2/7] Preparing custom image (permissions/pip/pytest)...")
        step_start = time.time()
        builder = ImageBuilder(self.image_name)
        self.fixed_image = builder.get_or_build()
        self.results.permission_fix_time = time.time() - step_start

        print("[3/7] Collecting image and disk info...")
        self.results.image_info = get_image_info(self.fixed_image)
        size = (
            self.results.image_info.get("size_mb", "N/A")
            if self.results.image_info
            else "N/A"
        )
        print(f"  Image size: {size} MB")

        print("[4/7] Preparing output directory...")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.results.output_dir = str(self.output_dir)

        return self.fixed_image

    def run_container(
        self,
        prompt: str | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> None:
        try:
            print(
                f"[5/7] Running Claude Code ({self.model}) with resource monitoring..."
            )
            step_start = time.time()
            claude_result = self._run_claude(
                self.fixed_image,
                prompt or WORKFLOW_PROMPT,
                self.model,
                env_vars,
                self.tmp_claude,
            )
            self.results.claude_time = time.time() - step_start
            self.results.model_actual = self.model
            self.results.model = self.model
            self.results.claude_output = claude_result
        except Exception as exc:
            self.results.error = str(exc)
            print(f"Error in container run for image: {self.image_name}: {exc}")

    def get_results(self) -> SWEBenchResults:
        try:
            if not self.results.error and self.results.claude_output:
                print("[6/7] Parsing disk usage...")
                self.results.disk_usage = _parse_disk_usage(
                    self.results.claude_output.stdout
                )
                print(f"  Disk usage (/testbed): {self.results.disk_usage or 'N/A'} MB")

                print("[7/7] Collecting trace logs...")
                trace_dir = self.tmp_claude / "projects" / "-testbed"
                self.results.traces = TraceCollector(
                    trace_dir, self.output_dir
                ).collect()
        except Exception as exc:
            if not self.results.error:
                self.results.error = str(exc)
            print(f"Error in results collection: {exc}")
        finally:
            if hasattr(self, "tmp_dir_obj"):
                self.tmp_dir_obj.cleanup()

        self.results.total_time = time.time() - self.start_time
        self.results.end_time = datetime.now().isoformat()
        self._save_results(self.results)
        return self.results

    # ------------------------------------------------------------------
    # Container execution
    # ------------------------------------------------------------------

    def _run_claude(
        self,
        image: str,
        prompt: str,
        model: str,
        env_vars: dict[str, str] | None,
        tmp_claude: Path,
    ) -> SWEBenchRunResult:
        """Start a container, run Claude Code, and collect outputs."""

        script = self._build_script(prompt, model)
        opts = self._container_options(
            image,
            script,
            env_vars,
            tmp_claude,
        )

        with Container(opts) as container:
            self.container_id = container.container_id
            exit_code = container.wait()

            stdout, stderr = container.logs()

        # Persist raw output
        self._write_text(self.output_dir / "claude_output.txt", stdout)
        if stderr:
            self._write_text(self.output_dir / "claude_stderr.txt", stderr)

        run_result = SWEBenchRunResult(
            stdout=stdout, stderr=stderr, exit_code=exit_code
        )
        return run_result

    def _container_options(
        self,
        image: str,
        script: str,
        env_vars: dict[str, str] | None,
        tmp_claude: Path,
    ) -> ContainerOptions:
        """Assemble the full set of container run options."""

        # The host's /usr, /lib, /lib64 are mounted read-only to
        # non-overlapping paths so the container keeps its own Python
        # while gaining access to the host's Node.js binary.
        # /home is mounted read-only to allow access to the host's Claude binary
        # while a temporary read-write directory is mounted over ~/.claude to
        # collect traces and maintain idempotency.
        volumes = [
            "/usr:/host_usr:ro",
            "/lib:/host_lib:ro",
            "/lib64:/host_lib64:ro",
            "/home:/home:ro",
        ]
        if not self.home.startswith("/home/"):
            volumes.append(f"{self.home}:{self.home}:ro")

        volumes.append(f"{tmp_claude}:{self.home}/.claude:rw")

        env = {
            "HOME": self.home,
            "SHELL": "/bin/bash",
            "PYTHONUSERBASE": "/testbed/.local",
            "XDG_CACHE_HOME": "/testbed/.cache",
        }
        if env_vars:
            env.update(env_vars)

        return ContainerOptions(
            image=image,
            command=["bash", "-c", script],
            volumes=volumes,
            env=env,
            workdir="/testbed",
            memory=self.memory_limit,
            cpus=self.cpu_limit,
            network="host",
            userns="keep-id",
            cgroup_parent=self.cgroup_parent,
        )

    def _build_script(self, prompt: str, model: str) -> str:
        mock_server_url = "http://localhost:8000"

        return f"""\
git config user.email "test@test.com"
git config user.name "Test"
git config --add safe.directory /testbed

# --- Node/glibc isolation wrapper ---
# The host's Node.js is dynamically linked against the host's glibc,
# which may be newer than the container's glibc.  We mount the host's
# /usr, /lib, /lib64 to non-overlapping paths and create a tiny wrapper
# that invokes Node through the host's own dynamic linker so it gets a
# self-contained host runtime without disturbing the container's glibc.
mkdir -p /tmp/host_bin
cat > /tmp/host_bin/node << 'WRAPPER'
#!/bin/bash
exec /host_lib64/ld-linux-x86-64.so.2 \\
    --library-path /host_usr/lib/x86_64-linux-gnu \\
    /host_usr/bin/node "$@"
WRAPPER
chmod +x /tmp/host_bin/node
export PATH="/tmp/host_bin:$PYTHONUSERBASE/bin:$HOME/.local/bin:$PATH"

if [ -x "$HOME/.local/bin/claude" ]; then
    CLAUDE_BIN="$HOME/.local/bin/claude"
elif [ -x "/host_usr/local/bin/claude" ]; then
    CLAUDE_BIN="/host_usr/local/bin/claude"
else
    CLAUDE_BIN="$(command -v claude)"
fi
echo "[Runner] Claude binary: $CLAUDE_BIN"
echo "[Runner] Node wrapper: $(which node)"
echo "[Runner] Node version: $(node --version 2>&1)"

export ANTHROPIC_BASE_URL="{mock_server_url}"
export ANTHROPIC_AUTH_TOKEN="{self.replay_session}"
export CLAUDE_CODE_ENABLE_TASKS=0
"$CLAUDE_BIN" --model {model} --print --dangerously-skip-permissions "{prompt}"

echo "{_MARKER_GIT_DIFF}"
git diff

echo "{_MARKER_DISK_USAGE}"
du -sm /testbed 2>/dev/null || echo "N/A"

echo "{_MARKER_TOOL_LOG}"
cat /tmp/agentcg_tools.jsonl 2>/dev/null || echo "No tool call log"
"""

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def _save_results(self, results: SWEBenchResults) -> None:
        if not self.output_dir:
            return
        path = self.output_dir / "results.json"
        self._write_json(path, asdict(results))
        print(f"\nResults saved to: {path}")

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        with open(path, "w") as f:
            f.write(text)




def _parse_disk_usage(stdout: str) -> str | None:
    """
    Extract the ``du -sm /testbed`` value from container stdout.

    Returns the size in MB as a string, or ``None`` if not found.
    """
    if _MARKER_DISK_USAGE not in stdout:
        return None
    try:
        after_marker = stdout.split(_MARKER_DISK_USAGE, 1)[1]
        first_line = after_marker.strip().split("\n", 1)[0].strip()
        if first_line == "N/A":
            return None
        size = first_line.split()[0]
        return size if size.isdigit() else None
    except (IndexError, ValueError):
        return None
