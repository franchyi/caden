PYTHON ?= python3
GO ?= go
SANDBOXFS_COMMANDS := sandboxd sandboxfsd sandboxfsctl sandboxfsbench sandboxfscorpus

.PHONY: test test-core test-agent native-check verify-vendor sandboxfs-build sandboxfs-test build source-dist

build: sandboxfs-build
	$(MAKE) -C native/cxl_coldstore all

verify-vendor:
	$(PYTHON) scripts/verify_vendor.py

sandboxfs-build: verify-vendor
	@mkdir -p third_party/sandboxfs/bin
	@set -e; for command in $(SANDBOXFS_COMMANDS); do \
		cd "$(CURDIR)/third_party/sandboxfs"; \
		$(GO) build -trimpath -o bin/$$command ./cmd/$$command; \
	done

sandboxfs-test: verify-vendor
	cd third_party/sandboxfs && $(GO) test ./...

# An all-source delivery, including vendored SandboxFS and tests, with no Git
# metadata or external submodule fetch. Only a clean committed tree is accepted.
source-dist:
	$(PYTHON) scripts/export_source.py --archive dist/caden-source.tar.gz

test: test-core test-agent

test-core:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) -B -m pytest -q -p no:cacheprovider tests

test-agent:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=experiments/agent_pipeline $(PYTHON) -B -m pytest -q -p no:cacheprovider experiments/agent_pipeline/tests

native-check:
	$(MAKE) -C native/cxl_coldstore check
