/* crate_pagerd: trusted host-side pager for cooperative sandbox mappings.
 *
 * Paging contract (see PAGER.md):
 *   - Eligible memory is a MAP_SHARED mapping of a memfd that a sandbox process
 *     created for this purpose, optionally with a userfaultfd for that process.
 *     Ordinary private anonymous memory, tmpfs files and file cache are NOT
 *     eligible; nothing here changes another process's mappings behind its back.
 *   - The owner must be quiescent for a demotion: its cgroup is confirmed frozen
 *     (or, in the explicitly unfenced test mode, every owner task is stopped).
 *   - Page-out = copy resident memfd pages into the cold store, then release the
 *     source DRAM with fallocate(PUNCH_HOLE). Released bytes are measured from
 *     the memfd's block count, never inferred from the store write.
 *   - Page-in = UFFDIO_COPY into the owner's address space (charged to the
 *     owner's cgroup), either eagerly before thaw or on demand from the fault
 *     thread. Without a userfaultfd only eager pwrite restore is possible.
 *   - Device-DAX stays in this process. Payloads move through bounded, optionally
 *     locked staging buffers and never enter the Python policy process.
 *   - The store index is ephemeral: SHUTDOWN is refused while any page is cold.
 *
 * No swapon/swapoff, no kernel NBD attachment, no DAX conversion, no ptrace
 * injection and no host-wide setting is touched. */
#define _GNU_SOURCE
#include "coldstore.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifndef __linux__
int main(void) {
    fprintf(stderr, "crate_pagerd requires Linux (memfd hole punching, pidfd_getfd, userfaultfd)\n");
    return 69; /* EX_UNAVAILABLE: reported as unsupported, never as a pass. */
}
#else
#include <linux/userfaultfd.h>
#include <poll.h>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/un.h>

#define PG_PAGE 4096u
#define PG_MAX_SANDBOXES 512
#define PG_MAX_REGIONS 2048
#define PG_MAX_EXTENTS 4096
#define PG_MAX_CONNECTIONS 64
#define PG_STAGING_BYTES (1u << 20)
#define PG_LINE 8192

typedef struct sandbox sandbox;
typedef struct {
    int used;
    uint32_t id;
    sandbox *owner;
    pid_t pid;
    int pidfd, memfd, uffd;
    int kernel_faults, registered, gone;
    uint64_t addr, length, file_offset, logical_base;
    uint64_t *cold; /* One bit per page: payload lives only in the store. */
    uint64_t cold_pages;
    size_t cold_words;
} region;

struct sandbox {
    pthread_mutex_t lock; /* Never copied/reset when a slot is reused. */
    int used, poisoned;
    char id[128];
    uint64_t incarnation;
    char cgroup[PATH_MAX];
    uint64_t max_bytes, reserved_bytes;
    int64_t last_generation;
    uint64_t requested, eligible, stored, released, restored_eager, restored_fault;
    uint64_t faults, demotions, restores, stale, errors;
    uint64_t copy_out_ns, punch_ns, copy_in_ns, fault_ns;
};

typedef struct { uint64_t base, length; } extent;

static struct {
    cs_store *store;
    cs_options options;
    const char *socket_path, *cgroup_root;
    int unfenced, lock_buffers, warned;
    _Atomic int stop;
    volatile sig_atomic_t signals;
    unsigned fault_around;
    uint64_t per_sandbox_max;
    int epoll_fd, listen_fd;
    pthread_mutex_t table; /* sandboxes, regions, extents, connection count */
    pthread_rwlock_t lifecycle; /* Shutdown excludes commands and fault service. */
    pthread_cond_t drained;
    int connection_fds[PG_MAX_CONNECTIONS];
    sandbox sandboxes[PG_MAX_SANDBOXES];
    region regions[PG_MAX_REGIONS];
    extent free_extents[PG_MAX_EXTENTS];
    size_t free_count;
    uint32_t next_region;
    unsigned connections;
    uint64_t bitmap_bytes, staging_bytes;
} G;

static uint64_t now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}
static void logf_(const char *format, ...) {
    va_list args;
    va_start(args, format);
    fprintf(stderr, "crate_pagerd: ");
    vfprintf(stderr, format, args);
    fputc('\n', stderr);
    va_end(args);
    fflush(stderr);
}

/* ---- logical extent allocator (first fit, coalescing; caller holds table) ---- */
static int extent_alloc(uint64_t length, uint64_t *base) {
    for (size_t n = 0; n < G.free_count; n++) {
        if (G.free_extents[n].length < length) continue;
        *base = G.free_extents[n].base;
        G.free_extents[n].base += length;
        G.free_extents[n].length -= length;
        if (!G.free_extents[n].length) {
            memmove(&G.free_extents[n], &G.free_extents[n + 1],
                    (G.free_count - n - 1) * sizeof(extent));
            G.free_count--;
        }
        return 0;
    }
    errno = ENOSPC;
    return -1;
}
static void extent_free(uint64_t base, uint64_t length) {
    size_t at = 0;
    while (at < G.free_count && G.free_extents[at].base < base) at++;
    if (at > 0 && G.free_extents[at - 1].base + G.free_extents[at - 1].length == base) {
        G.free_extents[at - 1].length += length;
        if (at < G.free_count &&
            G.free_extents[at - 1].base + G.free_extents[at - 1].length == G.free_extents[at].base) {
            G.free_extents[at - 1].length += G.free_extents[at].length;
            memmove(&G.free_extents[at], &G.free_extents[at + 1],
                    (G.free_count - at - 1) * sizeof(extent));
            G.free_count--;
        }
        return;
    }
    if (at < G.free_count && base + length == G.free_extents[at].base) {
        G.free_extents[at].base = base;
        G.free_extents[at].length += length;
        return;
    }
    if (G.free_count == PG_MAX_EXTENTS) { /* Leak logical space rather than corrupt. */
        logf_("extent table full; leaking %" PRIu64 " logical bytes", length);
        return;
    }
    memmove(&G.free_extents[at + 1], &G.free_extents[at], (G.free_count - at) * sizeof(extent));
    G.free_extents[at] = (extent){base, length};
    G.free_count++;
}
static uint64_t extent_free_bytes(void) {
    uint64_t total = 0;
    for (size_t n = 0; n < G.free_count; n++) total += G.free_extents[n].length;
    return total;
}

