import sys
import time

from agent_pipeline import prompts
from agent_pipeline.parsing import FormatError, parse_action


class Agent:
    """Backend-agnostic loop: model.query(messages, budget=wall_seconds_left) returns prose +
    bash block(s), env.execute runs them, repeat until submit. Records LLM_WAIT (around
    model.query) and TOOL_BURST (around env.execute) stage timings."""

    def __init__(self, model, env, *, step_limit: int = 30, wall_limit: float = 1200.0,
                 clock=time.monotonic):
        self.model = model
        self.env = env
        self.step_limit = step_limit
        self.wall_limit = wall_limit  # hard cap on whole-run wall time; bounds slow/stuck tasks
        self.clock = clock
        self.messages: list[dict] = []
        self.stage_events: list[dict] = []

    def _mark(self, stage, start, step):
        self.stage_events.append(
            {"stage": stage, "start": start, "end": self.clock(), "step": step}
        )

    def run(self, task: str) -> dict:
        self.messages = [
            {"role": "system", "content": prompts.SYSTEM},
            {"role": "user", "content": prompts.render_instance(task)},
        ]
        t_start = self.clock()
        for step in range(self.step_limit):
            if self.clock() - t_start > self.wall_limit:
                print(f"[wall-clock limit {self.wall_limit:.0f}s exceeded at step {step}]",
                      file=sys.stderr, flush=True)
                return self._result("wall_timeout", step)
            t0 = self.clock()
            try:
                # budget = wall seconds left, so a retry-heavy turn can't blow far past the cap
                reply = self.model.query(self.messages,
                                         budget=self.wall_limit - (t0 - t_start))  # LLM_WAIT
            except Exception as e:  # model backend failed after its own retries → end gracefully
                self._mark("LLM_WAIT", t0, step)
                if self.clock() - t_start > self.wall_limit:  # it failed running out the wall cap
                    print(f"[wall-clock limit {self.wall_limit:.0f}s exceeded at step {step}]",
                          file=sys.stderr, flush=True)
                    return self._result("wall_timeout", step)
                print(f"[step {step}] model error: {e}", file=sys.stderr, flush=True)
                return self._result("model_error", step)
            self._mark("LLM_WAIT", t0, step)
            self.messages.append({"role": "assistant", "content": reply})

            try:
                command = parse_action(reply)
            except FormatError as e:
                print(f"[step {step}] format error; reprompting", file=sys.stderr, flush=True)
                self.messages.append({"role": "user",
                                      "content": f"{prompts.FORMAT_ERROR} ({e})"})
                continue

            print(f"[step {step}] $ {command.splitlines()[0][:120]}", file=sys.stderr, flush=True)
            t1 = self.clock()
            result = self.env.execute(command)  # TOOL_BURST
            self._mark("TOOL_BURST", t1, step)

            # Record the observation BEFORE the submit check so the final tool output is always in
            # the transcript (the exported trace keeps it). Skipping it on submit dropped the last,
            # richest observation — e.g. a `pytest && echo SENTINEL` turn's test output.
            self.messages.append(
                {"role": "user", "content": prompts.render_observation(result)}
            )
            # Submit if the sentinel was echoed on ANY output line — models often run it as the
            # tail of a final block (`pytest -q && echo SENTINEL`), not alone as instructed.
            if command.strip() == f"echo {prompts.SUBMIT_SENTINEL}" or any(
                ln.strip() == prompts.SUBMIT_SENTINEL
                for ln in result.get("output", "").splitlines()
            ):
                return self._result("submitted", step)
        return self._result("step_limit", self.step_limit)

    def _result(self, exit_status, steps) -> dict:
        return {
            "exit_status": exit_status,
            "steps": steps,
            "stage_events": self.stage_events,
            "messages": self.messages,
        }
