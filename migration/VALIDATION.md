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