/* ---- helpers ---- */
static int valid_token(const char *text) {
    if (!*text || strlen(text) >= 120) return 0;
    for (const char *c = text; *c; c++)
        if (!((*c >= 'a' && *c <= 'z') || (*c >= 'A' && *c <= 'Z') || (*c >= '0' && *c <= '9') ||
              *c == '.' || *c == '_' || *c == '-')) return 0;
    return 1;
}
static int parse_u64(const char *text, uint64_t *out) {
    char *end;
    if (!text || !*text || *text == '-') return -1;
    errno = 0;
    unsigned long long value = strtoull(text, &end, 0);
    if (errno || *end) return -1;
    *out = value;
    return 0;
}
static int parse_i64(const char *text, int64_t *out) {
    char *end;
    if (!text || !*text) return -1;
    errno = 0;
    long long value = strtoll(text, &end, 10);
    if (errno || *end) return -1;
    *out = value;
    return 0;
}
static int cold_get(const region *r, uint64_t page) { return (r->cold[page / 64] >> (page % 64)) & 1; }
static void cold_set(region *r, uint64_t page, int value) {
    uint64_t mask = UINT64_C(1) << (page % 64);
    if (value && !(r->cold[page / 64] & mask)) { r->cold[page / 64] |= mask; r->cold_pages++; }
    if (!value && (r->cold[page / 64] & mask)) { r->cold[page / 64] &= ~mask; r->cold_pages--; }
}
static sandbox *find_sandbox(const char *id, uint64_t incarnation) {
    sandbox *found = NULL;
    pthread_mutex_lock(&G.table);
    for (int n = 0; n < PG_MAX_SANDBOXES; n++)
        if (G.sandboxes[n].used && G.sandboxes[n].incarnation == incarnation &&
            !strcmp(G.sandboxes[n].id, id)) { found = &G.sandboxes[n]; break; }
    pthread_mutex_unlock(&G.table);
    return found;
}
static int still_attached(const sandbox *s, const char *id, uint64_t incarnation) {
    return s->used && s->incarnation == incarnation && !strcmp(s->id, id);
}

/* Quiescence is verified here as well as by the caller. */
static int cgroup_frozen(const sandbox *s) {
    char path[PATH_MAX + 32], line[256];
    snprintf(path, sizeof(path), "%s/cgroup.events", s->cgroup);
    FILE *file = fopen(path, "r");
    if (!file) return -1;
    int frozen = 0;
    while (fgets(line, sizeof(line), file))
        if (!strncmp(line, "frozen ", 7)) frozen = atoi(line + 7) == 1;
    fclose(file);
    return frozen;
}
static int pid_in_cgroup(const sandbox *s, pid_t pid) {
    char path[PATH_MAX + 32], line[64];
    snprintf(path, sizeof(path), "%s/cgroup.procs", s->cgroup);
    FILE *file = fopen(path, "r");
    if (!file) return -1;
    int found = 0;
    while (fgets(line, sizeof(line), file))
        if ((pid_t)atol(line) == pid) { found = 1; break; }
    fclose(file);
    return found;
}
static int all_tasks_stopped(pid_t pid) {
    char path[64], line[512];
    snprintf(path, sizeof(path), "/proc/%d/status", (int)pid);
    FILE *file = fopen(path, "r");
    if (!file) return -1;
    int stopped = 0;
    while (fgets(line, sizeof(line), file))
        if (!strncmp(line, "State:", 6)) { stopped = strchr(line + 6, 'T') || strchr(line + 6, 't'); break; }
    fclose(file);
    return stopped;
}
static int process_gone(const region *r) {
    struct pollfd probe = {.fd = r->pidfd, .events = POLLIN};
    return poll(&probe, 1, 0) > 0;
}
/* The registered range must be a shared mapping of exactly this memfd. */
static int mapping_matches(pid_t pid, uint64_t addr, uint64_t length, uint64_t file_offset, ino_t inode) {
    char path[64], line[PATH_MAX + 128];
    snprintf(path, sizeof(path), "/proc/%d/maps", (int)pid);
    FILE *file = fopen(path, "r");
    if (!file) return -1;
    uint64_t cursor = addr, end = addr + length;
    while (cursor < end && fgets(line, sizeof(line), file)) {
        unsigned long long start, stop, offset, node;
        char perms[8];
        if (sscanf(line, "%llx-%llx %7s %llx %*s %llu", &start, &stop, perms, &offset, &node) != 5) continue;
        if (stop <= cursor) continue;
        if (start > cursor) break;
        if ((ino_t)node != inode || perms[3] != 's' || perms[0] != 'r' || perms[1] != 'w' ||
            offset + (cursor - start) != file_offset + (cursor - addr)) break;
        cursor = stop;
    }
    fclose(file);
    return cursor >= end;
}
static int uffd_register(region *r) {
    if (r->uffd < 0 || r->registered) return 0;
    struct uffdio_register request = {.range = {.start = r->addr, .len = r->length},
                                      .mode = UFFDIO_REGISTER_MODE_MISSING};
    if (ioctl(r->uffd, UFFDIO_REGISTER, &request)) return -1;
    if (!(request.ioctls & (UINT64_C(1) << _UFFDIO_COPY))) {
        struct uffdio_range range = {.start = r->addr, .len = r->length};
        ioctl(r->uffd, UFFDIO_UNREGISTER, &range);
        errno = EOPNOTSUPP;
        return -1;
    }
    r->registered = 1;
    return 0;
}
static void uffd_unregister(region *r) {
    if (r->uffd < 0 || !r->registered) return;
    struct uffdio_range range = {.start = r->addr, .len = r->length};
    ioctl(r->uffd, UFFDIO_UNREGISTER, &range); /* ESRCH/ENOMEM when the owner is gone. */
    r->registered = 0;
}
/* Copy pages into the owner. Returns 0, or -1 with errno; ESRCH marks the owner gone. */
static int page_in(region *r, uint64_t page, const unsigned char *data, uint64_t pages) {
    uint64_t bytes = pages * PG_PAGE, done = 0;
    if (r->uffd < 0) {
        while (done < bytes) {
            ssize_t wrote = pwrite(r->memfd, data + done, bytes - done,
                                   (off_t)(r->file_offset + page * PG_PAGE + done));
            if (wrote < 0) { if (errno == EINTR) continue; return -1; }
            done += (uint64_t)wrote;
        }
        return 0;
    }
    while (done < bytes) {
        struct uffdio_copy copy = {.dst = r->addr + page * PG_PAGE + done,
                                   .src = (uint64_t)(uintptr_t)(data + done), .len = bytes - done};
        if (!ioctl(r->uffd, UFFDIO_COPY, &copy)) { done += (uint64_t)copy.copy; continue; }
        if (copy.copy > 0) { done += (uint64_t)copy.copy; continue; }
        if (errno == EAGAIN || errno == EINTR) continue;
        if (errno == EEXIST) { done += PG_PAGE; continue; } /* Already resident. */
        return -1;
    }
    return 0;
}
static void drop_region(region *r, int discard_store) {
    /* Caller holds the owner's lock. */
    if (discard_store && r->cold_pages) cs_trim(G.store, r->logical_base, (size_t)r->length);
    uffd_unregister(r);
    if (r->uffd >= 0) { epoll_ctl(G.epoll_fd, EPOLL_CTL_DEL, r->uffd, NULL); close(r->uffd); }
    if (r->memfd >= 0) close(r->memfd);
    if (r->pidfd >= 0) close(r->pidfd);
    pthread_mutex_lock(&G.table);
    extent_free(r->logical_base, r->length);
    r->owner->reserved_bytes -= r->length;
    G.bitmap_bytes -= r->cold_words * sizeof(uint64_t);
    free(r->cold);
    memset(r, 0, sizeof(*r));
    r->pidfd = r->memfd = r->uffd = -1;
    pthread_mutex_unlock(&G.table);
}

