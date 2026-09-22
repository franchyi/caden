# Caden Documentation

Active plan: `2026-05-29-plan.md`.
SWE-rebench trace evidence: `eval/local-trace-stage-report.md`.
Analyzer: `analysis/analyze_local_trace_stages.py`.
System design (discussion draft): `scheduling-system-design.md`.
Sandbox cold-start filesystem design: `overlayfs-coldstart-design.md`.

Caden is now framed as stage-aware host scheduling for dense AI-agent sandboxes:
LLM-waiting sandboxes are cold/resumable tenants, while response wakeups and
local tool bursts receive bounded CPU and memory priority.
