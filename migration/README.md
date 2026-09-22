# Independent repository import

Destination: `https://github.com/franchyi/caden` (private), branch `crate-mlsys`.

The import root contains the current readable implementation files from the
source working tree, **including uncommitted changes**, not a checkout of only
the old committed branch tip. Source origin:

- Repository: `https://github.com/franchyi/orca.git`
- Branch: `crate-mlsys`
- Base commit: `5d1acbd640cfea4221ed09cdab0cf1861b69e367`
- SandboxFS submodule: `652aa279bbb2afb4068d4b838e2df8e103b247fe`

`import-manifest.json` records SHA-256 values for all 230 imported files as they
existed before the standalone README/test-entrypoint changes. Verify those
values against this repository's initial commit, not against later revisions.
Scheduler code, C/CXL code and test bodies were copied without modification.
The submodule is a pinned gitlink, not a copied implementation directory.

The source allowlist contains root project metadata, `src`, `tests`, `doc`,
`scripts`, `experiments` and `native`. Historical raw `results`, `traces`,
`maps`, archives, runtime environments, caches and build products were excluded.
No CXLGen branch or old Git object history was pushed. Small documentation
summaries remain as originally imported; they are not new performance results.

The original repository and its uncommitted changes remain intact. This
repository starts new independent history; it is neither a GitHub fork nor a
mirror that might expose unrelated branches. Use explicit commits/pushes on
this repository for subsequent synchronization; no background bidirectional
sync is configured.

The second commit supplies the new root README, experiment index, ignore rules,
local test dependencies, test Makefile, this provenance explanation and agent
instructions. Frozen measured-source packages are not renamed or substituted
with this newer development snapshot.