/* ---- commands ---- */
typedef struct { char text[PG_LINE]; size_t used; } reply;
static void respond(reply *out, const char *format, ...) {
    va_list args;
    va_start(args, format);
    int wrote = vsnprintf(out->text + out->used, sizeof(out->text) - out->used, format, args);
    va_end(args);
    if (wrote > 0 && (size_t)wrote < sizeof(out->text) - out->used) out->used += (size_t)wrote;
}
static void fail_reply(reply *out, int error, const char *what) {
    out->used = 0;
    respond(out, "ERR %d %s: %s\n", error, what, strerror(error));
}

static void command_hello(reply *out) {
    cs_stats stats;
    cs_get_stats(G.store, &stats);
    respond(out, "OK version=1 medium=%s path=%s offset=%" PRIu64 " capacity=%" PRIu64
            " logical=%" PRIu64 " codec=%s page=%u fault_around=%u quiescence=%s uffd_poison=%d"
            " per_sandbox_max=%" PRIu64 "\n",
            G.options.emulate_file ? "file-emulation" : "device-dax", G.options.path,
            G.options.offset, G.options.capacity, G.options.logical_bytes,
            G.options.codec == CS_CODEC_LZ4 ? "lz4" : "none", PG_PAGE, G.fault_around,
            G.unfenced ? "caller-asserted-stopped-tasks" : "cgroup-freeze",
#ifdef UFFDIO_POISON
            1,
#else
            0,
#endif
            G.per_sandbox_max);
}

static void command_attach(char **argv, int argc, reply *out) {
    uint64_t incarnation, max_bytes;
    if (argc != 5 || !valid_token(argv[1]) || parse_u64(argv[2], &incarnation) ||
        parse_u64(argv[4], &max_bytes) || strlen(argv[3]) >= PATH_MAX) { fail_reply(out, EINVAL, "ATTACH"); return; }
    int unfenced = !strcmp(argv[3], "-");
    if (unfenced && !G.unfenced) { fail_reply(out, EPERM, "ATTACH without cgroup needs --allow-unfenced-quiescence"); return; }
    size_t root_length = strlen(G.cgroup_root);
    if (!unfenced && (strncmp(argv[3], G.cgroup_root, root_length) || argv[3][root_length] != '/' ||
                      strstr(argv[3], "/../") || strstr(argv[3], "/./"))) {
        fail_reply(out, EPERM, "ATTACH cgroup is outside the configured cgroup root"); return;
    }
    if (!max_bytes || max_bytes > G.per_sandbox_max) max_bytes = G.per_sandbox_max;
    pthread_mutex_lock(&G.table);
    sandbox *slot = NULL;
    for (int n = 0; n < PG_MAX_SANDBOXES; n++) {
        if (G.sandboxes[n].used && !strcmp(G.sandboxes[n].id, argv[1])) {
            if (slot) pthread_mutex_unlock(&slot->lock);
            pthread_mutex_unlock(&G.table);
            fail_reply(out, EEXIST, "ATTACH sandbox identifier still attached");
            return;
        }
        if (!G.sandboxes[n].used && !slot && !pthread_mutex_trylock(&G.sandboxes[n].lock))
            slot = &G.sandboxes[n];
    }
    if (!slot) { pthread_mutex_unlock(&G.table); fail_reply(out, ENOSPC, "ATTACH sandbox table"); return; }
    memset((char *)slot + offsetof(sandbox, used), 0, sizeof(*slot) - offsetof(sandbox, used));
    snprintf(slot->id, sizeof(slot->id), "%s", argv[1]);
    snprintf(slot->cgroup, sizeof(slot->cgroup), "%s", unfenced ? "" : argv[3]);
    slot->incarnation = incarnation;
    slot->max_bytes = max_bytes;
    slot->last_generation = -1;
    slot->used = 1;
    pthread_mutex_unlock(&G.table);
    pthread_mutex_unlock(&slot->lock);
    respond(out, "OK max_bytes=%" PRIu64 "\n", max_bytes);
}

