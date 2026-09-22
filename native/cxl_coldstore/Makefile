CC ?= cc
CPPFLAGS += -Ithird_party/lz4
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Werror -pthread
LDLIBS += -lz -pthread
LZ4_SOURCE = third_party/lz4/lz4.c
LZ4_DEPS = $(LZ4_SOURCE) third_party/lz4/lz4.h

# The pager moves real process pages (memfd hole punch, pidfd_getfd,
# userfaultfd) and is Linux only. Elsewhere it is not built and its tests
# report themselves as skipped, never as passed.
CRATE_PAGER_TARGETS :=
ifeq ($(shell uname -s),Linux)
CRATE_PAGER_TARGETS := build/crate_pagerd build/coop_holder
endif

.PHONY: all check
all: build/test_coldstore build/test_nbd_transport build/coldstore_probe $(CRATE_PAGER_TARGETS)
build:
	mkdir -p build
build/test_coldstore: coldstore.c coldstore.h test_coldstore.c $(LZ4_DEPS) | build
	$(CC) $(CPPFLAGS) $(CFLAGS) coldstore.c test_coldstore.c $(LZ4_SOURCE) -o $@ $(LDLIBS)
build/coldstore_probe: coldstore.c coldstore.h coldstore_probe.c $(LZ4_DEPS) | build
	$(CC) $(CPPFLAGS) $(CFLAGS) coldstore.c coldstore_probe.c $(LZ4_SOURCE) -o $@ $(LDLIBS)
build/test_nbd_transport: coldstore.c coldstore.h nbd_transport.c nbd_transport.h test_nbd_transport.c $(LZ4_DEPS) | build
	$(CC) $(CPPFLAGS) $(CFLAGS) coldstore.c nbd_transport.c test_nbd_transport.c $(LZ4_SOURCE) -o $@ $(LDLIBS)
build/crate_pagerd: pagerd.c coldstore.c coldstore.h $(LZ4_DEPS) | build
	$(CC) $(CPPFLAGS) $(CFLAGS) coldstore.c pagerd.c $(LZ4_SOURCE) -o $@ $(LDLIBS)
# Static when the toolchain allows it: the holder runs inside sandbox root
# filesystems whose C library is not the host's.
build/coop_holder: coop_holder.c coop.c coop.h | build
	$(CC) $(CFLAGS) -static coop.c coop_holder.c -o $@ 2>/dev/null || \
		$(CC) $(CFLAGS) coop.c coop_holder.c -o $@
check: all
	./build/test_coldstore
	./build/test_nbd_transport
	python3 test_probe.py
	python3 test_qualify.py
	python3 test_qualify_remote.py
