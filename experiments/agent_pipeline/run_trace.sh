#!/usr/bin/env bash
# Launch trace_runner under cgroup v2: create caden_run (the agent's cgroup, sampled) and caden_llm
# (sibling cgroup the claude client is moved into so it's excluded from the measurement), place the
# runner in caden_run, and point the model adapter at caden_llm. Runs as root to manage cgroups.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
INSTANCE=${1:?usage: run_trace.sh <instance_id> [sonnet|opus]}
MODEL=${2:-sonnet}
STEP_LIMIT=${STEP_LIMIT:-40}
VENV_PYTHON_BIN=${VENV_PYTHON_BIN:-/usr/bin/python3}
CG=/sys/fs/cgroup; RUN=$CG/caden_run; LLM=$CG/caden_llm

cleanup() { sudo rmdir "$RUN" "$LLM" 2>/dev/null || true; }
trap cleanup EXIT

sudo bash -c "
  for c in cpu memory; do grep -qw \$c $CG/cgroup.subtree_control || echo +\$c > $CG/cgroup.subtree_control 2>/dev/null || true; done
  mkdir -p $RUN $LLM
  [ -e $RUN/memory.current ] || { echo 'FATAL: no memory controller in caden_run'; exit 1; }
  [ -e $RUN/cpu.stat ] || echo 'WARN: no cpu.stat in caden_run (cpu% will read 0)'
  echo \$\$ > $RUN/cgroup.procs
  cd $HERE
  exec env AGENT_CGROUP=$RUN LLM_CGROUP=$LLM AGENT_MODEL=$MODEL STEP_LIMIT=$STEP_LIMIT \
       VENV_PYTHON_BIN=$VENV_PYTHON_BIN HOME=/root \
       PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin \
       uv run --python 3.11 python trace_runner.py $INSTANCE $MODEL
"