static void command_region(char **argv, int argc, reply *out) {
    uint64_t incarnation, pid_value, addr, length, file_offset, kernel_faults;
    int64_t memfd_number, uffd_number;
    if (argc != 10 || !valid_token(argv[1]) || parse_u64(argv[2], &incarnation) ||
        parse_u64(argv[3], &pid_value) || parse_i64(argv[4], &memfd_number) ||
        parse_i64(argv[5], &uffd_number) || parse_u64(argv[6], &addr) || parse_u64(argv[7], &length) ||
        parse_u64(argv[8], &file_offset) || parse_u64(argv[9], &kernel_faults) ||
        !length || addr % PG_PAGE || length % PG_PAGE || file_offset % PG_PAGE ||
        memfd_number < 0 || pid_value == 0 || pid_value > INT_MAX || length > UINT64_MAX - addr) {
        fail_reply(out, EINVAL, "REGION"); return;
    }
    sandbox *s = find_sandbox(argv[1], incarnation);
    if (!s) { fail_reply(out, ENOENT, "REGION sandbox"); return; }
    pid_t pid = (pid_t)pid_value;
    int pidfd = -1, memfd = -1, reopened = -1, uffd = -1, error = 0;
    const char *what = "REGION";
    pthread_mutex_lock(&s->lock);
    if (!still_attached(s, argv[1], incarnation)) { error = ENOENT; what = "REGION sandbox detached"; goto out; }
    pthread_mutex_lock(&G.table);
    for (int n = 0; n < PG_MAX_REGIONS; n++) {
        region *r = &G.regions[n];
        if (r->used && r->owner == s && r->pid == pid && r->addr == addr && r->length == length &&
            !process_gone(r)) {
            pthread_mutex_unlock(&G.table);
            respond(out, "OK region=%u existing=1\n", r->id);
            goto out;
        }
    }
    pthread_mutex_unlock(&G.table);
    pidfd = (int)syscall(SYS_pidfd_open, pid, 0);
    if (pidfd < 0) { error = errno; what = "REGION pidfd_open"; goto out; }
    /* Membership is checked after the pidfd pins the process against PID reuse. */
    if (s->cgroup[0] && pid_in_cgroup(s, pid) != 1) { error = EPERM; what = "REGION pid is outside the sandbox cgroup"; goto out; }
    memfd = (int)syscall(SYS_pidfd_getfd, pidfd, (int)memfd_number, 0);
    if (memfd < 0) { error = errno; what = "REGION pidfd_getfd(memfd)"; goto out; }
    struct stat st;
    int seals = fcntl(memfd, F_GET_SEALS);
    if (fstat(memfd, &st) || !S_ISREG(st.st_mode) || seals < 0) { error = EBADF; what = "REGION descriptor is not a memfd"; goto out; }
    if (seals & (F_SEAL_WRITE
#ifdef F_SEAL_FUTURE_WRITE
                 | F_SEAL_FUTURE_WRITE
#endif
                 )) { error = EPERM; what = "REGION memfd is write-sealed"; goto out; }
    if (st.st_size < 0 || file_offset > (uint64_t)st.st_size || length > (uint64_t)st.st_size - file_offset) {
        error = ERANGE; what = "REGION exceeds memfd"; goto out;
    }
    if (mapping_matches(pid, addr, length, file_offset, st.st_ino) != 1) {
        error = EFAULT; what = "REGION is not a shared rw mapping of that memfd"; goto out;
    }
    /* An independent open file description: our offset never disturbs the owner. */
    char link[64];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", memfd);
    reopened = open(link, O_RDWR | O_CLOEXEC);
    if (reopened < 0) { error = errno; what = "REGION reopen memfd"; goto out; }
    if (uffd_number >= 0) {
        uffd = (int)syscall(SYS_pidfd_getfd, pidfd, (int)uffd_number, 0);
        if (uffd < 0) { error = errno; what = "REGION pidfd_getfd(uffd)"; goto out; }
        char target[64] = {0}, name[64];
        snprintf(name, sizeof(name), "/proc/self/fd/%d", uffd);
        if (readlink(name, target, sizeof(target) - 1) < 0 || strcmp(target, "anon_inode:[userfaultfd]")) {
            error = EBADF; what = "REGION descriptor is not a userfaultfd"; goto out;
        }
    }
    pthread_mutex_lock(&G.table);
    region *slot = NULL;
    for (int n = 0; n < PG_MAX_REGIONS && !slot; n++) if (!G.regions[n].used) slot = &G.regions[n];
    uint64_t base = 0;
    if (!slot) error = ENOSPC, what = "REGION table";
    else if (length > s->max_bytes - s->reserved_bytes) error = EDQUOT, what = "REGION exceeds per-sandbox tier allocation";
    else if (extent_alloc(length, &base)) error = ENOSPC, what = "REGION store logical space";
    if (!error) {
        size_t words = (size_t)((length / PG_PAGE + 63) / 64);
        uint64_t *bits = calloc(words, sizeof(uint64_t));
        if (!bits) { extent_free(base, length); error = ENOMEM; what = "REGION bitmap"; }
        else {
            memset(slot, 0, sizeof(*slot));
            *slot = (region){.used = 1, .id = ++G.next_region, .owner = s, .pid = pid, .pidfd = pidfd,
                             .memfd = reopened, .uffd = uffd, .kernel_faults = kernel_faults != 0,
                             .addr = addr, .length = length, .file_offset = file_offset,
                             .logical_base = base, .cold = bits, .cold_words = words};
            s->reserved_bytes += length;
            G.bitmap_bytes += words * sizeof(uint64_t);
        }
    }
    pthread_mutex_unlock(&G.table);
    if (!error) {
        if (uffd >= 0) {
            struct epoll_event event = {.events = EPOLLIN, .data.ptr = slot};
            if (epoll_ctl(G.epoll_fd, EPOLL_CTL_ADD, uffd, &event)) {
                error = errno; what = "REGION epoll";
                pidfd = reopened = uffd = -1; /* Owned by the slot now. */
                drop_region(slot, 0);
                goto out;
            }
        }
        respond(out, "OK region=%u existing=0 logical_base=%" PRIu64 "\n", slot->id, base);
        pidfd = reopened = uffd = -1;
    }
out:
    pthread_mutex_unlock(&s->lock);
    if (memfd >= 0) close(memfd);
    if (pidfd >= 0) close(pidfd);
    if (reopened >= 0) close(reopened);
    if (uffd >= 0) close(uffd);
    if (error) { s->errors++; fail_reply(out, error, what); }
}

static int admit_generation(sandbox *s, int64_t generation) {
    if (generation < 0) return 0;
    if (generation < s->last_generation) { s->stale++; return -1; }
    s->last_generation = generation;
    return 0;
}

static void command_demote(char **argv, int argc, unsigned char *staging, reply *out) {
    uint64_t incarnation;
    int64_t generation, target;
    if (argc != 5 || !valid_token(argv[1]) || parse_u64(argv[2], &incarnation) ||
        parse_i64(argv[3], &generation) || parse_i64(argv[4], &target)) { fail_reply(out, EINVAL, "DEMOTE"); return; }
    sandbox *s = find_sandbox(argv[1], incarnation);
    if (!s) { fail_reply(out, ENOENT, "DEMOTE sandbox"); return; }
    pthread_mutex_lock(&s->lock);
    if (!still_attached(s, argv[1], incarnation)) { pthread_mutex_unlock(&s->lock); fail_reply(out, ENOENT, "DEMOTE sandbox detached"); return; }
    if (admit_generation(s, generation)) { pthread_mutex_unlock(&s->lock); fail_reply(out, ESTALE, "DEMOTE generation is older than an accepted movement"); return; }
    if (s->poisoned) { pthread_mutex_unlock(&s->lock); fail_reply(out, EIO, "DEMOTE sandbox is poisoned by an earlier restore failure"); return; }
    if (s->cgroup[0] && cgroup_frozen(s) != 1) {
        s->errors++;
        pthread_mutex_unlock(&s->lock);
        fail_reply(out, EBUSY, "DEMOTE requires a confirmed frozen cgroup");
        return;
    }
    uint64_t budget = target < 0 ? UINT64_MAX : (uint64_t)target;
    uint64_t eligible = 0, stored = 0, released = 0, regions = 0, gone = 0, copy_ns = 0, punch_ns = 0;
    int partial = 0, error = 0;
    s->requested += target < 0 ? 0 : (uint64_t)target;
    for (int n = 0; n < PG_MAX_REGIONS && !error; n++) {
        region *r = &G.regions[n];
        if (!r->used || r->owner != s) continue;
        if (process_gone(r)) { gone++; drop_region(r, 1); continue; }
        if (!s->cgroup[0] && all_tasks_stopped(r->pid) != 1) { error = EBUSY; break; }
        regions++;
        struct stat before, after;
        if (fstat(r->memfd, &before)) { error = errno; break; }
        off_t cursor = (off_t)r->file_offset, limit = (off_t)(r->file_offset + r->length);
        while (cursor < limit && stored < budget && !error) {
            off_t data = lseek(r->memfd, cursor, SEEK_DATA);
            if (data < 0 || data >= limit) break; /* ENXIO: no more resident data. */
            off_t hole = lseek(r->memfd, data, SEEK_HOLE);
            if (hole < 0 || hole > limit) hole = limit;
            data -= data % PG_PAGE;
            eligible += (uint64_t)(hole - data);
            while (data < hole && stored < budget && !error) {
                uint64_t chunk = (uint64_t)(hole - data);
                if (chunk > PG_STAGING_BYTES) chunk = PG_STAGING_BYTES;
                if (chunk > budget - stored) chunk = ((budget - stored) + PG_PAGE - 1) / PG_PAGE * PG_PAGE;
                uint64_t logical = r->logical_base + ((uint64_t)data - r->file_offset);
                uint64_t started = now_ns(), got = 0;
                while (got < chunk) {
                    ssize_t count = pread(r->memfd, staging + got, chunk - got, data + (off_t)got);
                    if (count < 0 && errno == EINTR) continue;
                    if (count <= 0) { error = count ? errno : EIO; break; }
                    got += (uint64_t)count;
                }
                if (!error && cs_write(G.store, logical, staging, (size_t)chunk)) {
                    error = errno;
                    /* A multi-page store write is not a transaction: forget the
                     * partial copy so no unreleased page has a stale store twin. */
                    cs_trim(G.store, logical, (size_t)chunk);
                }
                copy_ns += now_ns() - started;
                if (error) break;
                /* Arm demand paging before the first source page disappears. */
                if (uffd_register(r)) { error = errno; cs_trim(G.store, logical, (size_t)chunk); break; }
                started = now_ns();
                if (fallocate(r->memfd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, data, (off_t)chunk)) {
                    error = errno;
                    cs_trim(G.store, logical, (size_t)chunk);
                    break;
                }
                punch_ns += now_ns() - started;
                for (uint64_t page = 0; page < chunk / PG_PAGE; page++)
                    cold_set(r, ((uint64_t)data - r->file_offset) / PG_PAGE + page, 1);
                stored += chunk;
                data += (off_t)chunk;
            }
            cursor = hole;
        }
        if (!fstat(r->memfd, &after) && before.st_blocks > after.st_blocks)
            released += (uint64_t)(before.st_blocks - after.st_blocks) * 512u;
        if (!r->cold_pages) uffd_unregister(r);
    }
    if (error) { partial = 1; s->errors++; }
    s->eligible += eligible; s->stored += stored; s->released += released;
    s->copy_out_ns += copy_ns; s->punch_ns += punch_ns; s->demotions++;
    uint64_t cold = 0;
    for (int n = 0; n < PG_MAX_REGIONS; n++)
        if (G.regions[n].used && G.regions[n].owner == s) cold += G.regions[n].cold_pages * PG_PAGE;
    pthread_mutex_unlock(&s->lock);
    if (error && !stored) { fail_reply(out, error, "DEMOTE moved nothing"); return; }
    respond(out, "OK eligible=%" PRIu64 " stored=%" PRIu64 " released=%" PRIu64 " cold=%" PRIu64
            " regions=%" PRIu64 " gone=%" PRIu64 " partial=%d errno=%d copy_ns=%" PRIu64 " punch_ns=%" PRIu64 "\n",
            eligible, stored, released, cold, regions, gone, partial, error, copy_ns, punch_ns);
}

