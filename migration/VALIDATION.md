# Import validation

Validated locally on macOS with Python 3.13.1 on 22 September 2026. This checks
the independent repository copy, not the original checkout or remote server.

| Check | Result |
| --- | --- |
| `make test-core PYTHON=.venv/bin/python` | 484 passed, 22 skipped; 7 subtests passed |
| `make test-agent PYTHON=.venv/bin/python` | 35 passed |
| Native cold-store C test | Passed, file emulation only |
| Native transport C test | Passed, socketpair/file emulation only |
| Native probe Python test | 1 passed |
| Native qualification Python tests | 15 passed |
| Native remote-qualification unit tests | 13 passed, mocked transport |
| SandboxFS submodule clone | Checked out exact pin `652aa279bbb2afb4068d4b838e2df8e103b247fe` |
| Original source preservation | All 230 selected source-file SHA-256 values unchanged |

The 22 skips are Linux-only pager and GNU cp/fadvise integration coverage.
No real Device-DAX test, host-wide swap operation or remote performance campaign
was launched. The native checks do not establish a CXL hardware result.

The independent import root is commit `3648644`. Its imported-file hashes are
in `import-manifest.json`; later documentation and test-entrypoint additions
are separate from that original file identity. A targeted scan found no
credential-token/private-key patterns in the exported files. This is a scoped
check, not a guarantee that every possible secret format can be detected.

The source checkout remains on `crate-mlsys` at
`5d1acbd640cfea4221ed09cdab0cf1861b69e367`, with its prior uncommitted changes
preserved. Bulk raw evidence and unrelated branch histories were not pushed.

## Single repository delivery validation on 8 October 2026

Implementation revision: `d455d0a60848c8a6dc1d2a479505f1c15b44a178`.
The updated report pins this revision rather than claiming the September
measurements came from the new packaging. Validation ran on macOS arm64 with
Python 3.13.1 and Go 1.26.2; Linux amd64 binaries were also cross-built.

| Check | Result |
| --- | --- |
| Core scheduler and harness suite | 491 passed, 22 platform skips, 7 subtests passed |
| Agent-pipeline suite | 35 passed |
| Vendored SandboxFS | All 43 selected files verified at upstream `652aa279bbb2afb4068d4b838e2df8e103b247fe` |
| SandboxFS Go build and unit tests | Passed; no dependency fetch or submodule initialization |
| Native store, transport and Python checks | Passed; file emulation/socketpair/mock evidence only |
| Scheduler wheel | Built, installed and imported with no runtime Python dependencies |
| Source export | Built and ran core tests from an export without Git metadata |
| New export provenance | Verified with Python 3.12; distinct Caden and SandboxFS commit identities |
| Linux sandboxd cross-build | ELF x86-64, statically linked with CGO disabled |
| Serving workload reconstruction | 32 sessions, 384 calls; source commands, waits and fingerprints preserved |
| Report commands | 16 Bash blocks syntax-checked; embedded Python parsed; runner options inspected |
| Report layout | 18 rendered pages inspected; original body, figures and measured results preserved |

No remote performance campaign, DAX access, global swap change, device format
or host-service installation occurred. The new private-namespace binary binding
has syntax and local argument validation but was not exercised in a privileged
Linux end-to-end campaign here. Platform skips remain untested, not passes.
Prepared SWE inputs and lab-host setup are external requirements, not contained
in the source archive. The pinned SandboxFS commit contains no license file;
resolve licensing before public redistribution. Raw historical evidence and
the archived Caden Git checkout remain unchanged.
