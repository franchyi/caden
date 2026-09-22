"""Reset the test files to base (discarding any agent edits to tests), apply the gold test_patch,
then run FAIL_TO_PASS / PASS_TO_PASS. Resetting first is the SWE-bench protocol: the agent edits
source only, and the gold tests must be pristine — otherwise an agent that touches a test file makes
`git apply` fail and disqualifies its own grading."""

import re
import subprocess
from pathlib import Path

from agent_pipeline.scoring import is_resolved, run_tests


def evaluate(task: dict) -> dict:
    repo = task["repo_dir"]
    venv_python = task["venv_python"]
    test_patch = task["test_patch"]

    # Reset every file the gold test_patch touches to the base commit (drop the agent's version of a
    # file the patch creates), so the patch applies against pristine tests.
    for p in re.findall(r"^\+\+\+ b/(\S+)", test_patch, re.M):
        r = subprocess.run(["git", "checkout", task["base_commit"], "--", p], cwd=repo,
                           capture_output=True, text=True)
        if r.returncode != 0:
            (Path(repo) / p).unlink(missing_ok=True)

    patch_file = Path(repo) / ".gold_test.patch"
    patch_file.write_text(test_patch)
    ap = subprocess.run(["git", "apply", str(patch_file)], cwd=repo,
                        capture_output=True, text=True)
    if ap.returncode != 0:
        return {"fail_to_pass": {}, "pass_to_pass": {}, "resolved": False,
                "error": f"git apply test_patch failed: {ap.stderr[:300]}"}

    f2p = run_tests(repo, venv_python, task["fail_to_pass"])
    p2p = run_tests(repo, venv_python, task["pass_to_pass"])
    return {"fail_to_pass": f2p, "pass_to_pass": p2p, "resolved": is_resolved(f2p, p2p)}
