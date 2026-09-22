# Caden naming migration — September 21, 2026

The scheduler formerly named ORCA is now **Caden**. The umbrella system remains
Crate; SandboxFS still owns workspace construction and sandbox lifecycle.
This is a local naming migration, not a new experiment or a performance change.

## Current locations and interfaces

| Item | Current identity |
|---|---|
| Main engineering repository | `caden/`, branch `crate-mlsys` |
| Committed base | `5d1acbd640cfea4221ed09cdab0cf1861b69e367`; existing development and this rename remain uncommitted |
| Python package | `caden` |
| Scheduler protocol/config/implementation | `Caden`, `CadenPolicyConfig`, `StageAwareCaden` |
| Current policy CLI spelling | `--policy caden` |
| Linked reference worktree | `caden-swebench-verified/`, detached `f95df88e89ac117c9ba407a6320374b7012658ce` |
| Workload directory | `caden-workloads/` |
| SandboxFS submodule | `third_party/sandboxfs`, still `652aa279bbb2afb4068d4b838e2df8e103b247fe` |
| PM report | `tech_report/caden-sandboxfs-report.docx`, five pages |

The repository/worktree Git connections and submodule gitdirs were repaired
after moving directories. Branches, commits, remotes and Git history were not
renamed. Use the main engineering checkout, not the detached reference, for
further work. Existing dirty files and untracked development were preserved.

## Preserved evidence and compatibility

Raw traces, results, frozen source trees/diffs, capture logs, checksum manifests,
downloaded reference papers and historical receipts remain unchanged. Original
`orca` identifiers and paths within those packages are intentional. No checksum
was regenerated to conceal a rename. An audit checked **24,879 files** against
their pre-migration SHA256 values with zero mismatches.

New code emits Caden schema names. Readers explicitly accept the corresponding
historical ORCA v1 schemas (and the retained speculative-restore v6 schema).
Historical `--policy orca` input is normalized to `caden`; existing result
summarizers accept either label. Unknown versions and invalid hashes are still
rejected. Historical provenance retains the field names of its measured source.
The Python import namespace itself is `caden`, not an alias package named `orca`.

Remote nsl17/nsl18 directories, PIDs, services, mounts and DAX contents were not
modified. Use frozen remote source paths for reproducing earlier campaigns;
deploy the renamed source explicitly for a future run. Git administrative names
may still contain the old worktree identifier. These are not public project names.

## Verification and durable guidance

- Main local suite: **478 passed, 22 skipped, 7 subtests passed**; skips include
  Linux/hardware-only paths, which were not rerun remotely for this rename.
- Detached reference suite: **313 passed**.
- Python 3.13 package imports and both experiment runner help commands pass.
- Native cold-store `make check` passes using file/socketpair emulation only;
  no kernel swap, Device-DAX or remote host was involved.
- Paper checks verify 32 captures, 32 normalized traces, 282 commands per
  configuration and 334 artifact hashes; derived outputs still match.
- Figure-data, reader-language and Chinese-logic checks pass.
- The Crate and CXLGen PDF previews are rebuilt from their current working
  sources (10 and 5 pages); both `make check` targets pass. A XeTeX-only
  font-encoding guard preserves the MLSys Times setup under Tectonic, and the
  lifecycle sequence is kept within one column. pdfLaTeX font behavior and
  measurement content are unchanged.
- Five-page report: all pages visually inspected, five links resolve, four
  comments remain anchored, zero accessibility findings. Evaluation data and
  the evaluation plot were not changed by the architecture redesign.
- Workspace `AGENTS.md`, working documentation and the scoped Codex ad-hoc
  memory note now record Caden as the canonical name. Historical chat/session
  and raw memory records were not rewritten.

Migration backups, pre-edit diffs, the change inventory and verification receipt
are in workspace `tmp/caden-rename-20260921/`. These are audit records and retain
old names where needed. No Git commit or push was performed.