static void command_restore(char **argv, int argc, unsigned char *staging, reply *out) {
    uint64_t incarnation;
    int64_t generation;
    if (argc != 5 || !valid_token(argv[1]) || parse_u64(argv[2], &incarnation) || parse_i64(argv[3], &generation) ||
        (strcmp(argv[4], "eager") && strcmp(argv[4], "lazy") && strcmp(argv[4], "lazy-user"))) {
        fail_reply(out, EINVAL, "RESTORE"); return;
    }
    int lazy = argv[4][0] == 'l', allow_user_only = !strcmp(argv[4], "lazy-user");
    sandbox *s = find_sandbox(argv[1], incarnation);
    if (!s) { fail_reply(out, ENOENT, "RESTORE sandbox"); return; }
    pthread_mutex_lock(&s->lock); /* Synchronizes with any in-flight demotion. */
    if (!still_attached(s, argv[1], incarnation)) { pthread_mutex_unlock(&s->lock); fail_reply(out, ENOENT, "RESTORE sandbox detached"); return; }
    if (admit_generation(s, generation)) { pthread_mutex_unlock(&s->lock); fail_reply(out, ESTALE, "RESTORE generation is older than an accepted movement"); return; }
    if (s->poisoned) { pthread_mutex_unlock(&s->lock); fail_reply(out, EIO, "RESTORE sandbox is poisoned"); return; }
    uint64_t restored = 0, pending = 0, gone = 0, elapsed = now_ns();
    int error = 0;
    const char *what = "RESTORE";
    if (lazy) {
        /* Readiness contract: every cold page is covered by an armed fault path
         * that also resolves kernel-mode faults, unless the owner opted in. */
        for (int n = 0; n < PG_MAX_REGIONS && !error; n++) {
            region *r = &G.regions[n];
            if (!r->used || r->owner != s || !r->cold_pages) continue;
            if (r->uffd < 0 || !r->registered) { error = EOPNOTSUPP; what = "RESTORE lazy needs a registered userfaultfd"; }
            else if (!r->kernel_faults && !allow_user_only) { error = EOPNOTSUPP; what = "RESTORE lazy refused: user-mode-only userfaultfd"; }
            pending += r->cold_pages * PG_PAGE;
        }
    } else {
        for (int n = 0; n < PG_MAX_REGIONS && !error; n++) {
            region *r = &G.regions[n];
            if (!r->used || r->owner != s || !r->cold_pages) continue;
            if (process_gone(r)) { gone++; drop_region(r, 1); continue; }
            uint64_t pages = r->length / PG_PAGE, page = 0;
            while (page < pages && !error) {
                if (!cold_get(r, page)) { page++; continue; }
                uint64_t run = 1;
                while (page + run < pages && run < PG_STAGING_BYTES / PG_PAGE && cold_get(r, page + run)) run++;
                if (cs_read(G.store, r->logical_base + page * PG_PAGE, staging, (size_t)(run * PG_PAGE))) {
                    error = errno; what = "RESTORE store read"; s->poisoned = 1; break;
                }
                if (page_in(r, page, staging, run)) {
                    if (errno == ESRCH) { gone++; drop_region(r, 1); r = NULL; break; }
                    error = errno; what = "RESTORE page-in"; break;
                }
                for (uint64_t n2 = 0; n2 < run; n2++) cold_set(r, page + n2, 0);
                cs_trim(G.store, r->logical_base + page * PG_PAGE, (size_t)(run * PG_PAGE));
                restored += run * PG_PAGE;
                page += run;
            }
            if (r && !error && !r->cold_pages) uffd_unregister(r);
        }
        for (int n = 0; n < PG_MAX_REGIONS; n++)
            if (G.regions[n].used && G.regions[n].owner == s) pending += G.regions[n].cold_pages * PG_PAGE;
    }
    elapsed = now_ns() - elapsed;
    s->restored_eager += restored; s->copy_in_ns += elapsed; s->restores++;
    if (error) s->errors++;
    pthread_mutex_unlock(&s->lock);
    if (error) { fail_reply(out, error, what); return; }
    respond(out, "OK restored=%" PRIu64 " lazy_pending=%" PRIu64 " gone=%" PRIu64 " mode=%s ns=%" PRIu64 "\n",
            restored, pending, gone, argv[4], elapsed);
}

