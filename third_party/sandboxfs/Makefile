SHELL := /usr/bin/env bash

GO ?= go
BINDIR := bin
COMMANDS := sandboxd sandboxfsd sandboxfsctl sandboxfsbench sandboxfscorpus

.PHONY: all build check test install clean smoke integration

all: check build

build:
	@mkdir -p $(BINDIR)
	@for command in $(COMMANDS); do \
		$(GO) build -trimpath -o $(BINDIR)/$$command ./cmd/$$command; \
	done

check:
	@bash -n scripts/*.sh
	@python3 -m py_compile scripts/*.py
	@test -z "$$(gofmt -l cmd internal)" || \
		{ echo "Go files require gofmt" >&2; gofmt -l cmd internal >&2; exit 1; }
	@$(GO) vet ./...

test: check
	@$(GO) test ./...

install: build
	@install -d -m 0755 /usr/local/libexec/sandboxfs
	@install -m 0755 $(BINDIR)/sandboxd /usr/local/libexec/sandboxfs/sandboxd
	@install -m 0755 $(BINDIR)/sandboxfsd /usr/local/bin/sandboxfsd
	@install -m 0755 $(BINDIR)/sandboxfsctl /usr/local/bin/sandboxfsctl
	@install -m 0755 $(BINDIR)/sandboxfsbench /usr/local/bin/sandboxfsbench
	@install -m 0755 $(BINDIR)/sandboxfscorpus /usr/local/bin/sandboxfscorpus
	@install -m 0644 deploy/sandboxfsd.service /etc/systemd/system/sandboxfsd.service

clean:
	@rm -rf -- $(BINDIR)

smoke: check
	sudo -u ubuntu ./scripts/smoke-test.sh /agent-xfs-t1

integration: test
	sudo ./scripts/integration-test.sh
