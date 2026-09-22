/* Cooperative memory holder: a real sandbox process whose tier-eligible region
 * is paged out and back by crate_pagerd. It is a mechanism fixture for actual
 * sandbox paging tests, not SWE-bench state and not a density result.
 *
 *   coop_holder serve --socket PATH --size-mib N [--seed S] [--daemonize]
 *   coop_holder check|mutate|sysread|quit --socket PATH
 *
 * Expected page contents are regenerated from (seed, page, version), never read
 * back from the region, so a check detects lost, stale or zero-filled pages. */
#define _GNU_SOURCE
#include "coop.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define PAGE 4096u
static uint32_t crc_table[256];
static void crc_init(void) {
    for (uint32_t n = 0; n < 256; n++) {
        uint32_t value = n;
        for (int bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320u ^ (value >> 1) : value >> 1;
        crc_table[n] = value;
    }
}
static uint32_t crc_update(uint32_t crc, const unsigned char *data, size_t length) {
    crc = ~crc;
    while (length--) crc = crc_table[(crc ^ *data++) & 0xff] ^ (crc >> 8);
    return ~crc;
}
static uint64_t now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}
static void pattern(unsigned char *page, uint64_t seed, uint64_t index, uint32_t version) {
    uint64_t state = seed ^ (index * UINT64_C(0x9e3779b97f4a7c15)) ^ ((uint64_t)version << 40) ^ 1;
    for (unsigned n = 0; n < PAGE; n += 8) {
        state ^= state << 13; state ^= state >> 7; state ^= state << 17;
        memcpy(page + n, &state, 8);
    }
}
static int connect_to(const char *path) {
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(address.sun_path)) { errno = ENAMETOOLONG; return -1; }
    strcpy(address.sun_path, path);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0 || connect(fd, (struct sockaddr *)&address, sizeof(address))) { if (fd >= 0) close(fd); return -1; }
    return fd;
}
static int client(const char *path, const char *verb) {
    int fd = connect_to(path);
    if (fd < 0) { perror("connect"); return 1; }
    char line[1024];
    snprintf(line, sizeof(line), "%s\n", verb);
    if (write(fd, line, strlen(line)) < 0) { perror("write"); return 1; }
    size_t used = 0;
    ssize_t got;
    while (used < sizeof(line) - 1 && (got = read(fd, line + used, sizeof(line) - 1 - used)) > 0) used += (size_t)got;
    line[used] = 0;
    close(fd);
    fputs(line, stdout);
    return strstr(line, "\"ok\": true") ? 0 : 1;
}

