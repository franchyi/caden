"""Gold-test resolution: resolved = every FAIL_TO_PASS passes AND every PASS_TO_PASS still
passes. Each test id is run individually (rc==0 => pass) to avoid parsing pytest output."""

import subprocess


def is_resolved(fail_to_pass: dict[str, int], pass_to_pass: dict[str, int]) -> bool:
    """Resolved iff every listed test returned exit code 0."""
    return all(rc == 0 for rc in fail_to_pass.values()) and \
        all(rc == 0 for rc in pass_to_pass.values())


def run_tests(repo: str, venv_python: str, test_ids: list[str],
              timeout: int = 600) -> dict[str, int]:
    """Run each pytest node id individually; return {test_id: returncode}."""
    rcs = {}
    for tid in test_ids:
        r = subprocess.run(
            [venv_python, "-m", "pytest", "-x", "-q", tid],
            cwd=repo, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        rcs[tid] = r.returncode
    return rcs