static void command_stat(char **argv, int argc, reply *out) {
    uint64_t incarnation;
    if (argc != 3 || !valid_token(argv[1]) || parse_u64(argv[2], &incarnation)) { fail_reply(out, EINVAL, "STAT"); return; }
    sandbox *s = find_sandbox(argv[1], incarnation);
    if (!s) { fail_reply(out, ENOENT, "STAT sandbox"); return; }
    pthread_mutex_lock(&s->lock);
    uint64_t cold = 0, regions = 0, bitmap = 0;
    for (int n = 0; n < PG_MAX_REGIONS; n++)
        if (G.regions[n].used && G.regions[n].owner == s) {
            cold += G.regions[n].cold_pages * PG_PAGE; regions++;
            bitmap += G.regions[n].cold_words * sizeof(uint64_t);
        }
    respond(out, "OK requested=%" PRIu64 " eligible=%" PRIu64 " stored=%" PRIu64 " released=%" PRIu64
            " restored_eager=%" PRIu64 " restored_fault=%" PRIu64 " faults=%" PRIu64 " cold=%" PRIu64
            " regions=%" PRIu64 " reserved=%" PRIu64 " max_bytes=%" PRIu64 " demotions=%" PRIu64
            " restores=%" PRIu64 " stale=%" PRIu64 " errors=%" PRIu64 " poisoned=%d metadata=%" PRIu64
            " copy_out_ns=%" PRIu64 " punch_ns=%" PRIu64 " copy_in_ns=%" PRIu64 " fault_ns=%" PRIu64 "\n",
            s->requested, s->eligible, s->stored, s->released, s->restored_eager, s->restored_fault,
            s->faults, cold, regions, s->reserved_bytes, s->max_bytes, s->demotions, s->restores,
            s->stale, s->errors, s->poisoned, bitmap + sizeof(sandbox) + regions * sizeof(region),
            s->copy_out_ns, s->punch_ns, s->copy_in_ns, s->fault_ns);
    pthread_mutex_unlock(&s->lock);
}

static void command_stats(reply *out) {
    cs_stats stats;
    cs_get_stats(G.store, &stats);
    pthread_mutex_lock(&G.table);
    uint64_t attached = 0, cold = 0, regions = 0;
    for (int n = 0; n < PG_MAX_SANDBOXES; n++) attached += G.sandboxes[n].used != 0;
    for (int n = 0; n < PG_MAX_REGIONS; n++)
        if (G.regions[n].used) { regions++; cold += G.regions[n].cold_pages * PG_PAGE; }
    uint64_t logical_free = extent_free_bytes();
    uint64_t pager_metadata = sizeof(G) + G.bitmap_bytes + G.staging_bytes;
    pthread_mutex_unlock(&G.table);
    respond(out, "OK capacity=%" PRIu64 " logical=%" PRIu64 " logical_free=%" PRIu64 " payload=%" PRIu64
            " allocated=%" PRIu64 " written_pages=%" PRIu64 " store_metadata=%" PRIu64
            " pager_metadata=%" PRIu64 " staging=%" PRIu64 " sandboxes=%" PRIu64 " regions=%" PRIu64
            " cold=%" PRIu64 " writes=%" PRIu64 " reads=%" PRIu64 " trims=%" PRIu64
            " compression_calls=%" PRIu64 " decompression_calls=%" PRIu64 "\n",
            G.options.capacity, stats.logical_bytes, logical_free, stats.payload_bytes,
            stats.allocated_bytes, stats.written_pages, stats.metadata_bytes, pager_metadata,
            G.staging_bytes, attached, regions, cold, stats.writes, stats.reads, stats.trims,
            stats.compression_calls, stats.decompression_calls);
}

static void command_detach(char **argv, int argc, reply *out) {
    uint64_t incarnation;
    if (argc != 3 || !valid_token(argv[1]) || parse_u64(argv[2], &incarnation)) { fail_reply(out, EINVAL, "DETACH"); return; }
    sandbox *s = find_sandbox(argv[1], incarnation);
    if (!s) { fail_reply(out, ENOENT, "DETACH sandbox"); return; }
    pthread_mutex_lock(&s->lock); /* Waits for in-flight movement and fault service. */
    if (!still_attached(s, argv[1], incarnation)) { pthread_mutex_unlock(&s->lock); fail_reply(out, ENOENT, "DETACH sandbox detached"); return; }
    uint64_t discarded = 0, regions = 0;
    for (int n = 0; n < PG_MAX_REGIONS; n++) {
        region *r = &G.regions[n];
        if (!r->used || r->owner != s) continue;
        discarded += r->cold_pages * PG_PAGE;
        regions++;
        drop_region(r, 1);
    }
    pthread_mutex_lock(&G.table);
    s->used = 0;
    pthread_mutex_unlock(&G.table);
    pthread_mutex_unlock(&s->lock);
    respond(out, "OK discarded=%" PRIu64 " regions=%" PRIu64 "\n", discarded, regions);
}

static void command_shutdown(char **argv, int argc, reply *out) {
    /* Caller holds the lifecycle write lock: no demotion can be between its
     * store write and cold-page registration, and no new operation can start. */
    int force = argc == 2 && !strcmp(argv[1], "force");
    pthread_mutex_lock(&G.table);
    uint64_t cold = 0;
    for (int n = 0; n < PG_MAX_REGIONS; n++) if (G.regions[n].used) cold += G.regions[n].cold_pages * PG_PAGE;
    pthread_mutex_unlock(&G.table);
    if (cold && !force) { fail_reply(out, EBUSY, "SHUTDOWN refused: consumers still depend on cold pages"); return; }
    G.stop = 1;
    respond(out, "OK cold_discarded=%" PRIu64 "\n", cold);
}

