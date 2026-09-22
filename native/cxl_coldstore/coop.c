#define _GNU_SOURCE
#include "coop.h"
#include <errno.h>
#include <string.h>

#ifndef __linux__
int crate_coop_create(size_t length, crate_coop_region *out) {
    (void)length; (void)out;
    errno = ENOTSUP;
    return -1;
}
void crate_coop_destroy(crate_coop_region *region) { (void)region; }
#else
#include <fcntl.h>
#include <linux/userfaultfd.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef UFFD_USER_MODE_ONLY
#define UFFD_USER_MODE_ONLY 1
#endif

static int handshake(int uffd) {
    struct uffdio_api api = {.api = UFFD_API, .features = 0};
    if (ioctl(uffd, UFFDIO_API, &api)) return -1;
    if (!(api.features & UFFD_FEATURE_MISSING_SHMEM)) { errno = EOPNOTSUPP; return -1; }
    return 0;
}
/* Prefer a descriptor that also resolves kernel-mode faults. Unprivileged
 * sandbox processes normally get only the user-mode variant. */
static int open_uffd(int *kernel_faults) {
    int uffd = -1;
#ifdef USERFAULTFD_IOC_NEW
    int device = open("/dev/userfaultfd", O_RDWR | O_CLOEXEC);
    if (device >= 0) {
        uffd = ioctl(device, USERFAULTFD_IOC_NEW, O_CLOEXEC | O_NONBLOCK);
        close(device);
    }
#endif
    if (uffd < 0) uffd = (int)syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK);
    *kernel_faults = uffd >= 0;
    if (uffd < 0) uffd = (int)syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK | UFFD_USER_MODE_ONLY);
    if (uffd >= 0 && handshake(uffd)) { close(uffd); uffd = -1; *kernel_faults = 0; }
    return uffd;
}

int crate_coop_create(size_t length, crate_coop_region *out) {
    if (!out || !length) { errno = EINVAL; return -1; }
    length = (length + 4095u) & ~(size_t)4095u;
    memset(out, 0, sizeof(*out));
    out->memfd = out->uffd = -1;
    const char *no_uffd = getenv("CRATE_COOP_NO_UFFD");
    if (!no_uffd || strcmp(no_uffd, "1")) out->uffd = open_uffd(&out->kernel_faults);
    char name[64];
    snprintf(name, sizeof(name), "crate-tier.u%d.k%d", out->uffd, out->kernel_faults);
    out->memfd = memfd_create(name, MFD_CLOEXEC);
    if (out->memfd < 0 || ftruncate(out->memfd, (off_t)length)) goto error;
    out->addr = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, out->memfd, 0);
    if (out->addr == MAP_FAILED) goto error;
    out->length = length;
    /* Test deployments without a privileged pager: the pager is not an ancestor
     * of this process, so Yama needs explicit consent for pidfd_getfd. */
    const char *tracer = getenv("CRATE_COOP_PTRACER_ANY");
    if (tracer && !strcmp(tracer, "1")) prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0);
    return 0;
error: {
    int saved = errno;
    out->addr = NULL;
    crate_coop_destroy(out);
    errno = saved;
    return -1;
}}

void crate_coop_destroy(crate_coop_region *region) {
    if (!region) return;
    if (region->addr && region->length) munmap(region->addr, region->length);
    if (region->memfd >= 0) close(region->memfd);
    if (region->uffd >= 0) close(region->uffd);
    memset(region, 0, sizeof(*region));
    region->memfd = region->uffd = -1;
}
#endif
