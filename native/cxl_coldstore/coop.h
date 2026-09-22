#ifndef CRATE_COOP_H
#define CRATE_COOP_H
#include <stddef.h>

/* Cooperative tier-eligible memory for a sandbox process (Linux only).
 *
 * A region is a MAP_SHARED mapping of a private memfd. While its sandbox is
 * frozen, the trusted host pager may copy resident pages to the cold tier and
 * release them with a hole punch; it brings them back with UFFDIO_COPY before
 * thaw (eager) or on first touch (lazy). The owner never talks to the pager:
 * registration is the memfd's name, which the host discovers through /proc.
 *
 *   memfd name: crate-tier.u<uffd descriptor or -1>.k<0|1>
 *   k1 = the userfaultfd also resolves kernel-mode faults (read(2) into a cold
 *        buffer is safe); k0 = user-mode faults only, so lazy restore is unsafe
 *        unless the owner never passes region pointers to system calls.
 *
 * Owner obligations: one owning process per region (children of fork() share
 * the mapping, there is no copy-on-write); never write-seal or resize the
 * memfd; never munmap part of the region while it may be cold. */
typedef struct {
    void *addr;
    size_t length;
    int memfd;
    int uffd;          /* -1: eager pwrite restore only */
    int kernel_faults; /* 1: lazy restore is safe for system calls too */
} crate_coop_region;

/* Returns 0, or -1 with errno. length is rounded up to 4 KiB pages. */
int crate_coop_create(size_t length, crate_coop_region *out);
void crate_coop_destroy(crate_coop_region *region);
#endif