/* ---- demand-fault service ---- */
static void serve_fault(region *r, uint64_t address, unsigned char *buffer) {
    sandbox *s = r->owner;
    uint64_t started = now_ns();
    if (address < r->addr || address >= r->addr + r->length) return;
    uint64_t page = (address - r->addr) / PG_PAGE, pages = r->length / PG_PAGE, run = 1;
    if (!cold_get(r, page)) { /* Never stored: a hole the owner had not touched. */
        memset(buffer, 0, PG_PAGE);
        page_in(r, page, buffer, 1);
        return;
    }
    while (run < G.fault_around && page + run < pages && cold_get(r, page + run)) run++;
    if (cs_read(G.store, r->logical_base + page * PG_PAGE, buffer, (size_t)(run * PG_PAGE))) {
        s->poisoned = 1; s->errors++;
        logf_("store read failed for %s page %" PRIu64 ": %s", s->id, page, strerror(errno));
#ifdef UFFDIO_POISON
        /* Fail stop: the faulting access gets SIGBUS instead of wrong bytes. */
        struct uffdio_poison poison = {.range = {.start = r->addr + page * PG_PAGE, .len = PG_PAGE}};
        ioctl(r->uffd, UFFDIO_POISON, &poison);
#endif
        return;
    }
    if (page_in(r, page, buffer, run)) { s->errors++; return; }
    for (uint64_t n = 0; n < run; n++) cold_set(r, page + n, 0);
    cs_trim(G.store, r->logical_base + page * PG_PAGE, (size_t)(run * PG_PAGE));
    s->faults++;
    s->restored_fault += run * PG_PAGE;
    s->fault_ns += now_ns() - started;
    if (!r->cold_pages) uffd_unregister(r);
}
static void *fault_thread(void *unused) {
    (void)unused;
    size_t bytes = (size_t)G.fault_around * PG_PAGE;
    unsigned char *buffer = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (buffer == MAP_FAILED || (G.lock_buffers && mlock(buffer, bytes))) { logf_("fault buffer: %s", strerror(errno)); exit(1); }
    memset(buffer, 0, bytes);
    while (!G.stop) {
        struct epoll_event events[16];
        int count = epoll_wait(G.epoll_fd, events, 16, 100);
        for (int n = 0; n < count; n++) {
            pthread_rwlock_rdlock(&G.lifecycle);
            if (G.stop) { pthread_rwlock_unlock(&G.lifecycle); break; }
            region *r = events[n].data.ptr;
            pthread_mutex_lock(&G.table);
            sandbox *s = r->used ? r->owner : NULL;
            pthread_mutex_unlock(&G.table);
            if (!s) { pthread_rwlock_unlock(&G.lifecycle); continue; }
            pthread_mutex_lock(&s->lock);
            if (r->used && r->owner == s && r->uffd >= 0) {
                struct uffd_msg message;
                /* One message per readiness; epoll is level triggered. */
                struct pollfd ready = {.fd = r->uffd, .events = POLLIN};
                if (poll(&ready, 1, 0) > 0 && (ready.revents & POLLIN) &&
                    read(r->uffd, &message, sizeof(message)) == (ssize_t)sizeof(message) &&
                    message.event == UFFD_EVENT_PAGEFAULT)
                    serve_fault(r, message.arg.pagefault.address, buffer);
                else if (ready.revents & (POLLERR | POLLHUP)) {
                    epoll_ctl(G.epoll_fd, EPOLL_CTL_DEL, r->uffd, NULL);
                }
            }
            pthread_mutex_unlock(&s->lock);
            pthread_rwlock_unlock(&G.lifecycle);
        }
    }
    munmap(buffer, bytes);
    return NULL;
}