int main(int argc, char **argv) {
    const char *socket_path = NULL;
    uint64_t size_mib = 0, seed = 0x5eed;
    int daemonize = 0;
    if (argc < 2) goto usage;
    for (int n = 2; n < argc; n++) {
        if (!strcmp(argv[n], "--socket") && n + 1 < argc) socket_path = argv[++n];
        else if (!strcmp(argv[n], "--size-mib") && n + 1 < argc) size_mib = strtoull(argv[++n], NULL, 10);
        else if (!strcmp(argv[n], "--seed") && n + 1 < argc) seed = strtoull(argv[++n], NULL, 0);
        else if (!strcmp(argv[n], "--daemonize")) daemonize = 1;
        else goto usage;
    }
    if (!socket_path) goto usage;
    if (strcmp(argv[1], "serve")) {
        if (strcmp(argv[1], "check") && strcmp(argv[1], "mutate") && strcmp(argv[1], "sysread") && strcmp(argv[1], "quit")) goto usage;
        char verb[16];
        snprintf(verb, sizeof(verb), "%s", argv[1]);
        for (char *c = verb; *c; c++) *c = (char)(*c - 32);
        return client(socket_path, verb);
    }
    if (!size_mib || size_mib > 16384) goto usage;
    int ready[2] = {-1, -1};
    if (daemonize) {
        if (pipe(ready)) { perror("pipe"); return 1; }
        pid_t child = fork();
        if (child < 0) { perror("fork"); return 1; }
        if (child) { /* Parent: relay the child's readiness line and leave. */
            close(ready[1]);
            char line[512];
            ssize_t got = read(ready[0], line, sizeof(line) - 1);
            if (got <= 0) { fprintf(stderr, "holder failed before readiness\n"); return 1; }
            line[got] = 0;
            fputs(line, stdout);
            return 0;
        }
        close(ready[0]);
        setsid();
        int null = open("/dev/null", O_RDWR);
        if (null >= 0) { dup2(null, 0); dup2(null, 1); dup2(null, 2); if (null > 2) close(null); }
    }
    signal(SIGPIPE, SIG_IGN);
    crc_init();
    crate_coop_region region;
    if (crate_coop_create((size_t)(size_mib << 20), &region)) { perror("crate_coop_create"); return 1; }
    uint64_t pages = region.length / PAGE;
    uint32_t *versions = calloc(pages, sizeof(uint32_t));
    if (!versions) { perror("calloc"); return 1; }
    unsigned char *base = region.addr;
    for (uint64_t index = 0; index < pages; index++) pattern(base + index * PAGE, seed, index, 0);
    unlink(socket_path);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(socket_path) >= sizeof(address.sun_path)) { fprintf(stderr, "socket path too long\n"); return 1; }
    strcpy(address.sun_path, socket_path);
    int listener = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (listener < 0 || bind(listener, (struct sockaddr *)&address, sizeof(address)) || listen(listener, 4)) { perror("listen"); return 1; }
    char line[512];
    int wrote = snprintf(line, sizeof(line),
        "{\"ok\": true, \"ready\": true, \"pid\": %d, \"bytes\": %zu, \"memfd\": %d, \"uffd\": %d, \"kernel_faults\": %d}\n",
        (int)getpid(), region.length, region.memfd, region.uffd, region.kernel_faults);
    if (daemonize) { if (write(ready[1], line, (size_t)wrote) < 0) return 1; close(ready[1]); }
    else { fputs(line, stdout); fflush(stdout); }
    unsigned char expected[PAGE];
    for (;;) {
        int fd = accept4(listener, NULL, NULL, SOCK_CLOEXEC);
        if (fd < 0) { if (errno == EINTR) continue; break; }
        char verb[32] = {0};
        ssize_t got = read(fd, verb, sizeof(verb) - 1);
        if (got <= 0) { close(fd); continue; }
        if (!strncmp(verb, "QUIT", 4)) {
            if (write(fd, "{\"ok\": true}\n", 13) < 0) {}
            close(fd);
            break;
        }
        uint64_t started = now_ns(), mismatched = 0, mutated = 0;
        if (!strncmp(verb, "MUTATE", 6))
            for (uint64_t index = 0; index < pages; index += 5) {
                versions[index]++;
                pattern(base + index * PAGE, seed, index, versions[index]);
                mutated++;
            }
        int sysread_errno = 0;
        if (!strncmp(verb, "SYSREAD", 7)) {
            /* A kernel-mode access to the region: read(2) stores into page 0.
             * With a user-mode-only userfaultfd this is unsafe while cold. */
            int zero = open("/dev/zero", O_RDONLY | O_CLOEXEC);
            unsigned char keep[64];
            pattern(expected, seed, 0, versions[0]);
            memcpy(keep, expected, sizeof(keep));
            if (zero < 0 || read(zero, base, sizeof(keep)) != (ssize_t)sizeof(keep)) sysread_errno = errno ? errno : EIO;
            else memcpy(base, keep, sizeof(keep));
            if (zero >= 0) close(zero);
        }
        uint32_t actual = 0, wanted = 0;
        for (uint64_t index = 0; index < pages; index++) {
            pattern(expected, seed, index, versions[index]);
            wanted = crc_update(wanted, expected, PAGE);
            actual = crc_update(actual, base + index * PAGE, PAGE);
            mismatched += memcmp(expected, base + index * PAGE, PAGE) != 0;
        }
        wrote = snprintf(line, sizeof(line),
            "{\"ok\": %s, \"pages\": %" PRIu64 ", \"bytes\": %zu, \"crc\": \"%08x\", \"expected_crc\": \"%08x\","
            " \"mismatched_pages\": %" PRIu64 ", \"mutated_pages\": %" PRIu64 ", \"sysread_errno\": %d,"
            " \"elapsed_ns\": %" PRIu64 ", \"pid\": %d}\n",
            (!mismatched && actual == wanted && !sysread_errno) ? "true" : "false", pages, region.length,
            actual, wanted, mismatched, mutated, sysread_errno, now_ns() - started, (int)getpid());
        if (write(fd, line, (size_t)wrote) < 0) {}
        close(fd);
    }
    close(listener);
    unlink(socket_path);
    free(versions);
    crate_coop_destroy(&region);
    return 0;
usage:
    fprintf(stderr, "Usage: %s serve --socket PATH --size-mib N [--seed S] [--daemonize]\n"
                    "       %s check|mutate|sysread|quit --socket PATH\n", argv[0], argv[0]);
    return 2;
}