/* ---- connections ---- */
static void *connection_thread(void *argument) {
    int slot = (int)(intptr_t)argument;
    pthread_mutex_lock(&G.table);
    int fd = G.connection_fds[slot];
    pthread_mutex_unlock(&G.table);
    unsigned char *staging = mmap(NULL, PG_STAGING_BYTES, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (staging == MAP_FAILED || (G.lock_buffers && mlock(staging, PG_STAGING_BYTES))) {
        logf_("staging buffer: %s", strerror(errno));
        if (staging != MAP_FAILED) munmap(staging, PG_STAGING_BYTES);
        goto done;
    }
    memset(staging, 0, PG_STAGING_BYTES); /* Fault in before any movement. */
    pthread_mutex_lock(&G.table); G.staging_bytes += PG_STAGING_BYTES; pthread_mutex_unlock(&G.table);
    char line[PG_LINE];
    size_t used = 0;
    for (;;) {
        ssize_t got = read(fd, line + used, sizeof(line) - 1 - used);
        if (got < 0 && errno == EINTR) continue;
        if (got <= 0) break;
        used += (size_t)got;
        line[used] = 0;
        char *newline;
        while ((newline = memchr(line, '\n', used))) {
            *newline = 0;
            char *argv[16], *save = NULL;
            int argc = 0;
            for (char *token = strtok_r(line, " ", &save); token && argc < 16; token = strtok_r(NULL, " ", &save))
                argv[argc++] = token;
            reply out = {.used = 0};
            int exclusive = argc && (!strcmp(argv[0], "SHUTDOWN") || !strcmp(argv[0], "STATS"));
            if (exclusive) pthread_rwlock_wrlock(&G.lifecycle);
            else pthread_rwlock_rdlock(&G.lifecycle);
            if (G.stop) fail_reply(&out, ESHUTDOWN, "pager is stopping");
            else if (!argc) fail_reply(&out, EINVAL, "empty command");
            else if (!strcmp(argv[0], "HELLO")) command_hello(&out);
            else if (!strcmp(argv[0], "ATTACH")) command_attach(argv, argc, &out);
            else if (!strcmp(argv[0], "REGION")) command_region(argv, argc, &out);
            else if (!strcmp(argv[0], "DEMOTE")) command_demote(argv, argc, staging, &out);
            else if (!strcmp(argv[0], "RESTORE")) command_restore(argv, argc, staging, &out);
            else if (!strcmp(argv[0], "STAT")) command_stat(argv, argc, &out);
            else if (!strcmp(argv[0], "STATS")) command_stats(&out);
            else if (!strcmp(argv[0], "DETACH")) command_detach(argv, argc, &out);
            else if (!strcmp(argv[0], "SHUTDOWN")) command_shutdown(argv, argc, &out);
            else fail_reply(&out, ENOSYS, "unknown command");
            size_t sent = 0;
            while (sent < out.used) {
                ssize_t wrote = write(fd, out.text + sent, out.used - sent);
                if (wrote < 0 && errno == EINTR) continue;
                if (wrote <= 0) break;
                sent += (size_t)wrote;
            }
            pthread_rwlock_unlock(&G.lifecycle);
            size_t consumed = (size_t)(newline - line) + 1;
            memmove(line, newline + 1, used - consumed);
            used -= consumed;
        }
        if (used == sizeof(line) - 1) break; /* Oversized command: drop the peer. */
    }
    pthread_mutex_lock(&G.table); G.staging_bytes -= PG_STAGING_BYTES; pthread_mutex_unlock(&G.table);
    munmap(staging, PG_STAGING_BYTES);
done:
    pthread_mutex_lock(&G.table);
    close(fd);
    G.connection_fds[slot] = -1;
    G.connections--;
    pthread_cond_broadcast(&G.drained);
    pthread_mutex_unlock(&G.table);
    return NULL;
}

static void on_signal(int number) {
    (void)number;
    G.signals++;
}
static void usage(void) {
    fprintf(stderr,
        "Usage: crate_pagerd --socket PATH (--emulate-file PATH | --reserved-dax /dev/dax0.0)\n"
        "       --offset BYTES --capacity BYTES --logical-bytes BYTES [--codec none|lz4]\n"
        "       [--per-sandbox-max-bytes BYTES] [--fault-around-pages N] [--lock-buffers]\n"
        "       [--cgroup-root DIR] [--allow-unfenced-quiescence]\n");
    exit(2);
}
int main(int argc, char **argv) {
    memset(&G, 0, sizeof(G));
    G.fault_around = 16;
    G.cgroup_root = "/sys/fs/cgroup";
    G.epoll_fd = G.listen_fd = -1;
    for (int n = 1; n < argc; n++) {
        const char *value = n + 1 < argc ? argv[n + 1] : NULL;
        if (!strcmp(argv[n], "--socket") && value) { G.socket_path = value; n++; }
        else if (!strcmp(argv[n], "--emulate-file") && value) { G.options.path = value; G.options.emulate_file = 1; n++; }
        else if (!strcmp(argv[n], "--reserved-dax") && value) { G.options.path = value; G.options.emulate_file = 0; n++; }
        else if (!strcmp(argv[n], "--offset") && value && !parse_u64(value, &G.options.offset)) n++;
        else if (!strcmp(argv[n], "--capacity") && value && !parse_u64(value, &G.options.capacity)) n++;
        else if (!strcmp(argv[n], "--logical-bytes") && value && !parse_u64(value, &G.options.logical_bytes)) n++;
        else if (!strcmp(argv[n], "--per-sandbox-max-bytes") && value && !parse_u64(value, &G.per_sandbox_max)) n++;
        else if (!strcmp(argv[n], "--fault-around-pages") && value) {
            uint64_t pages;
            if (parse_u64(value, &pages) || !pages || pages > 256) usage();
            G.fault_around = (unsigned)pages; n++;
        }
        else if (!strcmp(argv[n], "--codec") && value) {
            if (!strcmp(value, "lz4")) G.options.codec = CS_CODEC_LZ4;
            else if (strcmp(value, "none")) usage();
            n++;
        }
        else if (!strcmp(argv[n], "--cgroup-root") && value) { G.cgroup_root = value; n++; }
        else if (!strcmp(argv[n], "--lock-buffers")) G.lock_buffers = G.options.lock_metadata = 1;
        else if (!strcmp(argv[n], "--allow-unfenced-quiescence")) G.unfenced = 1;
        else usage();
    }
    if (!G.socket_path || !G.options.path || !G.options.capacity || !G.options.logical_bytes) usage();
    if (sysconf(_SC_PAGESIZE) != (long)PG_PAGE) { logf_("host page size must be 4096"); return 1; }
    if (!G.per_sandbox_max || G.per_sandbox_max > G.options.logical_bytes) G.per_sandbox_max = G.options.logical_bytes;
    for (int n = 0; n < PG_MAX_SANDBOXES; n++) pthread_mutex_init(&G.sandboxes[n].lock, NULL);
    for (int n = 0; n < PG_MAX_REGIONS; n++) G.regions[n].pidfd = G.regions[n].memfd = G.regions[n].uffd = -1;
    pthread_mutex_init(&G.table, NULL);
    pthread_rwlock_init(&G.lifecycle, NULL);
    pthread_cond_init(&G.drained, NULL);
    for (int n = 0; n < PG_MAX_CONNECTIONS; n++) G.connection_fds[n] = -1;
    if (cs_open(&G.store, &G.options)) { logf_("cs_open %s: %s", G.options.path, strerror(errno)); return 1; }
    G.free_extents[0] = (extent){0, G.options.logical_bytes};
    G.free_count = 1;
    G.epoll_fd = epoll_create1(EPOLL_CLOEXEC);
    if (G.epoll_fd < 0) { logf_("epoll: %s", strerror(errno)); return 1; }
    struct sigaction action = {.sa_handler = on_signal};
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    signal(SIGPIPE, SIG_IGN);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(G.socket_path) >= sizeof(address.sun_path)) { logf_("socket path too long"); return 1; }
    strcpy(address.sun_path, G.socket_path);
    G.listen_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    mode_t previous = umask(0177); /* Control socket is owner-only. */
    if (G.listen_fd < 0 || bind(G.listen_fd, (struct sockaddr *)&address, sizeof(address)) || listen(G.listen_fd, 16)) {
        logf_("listen %s: %s", G.socket_path, strerror(errno));
        return 1;
    }
    umask(previous);
    pthread_t faults;
    if (pthread_create(&faults, NULL, fault_thread, NULL)) { logf_("fault thread: %s", strerror(errno)); return 1; }
    logf_("ready socket=%s medium=%s offset=%" PRIu64 " capacity=%" PRIu64, G.socket_path,
          G.options.emulate_file ? "file-emulation" : "device-dax", G.options.offset, G.options.capacity);
    while (!G.stop) {
        if (G.signals) {
            /* A signal is a polite request: it cannot strand consumers of cold
             * pages. Only a repeated signal forces the owner down. */
            pthread_rwlock_wrlock(&G.lifecycle);
            uint64_t cold = 0;
            for (int n = 0; n < PG_MAX_REGIONS; n++) if (G.regions[n].used) cold += G.regions[n].cold_pages;
            if (!cold || G.signals > 1) {
                if (cold) logf_("forced stop discards %" PRIu64 " cold pages", cold);
                G.stop = 1;
                pthread_rwlock_unlock(&G.lifecycle);
                break;
            }
            pthread_rwlock_unlock(&G.lifecycle);
            if (!G.warned) logf_("signal deferred: %" PRIu64 " cold pages still have consumers", cold);
            G.warned = 1;
        }
        struct pollfd wait = {.fd = G.listen_fd, .events = POLLIN};
        if (poll(&wait, 1, 200) <= 0) continue;
        int fd = accept4(G.listen_fd, NULL, NULL, SOCK_CLOEXEC);
        if (fd < 0) continue;
        pthread_mutex_lock(&G.table);
        int slot = -1;
        for (int n = 0; n < PG_MAX_CONNECTIONS; n++)
            if (G.connection_fds[n] < 0) { slot = n; break; }
        int admit = slot >= 0 && !G.stop;
        if (admit) { G.connections++; G.connection_fds[slot] = fd; }
        pthread_mutex_unlock(&G.table);
        pthread_t thread;
        if (!admit || pthread_create(&thread, NULL, connection_thread, (void *)(intptr_t)slot)) {
            if (admit) {
                pthread_mutex_lock(&G.table);
                G.connections--; G.connection_fds[slot] = -1;
                pthread_mutex_unlock(&G.table);
            }
            close(fd);
            continue;
        }
        pthread_detach(thread);
    }
    G.stop = 1;
    /* Wait for the successful SHUTDOWN reply and all store operations before
     * waking idle connection readers. Detached workers still must drain. */
    pthread_rwlock_wrlock(&G.lifecycle);
    pthread_mutex_lock(&G.table);
    for (int n = 0; n < PG_MAX_CONNECTIONS; n++)
        if (G.connection_fds[n] >= 0) shutdown(G.connection_fds[n], SHUT_RDWR);
    pthread_mutex_unlock(&G.table);
    pthread_rwlock_unlock(&G.lifecycle);
    pthread_mutex_lock(&G.table);
    while (G.connections) pthread_cond_wait(&G.drained, &G.table);
    pthread_mutex_unlock(&G.table);
    pthread_join(faults, NULL);
    close(G.listen_fd);
    unlink(G.socket_path);
    cs_close(G.store);
    logf_("stopped");
    return 0;
}
#endif
